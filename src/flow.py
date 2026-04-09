"""Unified orchestration loop — scope → configurable cycle → re-scope → deliver.


The flow is the engine's core loop. It processes a task through these phases:
1. Scope — decompose into subtasks via LLM (when configured)
2. For each subtask: run the configurable cycle (sequence of LLM and command steps)
3. Re-scope after each batch — zero subtasks = done
4. Checkpoint every max_iterations batches (pauses for human approval)
5. Deliver workspace and fire completion hook

The cycle is fully configurable: consumers define step sequences with LLM calls
and shell commands. Jump control (on_fail, next) enables retry patterns.
max_retries caps total jumps per subtask.

Default execution mode is LLM-direct: the engine calls the provider pool
directly with the task context. The workspace's CLAUDE.md provides domain
knowledge via cwd.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Any

from src.models import (
    CycleConfig,
    CycleStep,
    FlowPolicies,
    HooksConfig,
    ProviderError,
    StepResult,
    StepStatus,
    TaoError,
    TaskStatus,
    WorkspaceConfig,
)
from src.policy import (
    check_iteration_limit,
    check_subtask_limit,
    validate_cycle_config,
    validate_policies,
)
from src.step_runner import format_template_cmd
from src.store import Store

logger = logging.getLogger(__name__)

# Max characters for LLM context injection. Prevents absurdly large prompts
# when a previous step produces verbose output (e.g., full test logs).
# Full output is always saved to .tao/logs/ for human review.
_MAX_CONTEXT_CHARS = 50_000


def _save_log_and_truncate(
    content: str,
    task_id: int,
    step_id: str,
    subtask_index: int,
    workspace_path: str,
) -> str:
    """Save full content to a log file and return truncated version with reference.

    If content is within _MAX_CONTEXT_CHARS, returns it unchanged (no file written).
    Otherwise, writes to .tao/logs/task-{id}-{step}-{subtask}.log and returns
    a truncated version with a pointer to the full file.
    """
    if len(content) <= _MAX_CONTEXT_CHARS:
        return content

    log_dir = os.path.join(workspace_path, ".tao", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"task-{task_id}-{step_id}-{subtask_index + 1}.log")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(content)

    # Keep first and last portions, add reference to full file
    head = content[:_MAX_CONTEXT_CHARS // 2]
    tail = content[-_MAX_CONTEXT_CHARS // 2:]
    truncated = (
        f"{head}\n\n"
        f"... [TRUNCATED — full output: {log_file}] ...\n\n"
        f"{tail}"
    )
    logger.info(
        "[task %d] %s output truncated: %d -> %d chars, saved to %s",
        task_id, step_id, len(content), len(truncated), log_file,
    )
    return truncated

# Maps task_id → Event. Set the event to request a graceful stop.
_stop_events: dict[int, threading.Event] = {}


def request_stop(task_id: int) -> None:
    """Request graceful stop for a running flow. Idempotent."""
    event = _stop_events.get(task_id)
    if event is not None:
        logger.info("[task %d] stop requested", task_id)
        event.set()


def _set_status(store: Store, task_id: int, status: TaskStatus, reason: str = "") -> None:
    """Update task status with logging. Never raises — logs errors instead."""
    if reason:
        logger.info("[task %d] → %s (%s)", task_id, status.value, reason)
    else:
        logger.info("[task %d] → %s", task_id, status.value)
    try:
        store.update_task_status(task_id, status)
        # Clear current_step on terminal states
        if status != TaskStatus.RUNNING:
            store.update_current_step(task_id, "")
    except Exception:
        logger.exception("[task %d] failed to update status to %s in store", task_id, status.value)


def _parse_flow_config(
    config: dict[str, Any],
) -> tuple[WorkspaceConfig, HooksConfig, FlowPolicies, dict, CycleConfig, list[str]]:
    """Extract and parse the 6 config components from a task config dict.

    Returns:
        (workspace_cfg, hooks_cfg, policies, scope_cfg, cycle_config, tools)
    """
    # Workspace
    ws = config.get("workspace", {})
    workspace_cfg = ws if isinstance(ws, WorkspaceConfig) else WorkspaceConfig.from_dict(ws)

    # Hooks
    hk = config.get("hooks", {})
    hooks_cfg = hk if isinstance(hk, HooksConfig) else HooksConfig.from_dict(hk)

    # Policies
    pol = config.get("policies", {})
    if isinstance(pol, FlowPolicies):
        policies = pol
    else:
        policies = validate_policies(pol)

    # Scope config (pass-through dict with model_spec, timeout, failover)
    scope_cfg: dict = config.get("scope", {})

    # Cycle config
    cycle_raw = config.get("cycle", [])
    if isinstance(cycle_raw, CycleConfig):
        cycle_config = cycle_raw
    else:
        if not cycle_raw:
            raise TaoError(
                "task config missing 'cycle' — define at least one step. "
                "Example: \"cycle\": [{\"id\": \"run\", \"type\": \"llm\", "
                "\"prompt\": \"Do the task.\", \"model_spec\": \"sonnet@claude\"}]"
            )
        max_retries = config.get("max_retries", 3)
        steps = [CycleStep.from_dict(s) for s in cycle_raw]
        cycle_config = CycleConfig(steps=steps, max_retries=max_retries)

    validate_cycle_config(cycle_config)

    # Tools — if specified, only these are auto-approved (--allowedTools).
    # If omitted, all tools are permitted (--dangerously-skip-permissions).
    tools: list[str] = config.get("tools", [])

    return workspace_cfg, hooks_cfg, policies, scope_cfg, cycle_config, tools


def _run_workspace_cmd(template: str, values: dict[str, str]) -> tuple[bool, str]:
    """Run a workspace lifecycle command via subprocess.

    Trust model: workspace commands are user-configured, same trust as pack
    commands (see step_runner.py module docstring).

    Returns:
        (success, stdout) where success is True if exit code == 0.
    """
    if not template:
        return True, ""

    tid = values.get("task_id", "?")
    cmd = format_template_cmd(template, values)
    logger.debug("[task %s] workspace cmd: %s", tid, cmd)

    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S602
            cmd,
            shell=True,
            capture_output=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[task %s] workspace cmd timed out (120s): %s", tid, cmd)
        return False, ""

    elapsed = time.monotonic() - start
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "[task %s] workspace cmd failed (exit %d, %.1fs): %s — %s",
            tid,
            proc.returncode,
            elapsed,
            cmd,
            stderr[:500],
        )
        return False, stdout

    logger.debug("[task %s] workspace cmd completed in %.1fs", tid, elapsed)
    return True, stdout


def _fire_hook(
    template: str,
    values: dict[str, str],
    *,
    data_content: str | None = None,
    data_key: str = "output_file",
) -> None:
    """Fire a hook command. Non-fatal — catches all exceptions, logs warning.

    If data_content is provided, writes it to a temp file and adds the path
    to values[data_key]. The temp file is cleaned up in a finally block.
    """
    if not template:
        return

    tmp_path: str | None = None
    try:
        if data_content is not None:
            fd, tmp_path = tempfile.mkstemp(suffix=".json", prefix="tao_hook_")
            os.write(fd, data_content.encode("utf-8"))
            os.close(fd)
            values = dict(values)
            values[data_key] = tmp_path

        cmd = format_template_cmd(template, values)
        logger.debug("hook: %s", cmd)
        subprocess.run(  # noqa: S602
            cmd,
            shell=True,
            capture_output=True,
            timeout=30,
        )
    except Exception:
        tid = values.get("task_id", "?")
        logger.warning("[task %s] hook failed: %s", tid, template, exc_info=True)
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _run_scope_llm_step(
    ctx: dict[str, Any],
    step_cfg: dict[str, Any],
    pool: Any,
    workspace_path: str,
    tools: list[str] | None = None,
) -> StepResult:
    """Run the scope/re-scope step via LLM.

    Builds a scope prompt from task data and calls the provider pool.
    """
    from src.providers.pool import parse_model_spec

    model_spec = step_cfg.get("model_spec", "sonnet")
    model, provider_name = parse_model_spec(model_spec)
    failover_specs: list[str] = step_cfg.get("failover", [])
    timeout = step_cfg.get("timeout", 1800)

    task_title = ctx.get("task_title", "")
    task_body = ctx.get("task_body", "")
    batch_size = ctx.get("batch_size", 5)
    completed_summaries = ctx.get("completed_summaries", "")
    cwd = workspace_path or None

    # Human message section (from unblock context)
    human_message = ctx.get("human_message", "")
    human_section = ""
    if human_message:
        human_section = f"\n\n## Additional context from user\n{human_message}"

    # Build prompt based on context
    if completed_summaries:
        # Re-scope: we have prior work done
        prompt = (
            f"You are continuing work on a task. Here is what has been completed so far:\n\n"
            f"{completed_summaries}\n\n"
            f"# Original task: {task_title}\n\n{task_body}\n\n"
            f"Determine what remains to be done. Produce the next batch of up to "
            f"{batch_size} subtasks. If everything is done, return an empty array."
            f"{human_section}\n\n"
            'Output ONLY a JSON array of objects with "title" and "description".\n'
            "Empty array means the task is complete. No other text."
        )
    else:
        # Initial scope
        prompt = (
            f"Decompose this task into {batch_size} or fewer focused subtasks.\n\n"
            f"# Task: {task_title}\n\n{task_body}"
            f"{human_section}\n\n"
            'Output ONLY a JSON array of objects with "title" and "description".\n'
            "No other text."
        )

    # Build ordered list of (model, provider) to try: primary + failover
    attempts = [(model, provider_name)]
    for spec in failover_specs:
        fo_model, fo_provider = parse_model_spec(spec)
        attempts.append((fo_model, fo_provider))

    response = None
    last_error = ""
    for try_model, try_provider in attempts:
        try:
            response = pool.call(
                prompt=prompt,
                model=try_model,
                tools=tools,
                timeout=timeout,
                cwd=cwd,
                provider=try_provider,
            )
            break
        except ProviderError as e:
            last_error = str(e)
            if len(attempts) > 1:
                logger.warning("failover: %s@%s failed: %s", try_model, try_provider or "auto", e)
        except Exception as e:
            logger.exception("unexpected error in scope LLM step")
            last_error = f"LLM call error: {e}"

    if response is None:
        return StepResult(status=StepStatus.FAILED, output=last_error)

    output = response.get("output", "")
    success = response.get("success", False)

    if not success:
        return StepResult(
            status=StepStatus.FAILED,
            output=response.get("error", output or "LLM call failed"),
            tokens_in=response.get("tokens_in", 0),
            tokens_out=response.get("tokens_out", 0),
            cost_usd=response.get("cost_usd", 0.0),
            elapsed_s=response.get("elapsed_s", 0.0),
        )

    # Build data — scope always produces subtasks
    subtasks = _parse_scope_from_llm(output)
    return StepResult(
        status=StepStatus.SUCCEEDED,
        output=output,
        data={"subtasks": subtasks},
        tokens_in=response.get("tokens_in", 0),
        tokens_out=response.get("tokens_out", 0),
        cost_usd=response.get("cost_usd", 0.0),
        elapsed_s=response.get("elapsed_s", 0.0),
        session_id=response.get("session_id", ""),
    )


def _filter_subtasks(data: Any) -> list[dict[str, Any]]:
    """Filter a list to only valid subtask dicts with 'title' key."""
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict) and "title" in s]


def _parse_scope_from_llm(text: str) -> list[dict[str, Any]]:
    """Extract subtasks from LLM scope output.

    Accepts:
    - JSON array: [{title, description}, ...]
    - JSON object with "subtasks" key: {"subtasks": [...]}  (backward compat)

    Returns:
        List of subtask dicts. Empty list means the task is complete.
    """
    import re

    text = text.strip()

    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return _filter_subtasks(data)
        if isinstance(data, dict) and "subtasks" in data:
            return _filter_subtasks(data.get("subtasks", []))
    except json.JSONDecodeError:
        pass

    # Try extracting JSON array from surrounding text
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return _filter_subtasks(data)
        except json.JSONDecodeError:
            pass

    # Try extracting JSON object with subtasks key
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, dict) and "subtasks" in data:
                return _filter_subtasks(data.get("subtasks", []))
        except json.JSONDecodeError:
            pass

    logger.warning("could not parse scope output: %s", text[:200])
    return []


def _run_commands(
    commands: list[str],
    workspace_path: str,
    store: Store,
    task_id: int,
    subtask_index: int,
    role_name: str,
    *,
    best_effort: bool = False,
) -> list[dict[str, Any]]:
    """Run a list of shell commands and record traces.

    Args:
        commands: Shell commands to execute.
        workspace_path: Working directory.
        store: Store for trace recording.
        task_id: Task ID.
        subtask_index: Current subtask index.
        role_name: Trace role name (e.g. step ID like "validate").
        best_effort: If True, continue on failure. If False, return on first failure.

    Returns:
        List of result dicts with keys: command, passed, output, elapsed_s.
    """
    from src.gates import run_gate_command

    results: list[dict[str, Any]] = []
    for command in commands:
        start = time.monotonic()
        passed, output = run_gate_command(command, workspace_path)
        elapsed = time.monotonic() - start

        result = {
            "command": command,
            "passed": passed,
            "output": output,
            "elapsed_s": elapsed,
        }
        results.append(result)

        store.record_trace(
            task_id,
            {
                "subtask_index": subtask_index,
                "role": role_name,
                "model": "",
                "tokens_in": 0,
                "tokens_out": 0,
                "cost_usd": 0.0,
                "elapsed_s": elapsed,
                "success": passed,
                "attempt": 1,
                "error": output if not passed else "",
                "label": command,
            },
        )

        if not passed and not best_effort:
            break

    return results


def _run_cycle_llm_step(
    step: CycleStep,
    subtask: dict[str, Any],
    last_llm_output: str,
    pending_errors: str,
    pool: Any,
    workspace_path: str,
    store: Store,
    task_id: int,
    subtask_index: int,
    hooks_cfg: HooksConfig,
    tools: list[str] | None = None,
) -> StepResult:
    """Run a cycle LLM step with context injection.

    Prompt construction rules:
        - First LLM step (no prior output): subtask description + --- + step prompt
        - Subsequent steps: last LLM output + --- + step prompt
        - After on_fail jump: + --- + validation errors
    """
    from src.providers.pool import parse_model_spec

    model_spec = step.model_spec or "sonnet"
    model, provider_name = parse_model_spec(model_spec)
    failover_specs = step.failover
    timeout = step.timeout
    cwd = workspace_path or None

    # Context injection — truncate large outputs to prevent absurd prompts.
    # Full content is saved to .tao/logs/ for human review.
    if last_llm_output:
        context_section = _save_log_and_truncate(
            last_llm_output, task_id, f"{step.id}-context", subtask_index, workspace_path,
        )
    else:
        context_section = subtask.get("description", "")

    parts = [context_section, "---", step.prompt]
    if pending_errors:
        truncated_errors = _save_log_and_truncate(
            pending_errors, task_id, f"{step.id}-errors", subtask_index, workspace_path,
        )
        parts.extend(["---", truncated_errors])
    prompt = "\n".join(parts)

    subtask_title = subtask.get("title", "")
    step_label = f"{step.id}:{subtask_index + 1} — {subtask_title}"[:120]
    store.update_current_step(task_id, step_label)

    # Call LLM with failover
    attempts = [(model, provider_name)]
    for spec in failover_specs:
        fo_model, fo_provider = parse_model_spec(spec)
        attempts.append((fo_model, fo_provider))

    response = None
    last_error = ""
    for try_model, try_provider in attempts:
        try:
            response = pool.call(
                prompt=prompt,
                model=try_model,
                tools=tools,
                timeout=timeout,
                cwd=cwd,
                provider=try_provider,
            )
            break
        except ProviderError as e:
            last_error = str(e)
            if len(attempts) > 1:
                logger.warning("failover: %s@%s failed: %s", try_model, try_provider or "auto", e)
        except Exception as e:
            logger.exception("unexpected error in cycle LLM step")
            last_error = f"LLM call error: {e}"

    if response is None:
        result = StepResult(status=StepStatus.FAILED, output=last_error)
    else:
        output = response.get("output", "")
        success = response.get("success", False)
        if not success:
            result = StepResult(
                status=StepStatus.FAILED,
                output=response.get("error", output or "LLM call failed"),
                tokens_in=response.get("tokens_in", 0),
                tokens_out=response.get("tokens_out", 0),
                cost_usd=response.get("cost_usd", 0.0),
                elapsed_s=response.get("elapsed_s", 0.0),
            )
        else:
            result = StepResult(
                status=StepStatus.SUCCEEDED,
                output=output,
                tokens_in=response.get("tokens_in", 0),
                tokens_out=response.get("tokens_out", 0),
                cost_usd=response.get("cost_usd", 0.0),
                elapsed_s=response.get("elapsed_s", 0.0),
                session_id=response.get("session_id", ""),
            )

    # Record trace
    store.record_trace(
        task_id,
        {
            "subtask_index": subtask_index,
            "role": step.id,
            "model": model_spec,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "elapsed_s": result.elapsed_s,
            "success": result.status == StepStatus.SUCCEEDED,
            "attempt": 1,
            "error": result.output if result.status != StepStatus.SUCCEEDED else "",
            "label": step_label,
        },
    )

    # Fire hook
    _fire_hook(
        hooks_cfg.on_step_output,
        {"task_id": str(task_id), "step_name": step.id},
        data_content=result.output,
    )

    return result


def _format_command_errors(results: list[dict[str, Any]]) -> str:
    """Format failed command results into a readable string for LLM context."""
    lines: list[str] = []
    for r in results:
        if not r.get("passed"):
            lines.append(f"$ {r.get('command', '')}")
            output = r.get("output", "")
            if output:
                lines.append(output)
            lines.append("")
    return "\n".join(lines).strip()


def _run_subtask_cycle(
    subtask: dict[str, Any],
    subtask_index: int,
    cycle_config: CycleConfig,
    task_title: str,
    workspace_path: str,
    pool: Any,
    store: Store,
    task_id: int,
    hooks_cfg: HooksConfig,
    stop_event: threading.Event,
    base_env: dict[str, str],
    resume_step_index: int = 0,
    resume_last_llm_output: str = "",
    tools: list[str] | None = None,
) -> tuple[StepStatus, str, int, str]:
    """Process one subtask through the cycle interpreter.

    The cycle is a sequence of steps (LLM or command). Control flow keywords
    (on_fail, next) enable jump patterns. max_retries caps total backward
    jumps (forward jumps don't create loops and are free).

    Returns:
        (status, blocked_reason, last_step_index, last_llm_output)
    """
    steps = cycle_config.steps
    if not steps:
        return StepStatus.SUCCEEDED, "", 0, ""

    step_index_map = {s.id: i for i, s in enumerate(steps)}
    pc = resume_step_index
    last_llm_output = resume_last_llm_output
    pending_errors = ""
    jumps_used = 0

    while pc < len(steps):
        if stop_event.is_set():
            return StepStatus.SUCCEEDED, "", pc, last_llm_output

        step = steps[pc]

        if step.type == "llm":
            result = _run_cycle_llm_step(
                step, subtask, last_llm_output, pending_errors,
                pool, workspace_path, store, task_id, subtask_index,
                hooks_cfg, tools=tools,
            )
            pending_errors = ""  # consumed

            if result.status != StepStatus.SUCCEEDED:
                _fire_hook(
                    hooks_cfg.on_error,
                    {"task_id": str(task_id), "error": result.output},
                )
                return StepStatus.FAILED, "", pc, last_llm_output

            last_llm_output = result.output

            if step.next:
                target = step_index_map.get(step.next)
                if target is None:
                    logger.error(
                        "[task %d] step '%s': invalid next '%s'",
                        task_id, step.id, step.next,
                    )
                    return StepStatus.FAILED, "", pc, last_llm_output
                if target <= pc:
                    jumps_used += 1
                if jumps_used > cycle_config.max_retries:
                    logger.warning(
                        "[task %d] subtask %d: max_retries (%d) exhausted",
                        task_id, subtask_index + 1, cycle_config.max_retries,
                    )
                    _fire_hook(
                        hooks_cfg.on_error,
                        {"task_id": str(task_id),
                         "error": f"subtask {subtask_index + 1}: max_retries exhausted"},
                    )
                    return StepStatus.FAILED, "", pc, last_llm_output
                pc = target
            else:
                pc += 1

        elif step.type == "command":
            subtask_title = subtask.get("title", "")
            step_label = f"{step.id}:{subtask_index + 1} — {subtask_title}"[:120]
            store.update_current_step(task_id, step_label)

            cmd_results = _run_commands(
                step.commands, workspace_path, store, task_id,
                subtask_index, step.id, best_effort=True,
            )
            all_passed = all(r.get("passed", False) for r in cmd_results)

            if all_passed:
                if step.next:
                    target = step_index_map.get(step.next)
                    if target is None:
                        logger.error(
                            "[task %d] step '%s': invalid next '%s'",
                            task_id, step.id, step.next,
                        )
                        return StepStatus.FAILED, "", pc, last_llm_output
                    if target <= pc:
                        jumps_used += 1
                    if jumps_used > cycle_config.max_retries:
                        logger.warning(
                            "[task %d] subtask %d: max_retries (%d) exhausted",
                            task_id, subtask_index + 1, cycle_config.max_retries,
                        )
                        _fire_hook(
                            hooks_cfg.on_error,
                            {"task_id": str(task_id),
                             "error": f"subtask {subtask_index + 1}: max_retries exhausted"},
                        )
                        return StepStatus.FAILED, "", pc, last_llm_output
                    pc = target
                else:
                    pc += 1
            else:
                if step.on_fail:
                    target = step_index_map.get(step.on_fail)
                    if target is None:
                        logger.error(
                            "[task %d] step '%s': invalid on_fail '%s'",
                            task_id, step.id, step.on_fail,
                        )
                        return StepStatus.FAILED, "", pc, last_llm_output
                    pending_errors = _format_command_errors(cmd_results)
                    if target <= pc:
                        jumps_used += 1
                    if jumps_used > cycle_config.max_retries:
                        logger.warning(
                            "[task %d] subtask %d: max_retries (%d) exhausted",
                            task_id, subtask_index + 1, cycle_config.max_retries,
                        )
                        _fire_hook(
                            hooks_cfg.on_error,
                            {"task_id": str(task_id),
                             "error": f"subtask {subtask_index + 1}: max_retries exhausted"},
                        )
                        return StepStatus.FAILED, "", pc, last_llm_output
                    pc = target
                else:
                    _fire_hook(
                        hooks_cfg.on_error,
                        {"task_id": str(task_id),
                         "error": f"command step '{step.id}' failed"},
                    )
                    return StepStatus.FAILED, "", pc, last_llm_output

        else:
            logger.error("[task %d] unknown step type: %s", task_id, step.type)
            return StepStatus.FAILED, "", pc, last_llm_output

    return StepStatus.SUCCEEDED, "", len(steps) - 1, last_llm_output


def run_flow(
    task_id: int,
    *,
    store: Store,
    pool: Any,
    config: dict[str, Any],
) -> TaskStatus:
    """Run the orchestration flow for a task. Main public entry point.

    Args:
        task_id: ID of the task in the store.
        store: Persistence backend.
        pool: ProviderPool for LLM calls.
        config: Task configuration dict (cwd, scope, cycle, policies, hooks).

    Returns:
        Final TaskStatus.
    """
    stop_event = threading.Event()
    _stop_events[task_id] = stop_event
    try:
        return _run_flow_inner(
            task_id,
            store=store,
            pool=pool,
            config=config,
            stop_event=stop_event,
        )
    finally:
        _stop_events.pop(task_id, None)


def _run_flow_inner(
    task_id: int,
    *,
    store: Store,
    pool: Any,
    config: dict[str, Any],
    stop_event: threading.Event,
) -> TaskStatus:
    """Inner flow logic — separated for clean stop-event setup/teardown.

    Context is structured into 3 levels with distinct lifecycles:
      - Task context: completed_summaries, iteration — lives across the batch loop.
      - Subtask context: step_index, last_llm_output — lives across one subtask.
      - Step context: pending_errors — lives within the cycle interpreter.

    Immutable task data (title, body, workspace_path, batch_size) is read from
    the task record and config directly, not stored in context.
    """
    workspace_cfg, hooks_cfg, policies, scope_cfg, cycle_config, tools = (
        _parse_flow_config(config)
    )

    has_scope = bool(scope_cfg)
    configured_cwd = config.get("cwd", "")
    if not configured_cwd:
        raise TaoError("workspace path (cwd) is required in task config")
    if not os.path.isdir(configured_cwd):
        raise TaoError(f"cwd does not exist or is not a directory: {configured_cwd}")
    base_env: dict[str, str] = {"TAO_TASK_ID": str(task_id)}

    task = store.get_task(task_id)
    task_title = task["title"]
    task_body = task["body"]
    _set_status(store, task_id, TaskStatus.RUNNING)

    checkpoint = store.load_checkpoint(task_id)
    flow_complete_fired = False
    workspace_path = configured_cwd

    def _fire_flow_complete() -> None:
        nonlocal flow_complete_fired
        if flow_complete_fired:
            return
        flow_complete_fired = True
        summary = json.dumps(store.get_summary(task_id))
        _fire_hook(
            hooks_cfg.on_flow_complete,
            {"task_id": str(task_id)},
            data_content=summary,
            data_key="summary_file",
        )

    def _build_scope_ctx(task_ctx: dict[str, Any]) -> dict[str, Any]:
        """Build flat dict for scope/re-scope from task data + task_ctx."""
        ctx = {
            "task_title": task_title,
            "task_body": task_body,
            "workspace_path": workspace_path,
            "batch_size": policies.batch_size,
            "completed_summaries": task_ctx.get("completed_summaries", ""),
            "iteration": task_ctx.get("iteration", 1),
        }
        if task_ctx.get("human_message"):
            ctx["human_message"] = task_ctx["human_message"]
        return ctx

    def _run_scope(task_ctx: dict[str, Any], label: str) -> StepResult:
        """Run scope step (initial or re-scope). Returns StepResult."""
        store.update_current_step(task_id, label)
        scope_ctx = _build_scope_ctx(task_ctx)

        result = _run_scope_llm_step(scope_ctx, scope_cfg, pool, workspace_path, tools=tools)

        store.record_trace(task_id, {
            "subtask_index": 0,
            "role": "scope",
            "model": scope_cfg.get("model_spec", ""),
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "elapsed_s": result.elapsed_s,
            "success": result.status == StepStatus.SUCCEEDED,
            "attempt": 1,
            "error": result.output if result.status != StepStatus.SUCCEEDED else "",
            "label": label,
        })

        _fire_hook(
            hooks_cfg.on_scope_complete,
            {"task_id": str(task_id)},
            data_content=result.output,
        )

        return result

    def _save_checkpoint(
        completed_subtasks: list,
        subtasks: list,
        task_ctx: dict[str, Any],
        batch_number: int,
        *,
        subtask_context: dict[str, Any] | None = None,
        retry_scope: bool = False,
        blocked_reason: str = "",
    ) -> None:
        """Save checkpoint with structured context levels."""
        cp: dict[str, Any] = {
            "workspace_path": workspace_path,
            "completed_subtasks": completed_subtasks,
            "pending_subtasks": subtasks,
            "batch_number": batch_number,
            "task_context": dict(task_ctx),
        }
        if subtask_context is not None:
            cp["subtask_context"] = subtask_context
        if retry_scope:
            cp["retry_scope"] = True
        if blocked_reason:
            cp["blocked_reason"] = blocked_reason
        store.save_checkpoint(task_id, cp)

    try:
        # --- Initialize state ---
        if checkpoint is None:
            # Fresh start
            ws_values = {"task_id": str(task_id)}
            if workspace_cfg.create:
                ok, stdout = _run_workspace_cmd(workspace_cfg.create, ws_values)
                if not ok:
                    _fire_hook(
                        hooks_cfg.on_error,
                        {"task_id": str(task_id), "error": "workspace create failed"},
                    )
                    _set_status(store, task_id, TaskStatus.FAILED, "workspace create failed")
                    _fire_flow_complete()
                    return TaskStatus.FAILED
                workspace_path = stdout.strip()
                if not os.path.isdir(workspace_path):
                    _set_status(store, task_id, TaskStatus.FAILED,
                                f"workspace.create returned invalid path: {workspace_path}")
                    _fire_flow_complete()
                    return TaskStatus.FAILED
            else:
                workspace_path = configured_cwd

            # Task context (level 1) — mutable task-level state
            task_ctx: dict[str, Any] = {
                "completed_summaries": "",
                "iteration": 1,
            }

            # Run scope (if configured)
            if has_scope:
                if stop_event.is_set():
                    _set_status(store, task_id, TaskStatus.STOPPED)
                    _fire_flow_complete()
                    return TaskStatus.STOPPED

                scope_result = _run_scope(task_ctx, "scope")

                if scope_result.status != StepStatus.SUCCEEDED:
                    if scope_result.blocked_reason:
                        _save_checkpoint(
                            [], [], task_ctx, 1,
                            blocked_reason=scope_result.blocked_reason,
                        )
                        _fire_hook(
                            hooks_cfg.on_blocked,
                            {"task_id": str(task_id), "reason": scope_result.blocked_reason},
                        )
                        _set_status(store, task_id, TaskStatus.BLOCKED)
                        _fire_flow_complete()
                        return TaskStatus.BLOCKED
                    _fire_hook(
                        hooks_cfg.on_error,
                        {"task_id": str(task_id), "error": f"scope failed: {scope_result.output}"},
                    )
                    _save_checkpoint([], [], task_ctx, 1, retry_scope=True)
                    _set_status(store, task_id, TaskStatus.FAILED, "scope failed")
                    _fire_flow_complete()
                    return TaskStatus.FAILED

                subtasks = scope_result.data.get("subtasks", [])
                logger.info(
                    "[task %d] scope produced %d subtask(s)",
                    task_id, len(subtasks),
                )
                check_subtask_limit(len(subtasks), policies.max_subtasks)
                store.update_subtasks(task_id, subtasks)
            else:
                # No scope — single subtask from task title/body
                subtasks = [{"title": task_title, "description": task_body}]
                store.update_subtasks(task_id, subtasks)

            completed_subtasks: list[dict[str, Any]] = []
            batch_number = 1
            iteration = 1
            resume_subtask_index: int | None = None
            resume_step_index: int = 0
            resume_last_llm_output: str = ""
        else:
            # Resume from checkpoint
            logger.info("[task %d] resuming from checkpoint", task_id)
            workspace_path = checkpoint.get("workspace_path", "") or configured_cwd
            completed_subtasks = checkpoint.get("completed_subtasks", [])
            subtasks = checkpoint.get("pending_subtasks", [])
            batch_number = checkpoint.get("batch_number", 1)

            # Backward compat: old checkpoints use "context" key, new use "task_context"
            if "task_context" in checkpoint:
                task_ctx = dict(checkpoint["task_context"])
            elif "context" in checkpoint:
                # Migrate from flat context: extract task-level fields
                old_ctx = checkpoint["context"]
                task_ctx = {
                    "completed_summaries": old_ctx.get("completed_summaries", ""),
                    "iteration": old_ctx.get("iteration", 1),
                }
            else:
                task_ctx = {"completed_summaries": "", "iteration": 1}

            iteration = task_ctx.get("iteration", 1)

            # Resume subtask context (if mid-subtask)
            if "subtask_context" in checkpoint:
                sub_ctx_saved = checkpoint["subtask_context"]
                resume_subtask_index = sub_ctx_saved.get("subtask_index")
                resume_step_index = sub_ctx_saved.get("step_index", 0)
                resume_last_llm_output = sub_ctx_saved.get("last_llm_output", "")
            else:
                # Backward compat: old checkpoint keys
                resume_subtask_index = checkpoint.get("current_subtask_index")
                resume_step_index = 0
                resume_last_llm_output = ""

            # Retry scope if the checkpoint was saved after a scope/re-scope failure
            if checkpoint.get("retry_scope") and has_scope:
                logger.info("[task %d] retrying scope from checkpoint", task_id)

                # Build scope context with completed info if available
                if completed_subtasks:
                    task_ctx["completed_summaries"] = "\n".join(
                        f"Task {j + 1}: {s.get('title', '')} — completed"
                        for j, s in enumerate(completed_subtasks)
                    )

                scope_result = _run_scope(task_ctx, "scope")

                if scope_result.status != StepStatus.SUCCEEDED:
                    _save_checkpoint(
                        completed_subtasks, [], task_ctx, batch_number,
                        retry_scope=True,
                    )
                    _set_status(store, task_id, TaskStatus.FAILED, "scope retry failed")
                    _fire_flow_complete()
                    return TaskStatus.FAILED

                subtasks = scope_result.data.get("subtasks", [])
                logger.info("[task %d] scope retry produced %d subtask(s)", task_id, len(subtasks))
                check_subtask_limit(len(subtasks), policies.max_subtasks)
                store.update_subtasks(task_id, subtasks)
                resume_subtask_index = None
                resume_step_index = 0
                resume_last_llm_output = ""

        # --- Main loop ---
        while True:
            if stop_event.is_set():
                _save_checkpoint(completed_subtasks, subtasks, task_ctx, batch_number)
                _set_status(store, task_id, TaskStatus.STOPPED)
                _fire_flow_complete()
                return TaskStatus.STOPPED

            if check_iteration_limit(iteration, policies.max_iterations):
                logger.info(
                    "[task %d] iteration limit reached (%d), checkpoint",
                    task_id, policies.max_iterations,
                )
                _save_checkpoint(
                    completed_subtasks, subtasks, task_ctx, batch_number,
                    blocked_reason="iteration limit reached — awaiting human approval",
                )
                _fire_hook(
                    hooks_cfg.on_blocked,
                    {"task_id": str(task_id), "reason": "iteration limit reached"},
                )
                _set_status(store, task_id, TaskStatus.BLOCKED)
                _fire_flow_complete()
                return TaskStatus.BLOCKED

            # Scope returned empty subtasks — trivially complete
            if not subtasks:
                break

            for i, subtask in enumerate(subtasks):
                # On resume, skip already-completed subtasks
                if resume_subtask_index is not None and i < resume_subtask_index:
                    continue

                logger.info(
                    "[task %d] subtask %d/%d: %s",
                    task_id, i + 1, len(subtasks), subtask.get("title", ""),
                )

                if stop_event.is_set():
                    sub_ctx_cp = {
                        "subtask_index": i,
                        "step_index": 0,
                        "last_llm_output": "",
                    }
                    _save_checkpoint(
                        completed_subtasks, subtasks, task_ctx, batch_number,
                        subtask_context=sub_ctx_cp,
                    )
                    _set_status(store, task_id, TaskStatus.STOPPED)
                    _fire_flow_complete()
                    return TaskStatus.STOPPED

                # Determine resume state for this subtask
                is_resumed = resume_subtask_index is not None and i == resume_subtask_index
                step_idx_for_resume = resume_step_index if is_resumed else 0
                llm_output_for_resume = resume_last_llm_output if is_resumed else ""

                status, blocked_reason, last_step_index, last_llm_output = _run_subtask_cycle(
                    subtask=subtask,
                    subtask_index=i,
                    cycle_config=cycle_config,
                    task_title=task_title,
                    workspace_path=workspace_path,
                    pool=pool,
                    store=store,
                    task_id=task_id,
                    hooks_cfg=hooks_cfg,
                    stop_event=stop_event,
                    base_env=base_env,
                    resume_step_index=step_idx_for_resume,
                    resume_last_llm_output=llm_output_for_resume,
                    tools=tools,
                )

                # Clear resume state after the resumed subtask
                if resume_subtask_index is not None and i == resume_subtask_index:
                    resume_subtask_index = None
                    resume_step_index = 0
                    resume_last_llm_output = ""

                if stop_event.is_set():
                    _save_checkpoint(
                        completed_subtasks, subtasks, task_ctx, batch_number,
                        subtask_context={
                            "subtask_index": i,
                            "step_index": last_step_index,
                            "last_llm_output": last_llm_output,
                        },
                    )
                    _set_status(store, task_id, TaskStatus.STOPPED)
                    _fire_flow_complete()
                    return TaskStatus.STOPPED

                if status == StepStatus.FAILED and blocked_reason:
                    # Blocked — save checkpoint with mid-subtask position
                    _save_checkpoint(
                        completed_subtasks, subtasks, task_ctx, batch_number,
                        subtask_context={
                            "subtask_index": i,
                            "step_index": last_step_index,
                            "last_llm_output": last_llm_output,
                        },
                        blocked_reason=blocked_reason,
                    )
                    _fire_hook(
                        hooks_cfg.on_blocked,
                        {"task_id": str(task_id), "reason": blocked_reason},
                    )
                    _set_status(store, task_id, TaskStatus.BLOCKED)
                    _fire_flow_complete()
                    return TaskStatus.BLOCKED

                if status == StepStatus.FAILED:
                    _save_checkpoint(
                        completed_subtasks, subtasks, task_ctx, batch_number,
                        subtask_context={
                            "subtask_index": i,
                            "step_index": last_step_index,
                            "last_llm_output": last_llm_output,
                        },
                    )
                    _set_status(store, task_id, TaskStatus.FAILED)
                    _fire_flow_complete()
                    return TaskStatus.FAILED

                # Succeeded — persist workspace (non-fatal)
                if workspace_cfg.persist:
                    ws_ok, _ = _run_workspace_cmd(
                        workspace_cfg.persist,
                        {"workspace": workspace_path, "task_id": str(task_id)},
                    )
                    if not ws_ok:
                        logger.warning("[task %d] workspace persist failed (non-fatal)", task_id)

                completed_subtasks.append(subtask)

            # Batch complete — save checkpoint
            logger.info(
                "[task %d] batch %d complete (%d subtasks done)",
                task_id, batch_number, len(completed_subtasks),
            )
            _save_checkpoint(completed_subtasks, [], task_ctx, batch_number)

            # Always re-scope if scope is configured.
            # Zero subtasks from re-scope = task complete.
            if not has_scope:
                break

            iteration += 1
            batch_number += 1

            # Update task context for re-scope
            completed_summaries = "\n".join(
                f"Task {j + 1}: {s.get('title', '')} — completed"
                for j, s in enumerate(completed_subtasks)
            )
            task_ctx["completed_summaries"] = completed_summaries
            task_ctx["iteration"] = iteration

            scope_result = _run_scope(task_ctx, "re-scope")

            if scope_result.status != StepStatus.SUCCEEDED:
                _fire_hook(
                    hooks_cfg.on_error,
                    {"task_id": str(task_id),
                     "error": f"re-scope failed: {scope_result.output}"},
                )
                _save_checkpoint(
                    completed_subtasks, [], task_ctx, batch_number,
                    retry_scope=True,
                )
                _set_status(store, task_id, TaskStatus.FAILED, "re-scope failed")
                _fire_flow_complete()
                return TaskStatus.FAILED

            subtasks = scope_result.data.get("subtasks", [])
            logger.info(
                "[task %d] re-scope produced %d subtask(s)",
                task_id, len(subtasks),
            )
            store.update_subtasks(task_id, subtasks)
            # Loop continues — if subtasks is empty, next iteration breaks

        # --- Deliver ---
        if workspace_cfg.deliver:
            ok, _ = _run_workspace_cmd(
                workspace_cfg.deliver,
                {"workspace": workspace_path, "task_id": str(task_id)},
            )
            if not ok:
                _fire_hook(
                    hooks_cfg.on_error,
                    {"task_id": str(task_id), "error": "workspace deliver failed"},
                )
                _set_status(store, task_id, TaskStatus.FAILED, "deliver failed")
                _fire_flow_complete()
                return TaskStatus.FAILED

        if workspace_cfg.cleanup:
            _run_workspace_cmd(
                workspace_cfg.cleanup,
                {"workspace": workspace_path, "task_id": str(task_id)},
            )

        _set_status(store, task_id, TaskStatus.COMPLETED)
        _fire_flow_complete()
        return TaskStatus.COMPLETED

    except TaoError:
        _set_status(store, task_id, TaskStatus.FAILED)
        _fire_flow_complete()
        raise
    except Exception:
        logger.exception("unexpected error in flow for task %d", task_id)
        _set_status(store, task_id, TaskStatus.FAILED)
        _fire_flow_complete()
        raise
