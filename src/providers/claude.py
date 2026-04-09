"""Claude CLI provider — wraps ``claude`` CLI as an LLM provider.


Runs the ``claude`` binary via subprocess with ``--output-format json``.
No ``shell=True`` — the CLI is a known trusted binary invoked as a list of args.

Workspace setup:
  Claude CLI discovers its project root by walking up from cwd looking for
  ``.git`` or ``CLAUDE.md``. Without a ``CLAUDE.md`` in the cwd, it may resolve
  to a parent project and write files there. This provider creates a minimal
  ``CLAUDE.md`` in the cwd if one doesn't exist (logged as a warning).

Tool usage:
  Appends a system prompt telling Claude to use the Write tool for file
  creation. This is provider-level infrastructure (how to use tools), not
  task-level prescription (what to produce).

Permissions:
  Uses ``--dangerously-skip-permissions`` by default (all tools auto-approved).
  When ``tools`` list is specified, uses ``--allowedTools`` instead.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Any

from src.models import ProviderError

logger = logging.getLogger(__name__)


class ClaudeCliProvider:
    """LLM provider that wraps the ``claude`` CLI tool.

    Args:
        models: Optional mapping of model aliases to values passed to
                ``--model``. The CLI accepts both short aliases (``"opus"``,
                ``"sonnet"``, ``"haiku"``) and full model IDs
                (``"claude-opus-4-6"``). Using aliases is recommended —
                they always resolve to the latest version of each tier,
                so the config doesn't need updating when Anthropic
                releases new model versions. If a requested model is not
                in this map, it is passed through as-is.
        command: The CLI binary name/path. Defaults to ``"claude"``.
    """

    name: str = "claude"

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

    # Windows CreateProcess has a ~32760 char command-line limit.
    # When the prompt exceeds this threshold, pass it via stdin instead of -p.
    _MAX_ARG_LENGTH = 30000

    def _build_args(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        resume_session_id: str | None,
        cwd: str | None = None,
    ) -> tuple[list[str], bool]:
        """Build the CLI argument list.

        Returns:
            Tuple of (args, use_stdin). When use_stdin is True, the prompt
            must be piped via stdin instead of as a -p argument.
        """
        resolved = self._resolve_model(model)
        use_stdin = len(prompt) > self._MAX_ARG_LENGTH
        args = [
            self._command,
            "--model",
            resolved,
            "--output-format",
            "json",
            "-p",
            "-" if use_stdin else prompt,
        ]
        if tools:
            args.extend(["--allowedTools", ",".join(tools)])
        else:
            args.append("--dangerously-skip-permissions")
        if cwd:
            args.extend([
                "--append-system-prompt",
                f"Your workspace is {cwd}. All file paths should be relative to this directory. "
                f"When asked to create or write files, use the Write tool to save them to disk.",
            ])
        if resume_session_id:
            args.extend(["--resume", resume_session_id])
        return args, use_stdin

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
        """Execute a prompt via the claude CLI.

        Returns:
            Standard response dict with keys: success, output, elapsed_s,
            cost_usd, tokens_in, tokens_out, session_id.

        Raises:
            ProviderError: On timeout, non-zero exit, or unparseable output.
        """
        # Ensure cwd has a CLAUDE.md so Claude CLI recognizes it as a workspace.
        # Without it, Claude walks up to the nearest git root and uses that instead.
        if cwd and os.path.isdir(cwd) and not os.path.exists(os.path.join(cwd, "CLAUDE.md")):
            claude_md_path = os.path.join(cwd, "CLAUDE.md")
            with open(claude_md_path, "w", encoding="utf-8") as f:
                f.write("# Workspace\n\nThis is the working directory for this task.\n")
            logger.warning(
                "created CLAUDE.md in %s — Claude CLI requires it to recognize the workspace",
                cwd,
            )

        args, use_stdin = self._build_args(
            prompt,
            model=model,
            tools=tools,
            resume_session_id=resume_session_id,
            cwd=cwd,
        )
        logger.info("claude call: model=%s, tools=%s, cwd=%s, stdin=%s", model, tools, cwd, use_stdin)
        logger.debug("claude args: %s", args)

        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                input=prompt if use_stdin else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"claude CLI timed out after {timeout}s"
            raise ProviderError(msg) from exc

        elapsed = time.monotonic() - start

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "")[:500]
            msg = f"claude CLI exited with code {result.returncode}: {stderr_snippet}"
            raise ProviderError(msg)

        stdout = result.stdout or ""
        if not stdout.strip():
            stderr_snippet = (result.stderr or "")[:500]
            raise ProviderError(f"claude CLI returned empty output. stderr: {stderr_snippet}")

        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError as exc:
            msg = f"claude CLI returned invalid JSON: {exc}. stdout: {stdout[:300]}"
            raise ProviderError(msg) from exc

        usage = raw.get("usage", {})
        tokens_in = (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
        response = {
            "success": True,
            "output": raw.get("result", raw.get("output", "")),
            "elapsed_s": elapsed,
            "cost_usd": raw.get("total_cost_usd", 0.0),
            "tokens_in": tokens_in,
            "tokens_out": usage.get("output_tokens", 0),
            "session_id": raw.get("session_id", ""),
        }
        logger.info(
            "claude response: elapsed=%.1fs, cost=$%.4f, tokens=%d/%d",
            elapsed, response["cost_usd"], response["tokens_in"], response["tokens_out"],
        )
        return response
