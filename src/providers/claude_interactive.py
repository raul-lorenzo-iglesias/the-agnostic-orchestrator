"""Claude interactive provider — drives the ``claude`` TUI via a real ConPTY.

Why this exists:
    The default ``claude_cli`` provider invokes ``claude -p`` (headless print mode).
    Anthropic announced that ``claude -p`` and the Agent SDK may stop drawing from
    subscription rate limits and move to a separate credit pool. This provider is the
    fallback: it runs a genuine *interactive* Claude Code session (NOT ``-p``), which
    is not covered by that change, while still using the subscription. It is a
    "just in case" pivot — keep it wired and tested, activate it the day ``-p`` is cut.

How it works (validated empirically — see ``Citadel/companies/dev/tao/tao-siguiente.md``):
    A real interactive TUI needs a TTY, so the session runs inside a Windows
    pseudo-console (ConPTY, via ``pywinpty``). The pty is used ONLY as a launcher +
    keystroke injector — its screen is never scraped for output. Control and output
    flow through files the agent itself writes: a wrapped prompt asks the agent to write
    its full response to an output file and then create a sentinel file we poll for.

    The pty driving runs in a short-lived **subprocess** (``_pty_runner.py``), not in
    this process: pywinpty leaves a pump thread alive that would hang the long-running
    ``tao run`` host on exit. Isolating it in a subprocess that terminates cleanly avoids
    that (and contains pty crashes).

    Note: ``claude agents --json`` does not list pty-spawned sessions and no transcript
    ``.jsonl`` is persisted for them, so token/cost usage is unavailable here — the
    response reports ``cost_usd``/``tokens`` as 0.

Platform: Windows only (ConPTY). ``pywinpty`` is an optional dependency
(``pip install 'tao[interactive]'``); it is needed by the runner subprocess, not by this
module. ``call()`` surfaces a ``ProviderError`` if the runner reports it is unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

from src.models import ProviderError

logger = logging.getLogger(__name__)

# Absolute path to the standalone pty runner (executed by path, not imported).
_PTY_RUNNER = os.path.join(os.path.dirname(__file__), "_pty_runner.py")


class ClaudeInteractiveProvider:
    """LLM provider that drives an interactive ``claude`` session over a ConPTY.

    Args:
        models: Optional mapping of model aliases to values passed to ``--model``
                (e.g. ``{"opus": "opus"}``). Unknown aliases pass through as-is.
        command: The CLI binary name/path. Defaults to ``"claude"``.
    """

    name: str = "claude_interactive"

    # Wait budget for navigating the startup dialogs before injecting the prompt.
    _STARTUP_TIMEOUT_S = 30
    # Pty terminal size — wide enough to avoid odd wrapping in the TUI.
    _PTY_DIMENSIONS = (50, 120)

    def __init__(
        self,
        *,
        models: dict[str, str] | None = None,
        command: str = "claude",
    ) -> None:
        self._models: dict[str, str] = dict(models or {})
        self._command: str = command

    def _resolve_model(self, model: str) -> str:
        """Resolve a model alias to an actual model ID."""
        return self._models.get(model, model)

    def _build_argv(self, *, session_id: str, model: str, tools: list[str]) -> list[str]:
        """Build the ``claude`` interactive launch argv.

        Always uses ``--dangerously-skip-permissions`` so tool use does not block on
        permission prompts (the startup confirmation for this is auto-navigated). When
        ``tools`` is non-empty, also restricts the available toolset via ``--allowedTools``.
        """
        argv = [
            self._command,
            "--session-id",
            session_id,
            "--model",
            self._resolve_model(model),
            "--dangerously-skip-permissions",
        ]
        if tools:
            argv.extend(["--allowedTools", ",".join(tools)])
        return argv

    @staticmethod
    def _wrap_prompt(prompt: str, *, out_path: str, done_path: str) -> str:
        """Wrap the task prompt with output-file + sentinel-file instructions.

        The interactive TUI gives us no clean stdout channel, so the agent reports its
        result by writing it to a file and then creating a sentinel we can poll for.
        """
        return (
            f"{prompt}\n\n"
            "----- OUTPUT PROTOCOL (do this last, exactly) -----\n"
            "When you have finished the task above, write your COMPLETE final response "
            f"as plain text/markdown to this absolute path:\n{out_path}\n"
            "Then create an empty file at this absolute path to signal completion:\n"
            f"{done_path}\n"
            "Use the Write tool for both. Do not ask for confirmation. The sentinel file "
            "MUST be created only after the output file is fully written."
        )

    def call(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        timeout: int,
        cwd: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute a prompt via an interactive ``claude`` session.

        Returns:
            Standard response dict: success, output, elapsed_s, cost_usd, tokens_in,
            tokens_out, session_id. ``cost_usd``/``tokens_*`` are 0 (not observable in
            interactive mode).

        Raises:
            ProviderError: If pywinpty is unavailable, the turn times out, or no output
                file is produced.
        """
        # resume_session_id is intentionally ignored: spawn-per-call does not chain
        # sessions. TAO chains context through the prompt, not via session resume.
        if resume_session_id:
            logger.debug("claude_interactive ignores resume_session_id (spawn-per-call)")

        # Ensure cwd is recognized as a workspace (mirror of claude_cli behavior).
        if cwd and os.path.isdir(cwd) and not os.path.exists(os.path.join(cwd, "CLAUDE.md")):
            with open(os.path.join(cwd, "CLAUDE.md"), "w", encoding="utf-8") as f:
                f.write("# Workspace\n\nThis is the working directory for this task.\n")
            logger.warning("created CLAUDE.md in %s for workspace recognition", cwd)

        session_id = str(uuid.uuid4())
        # Sentinel + output files live in a private temp dir (keeps the workspace clean).
        signal_dir = tempfile.mkdtemp(prefix="tao_int_")
        out_path = os.path.join(signal_dir, "out.txt")
        done_path = os.path.join(signal_dir, "done.flag")
        argv = self._build_argv(session_id=session_id, model=model, tools=tools)
        wrapped = self._wrap_prompt(prompt, out_path=out_path, done_path=done_path)

        logger.info("claude_interactive call: model=%s, tools=%s, cwd=%s", model, tools, cwd)
        start = time.monotonic()
        try:
            output = self._run_turn(
                argv, cwd=cwd, wrapped_prompt=wrapped,
                done_path=done_path, out_path=out_path, timeout=timeout,
            )
        finally:
            shutil.rmtree(signal_dir, ignore_errors=True)

        elapsed = time.monotonic() - start
        logger.info("claude_interactive response: elapsed=%.1fs, chars=%d", elapsed, len(output))
        return {
            "success": True,
            "output": output,
            "elapsed_s": elapsed,
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "session_id": session_id,
        }

    def _run_turn(
        self,
        argv: list[str],
        *,
        cwd: str | None,
        wrapped_prompt: str,
        done_path: str,
        out_path: str,
        timeout: int,
    ) -> str:
        """Run one interactive turn out-of-process via the pty runner; return its output.

        The runner does all pywinpty work and exits, so no pty state leaks into this
        (long-lived) process. Communication is JSON over stdin/stdout.
        """
        spec = {
            "argv": argv,
            "cwd": cwd,
            "wrapped_prompt": wrapped_prompt,
            "done_path": done_path,
            "out_path": out_path,
            "timeout": timeout,
            "startup_timeout": self._STARTUP_TIMEOUT_S,
            "rows": self._PTY_DIMENSIONS[0],
            "cols": self._PTY_DIMENSIONS[1],
        }
        # Backstop timeout: the runner enforces `timeout` on the turn itself; allow extra
        # for startup + teardown so subprocess.run only fires if the runner itself wedges.
        sub_timeout = timeout + self._STARTUP_TIMEOUT_S + 60
        try:
            proc = subprocess.run(
                [sys.executable, _PTY_RUNNER],
                input=json.dumps(spec),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=sub_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(
                f"claude_interactive pty runner did not return within {sub_timeout}s"
            ) from exc

        if not (proc.stdout or "").strip():
            raise ProviderError(
                f"claude_interactive pty runner produced no output "
                f"(rc={proc.returncode}, stderr={(proc.stderr or '')[:300]})"
            )
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                f"claude_interactive pty runner returned invalid JSON: {proc.stdout[:300]}"
            ) from exc

        if not result.get("success"):
            raise ProviderError(f"claude_interactive: {result.get('error', 'unknown error')}")
        return result.get("output", "")
