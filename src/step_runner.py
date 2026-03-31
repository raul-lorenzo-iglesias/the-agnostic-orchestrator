"""Step runner — discover, validate, and execute steps as subprocesses.


A step is an executable (any language) with a JSON manifest declaring:
- command: how to run it
- needs: required context keys (validated before execution)
- provides: keys injected into context after success

Execution: JSON piped via stdin, StepResult read from stdout, stderr captured for logs.
Engine sets TAO_TASK_ID, TAO_SUBTASK_INDEX, TAO_ROLE env vars for trace correlation.

Trust model for shell=True: Pack sources are user-provided and trusted. The manifest's
``command`` field is authored by the pack creator, not by external input. This is
analogous to running a Makefile target — the user who provides the pack controls what
commands run.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess

from src.models import (
    StepManifest,
    StepResult,
    StepStatus,
    StepTimeoutError,
    TaoError,
)

logger = logging.getLogger(__name__)


def validate_context(manifest: StepManifest, ctx: dict) -> None:
    """Validate that all keys declared in manifest.needs are present in ctx.

    Raises:
        TaoError: if any required keys are missing.
    """
    missing = set(manifest.needs) - set(ctx.keys())
    if missing:
        raise TaoError(f"step '{manifest.name}' missing context keys: {sorted(missing)}")


def format_template_cmd(template: str, values: dict[str, str]) -> str:
    """Format a shell command template with escaped values.

    All values are escaped with shlex.quote() to prevent shell injection.
    Only known keys are accepted — unknown placeholders raise ValueError.

    Args:
        template: Command string with {key} placeholders.
        values: Mapping of placeholder names to values.

    Returns:
        Formatted command string with safely escaped values.

    Raises:
        ValueError: if template references an unknown placeholder key.
    """
    escaped = {k: shlex.quote(str(v)) for k, v in values.items()}
    try:
        return template.format_map(escaped)
    except KeyError as e:
        raise ValueError(f"unknown placeholder in template: {e}") from e


def run_step(
    manifest: StepManifest,
    ctx: dict,
    config: dict,
    *,
    pack_path: str,
    env_extras: dict[str, str] | None = None,
) -> StepResult:
    """Execute a step as a subprocess and parse the result.

    The step receives JSON on stdin (``{"ctx": ..., "config": ...}``),
    writes a JSON StepResult to stdout, and may write debug info to stderr.

    Trust model: ``shell=True`` is used because pack commands are authored
    by the user who provides the pack (see module docstring).

    Args:
        manifest: Step manifest with command, timeout, etc.
        ctx: Context dict passed to the step.
        config: Step-specific config (model, tools, timeout).
        pack_path: Working directory for the subprocess.
        env_extras: Additional environment variables (e.g., TAO_TASK_ID).

    Returns:
        StepResult parsed from the step's stdout.

    Raises:
        StepTimeoutError: if the step exceeds its configured timeout.
    """
    tid = (env_extras or {}).get("TAO_TASK_ID", "?")
    logger.info("[task %s] running step '%s' (timeout=%ds)", tid, manifest.name, manifest.timeout)

    env = os.environ.copy()
    if env_extras:
        env.update(env_extras)

    stdin_bytes = json.dumps({"ctx": ctx, "config": config}).encode("utf-8")

    proc = subprocess.Popen(  # noqa: S602 — shell=True trust model documented above
        manifest.command,
        shell=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=pack_path,
        env=env,
    )

    try:
        stdout_bytes, stderr_bytes = proc.communicate(input=stdin_bytes, timeout=manifest.timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            logger.warning("[task %s] process %d did not exit after kill+terminate", tid, proc.pid)
        raise StepTimeoutError(f"step '{manifest.name}' timed out after {manifest.timeout}s")

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if stderr:
        logger.debug("[task %s] step '%s' stderr: %s", tid, manifest.name, stderr)

    # Try to parse stdout as a valid StepResult
    result = None
    if stdout:
        try:
            data = json.loads(stdout)
            result = StepResult.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("[task %s] step '%s' unparseable output: %s", tid, manifest.name, exc)
            result = None

    if result is not None:
        logger.info("[task %s] step '%s' → %s", tid, manifest.name, result.status)
        return result

    # No valid StepResult parsed — synthesize a failure
    if proc.returncode != 0:
        output = f"step exited with code {proc.returncode}: {stderr[:500]}"
    else:
        output = f"invalid JSON output: {stdout[:200]}"

    logger.info("[task %s] step '%s' failed: %s", tid, manifest.name, output)
    return StepResult(status=StepStatus.FAILED, output=output)
