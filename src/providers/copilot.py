"""Copilot CLI provider — wraps a copilot CLI as an LLM provider.


Runs the copilot binary via subprocess. The exact CLI interface is configurable
via the ``command`` parameter since the Copilot CLI is less established than Claude's.
No ``shell=True`` — the CLI is a known trusted binary invoked as a list of args.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

from src.models import ProviderError

logger = logging.getLogger(__name__)


class CopilotCliProvider:
    """LLM provider that wraps a copilot CLI tool.

    Args:
        models: Optional mapping of model aliases to actual model IDs
                (e.g. ``{"codex": "gpt-4"}``). If a requested model is not
                in this map, it is passed through as-is.
        command: The CLI binary name/path. Defaults to ``"copilot"``.
    """

    name: str = "copilot"

    def __init__(
        self,
        *,
        models: dict[str, str] | None = None,
        command: str = "copilot",
    ) -> None:
        self._models: dict[str, str] = dict(models or {})
        self._command: str = command

    def _resolve_model(self, model: str) -> str:
        """Resolve a model alias to an actual model ID."""
        return self._models.get(model, model)

    def _build_args(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        resume_session_id: str | None,
    ) -> list[str]:
        """Build the CLI argument list."""
        resolved = self._resolve_model(model)
        args = [
            self._command,
            "--model",
            resolved,
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
        if tools:
            args.extend(["--tools", ",".join(tools)])
        else:
            args.append("--dangerously-skip-permissions")
        if resume_session_id:
            logger.warning(
                "copilot provider: resume_session_id=%s provided but "
                "session resume may not be supported",
                resume_session_id,
            )
            args.extend(["--resume", resume_session_id])
        return args

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
        """Execute a prompt via the copilot CLI.

        Returns:
            Standard response dict with keys: success, output, elapsed_s,
            cost_usd, tokens_in, tokens_out, session_id.

        Raises:
            ProviderError: On timeout, non-zero exit, or unparseable output.
        """
        args = self._build_args(
            prompt,
            model=model,
            tools=tools,
            resume_session_id=resume_session_id,
        )
        logger.info("copilot call: model=%s, tools=%s", model, tools)
        logger.debug("copilot args: %s", args)

        start = time.monotonic()
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=cwd,
            )
        except subprocess.TimeoutExpired as exc:
            msg = f"copilot CLI timed out after {timeout}s"
            raise ProviderError(msg) from exc

        elapsed = time.monotonic() - start

        if result.returncode != 0:
            stderr_snippet = (result.stderr or "")[:500]
            msg = f"copilot CLI exited with code {result.returncode}: {stderr_snippet}"
            raise ProviderError(msg)

        try:
            raw = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            msg = f"copilot CLI returned invalid JSON: {exc}"
            raise ProviderError(msg) from exc

        response = {
            "success": True,
            "output": raw.get("result", raw.get("output", "")),
            "elapsed_s": elapsed,
            "cost_usd": raw.get("cost_usd", 0.0),
            "tokens_in": raw.get("tokens_in", 0),
            "tokens_out": raw.get("tokens_out", 0),
            "session_id": raw.get("session_id", ""),
        }
        logger.info(
            "copilot response: elapsed=%.1fs, cost=$%.4f, tokens=%d/%d",
            elapsed, response["cost_usd"], response["tokens_in"], response["tokens_out"],
        )
        return response
