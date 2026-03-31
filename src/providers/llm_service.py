"""LLM service — implements `tao llm` command.


Reads JSON from stdin (prompt, model, tools, timeout, cwd, resume_session_id).
Calls ProviderPool to execute. Writes JSON result to stdout.
Reads TAO_TASK_ID/TAO_SUBTASK_INDEX/TAO_ROLE env vars for trace correlation.

Trace recording is decoupled via an optional ``on_trace`` callback, injected
by the caller (CLI/API layer). This module does NOT import Store — the DAG
forbids providers/* from importing store.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from typing import Any, TextIO

from src.models import ProviderError
from src.providers.pool import ProviderPool

logger = logging.getLogger(__name__)


def _write_error(output_stream: TextIO, error: str) -> None:
    """Write a JSON error response to the output stream."""
    json.dump({"success": False, "error": error, "output": ""}, output_stream)
    output_stream.write("\n")


def run_llm_service(
    pool: ProviderPool,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
    on_trace: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Run the LLM service: read JSON request from input, call pool, write response.

    Args:
        pool: ProviderPool for model routing and failover.
        input_stream: Where to read the JSON request (default: sys.stdin).
        output_stream: Where to write the JSON response (default: sys.stdout).
        on_trace: Optional callback for trace recording. Called with a trace
            dict when TAO_TASK_ID is set in the environment.

    Returns:
        Exit code: 0 = success, 1 = expected error, 2 = unexpected error.
    """
    if input_stream is None:
        input_stream = sys.stdin
    if output_stream is None:
        output_stream = sys.stdout

    # Read and parse input
    raw = input_stream.read()
    if not raw.strip():
        _write_error(output_stream, "empty input: expected JSON object")
        return 1

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        _write_error(output_stream, f"invalid JSON input: {exc}")
        return 1

    # Validate required fields
    prompt = request.get("prompt")
    if not prompt or not isinstance(prompt, str):
        _write_error(output_stream, "missing or empty required field: prompt")
        return 1

    model = request.get("model")
    if not model or not isinstance(model, str):
        _write_error(output_stream, "missing or empty required field: model")
        return 1

    # Extract optional fields with defaults
    tools = request.get("tools", [])
    timeout = request.get("timeout", 300)
    cwd = request.get("cwd", None)
    resume_session_id = request.get("resume_session_id", None)

    # Call provider pool
    try:
        response = pool.call(
            prompt=prompt,
            model=model,
            tools=tools,
            timeout=timeout,
            cwd=cwd,
            resume_session_id=resume_session_id,
        )
    except ProviderError as exc:
        _write_error(output_stream, str(exc))
        return 1
    except Exception as exc:
        logger.exception("unexpected error in LLM service")
        _write_error(output_stream, f"unexpected error: {exc}")
        return 2

    # Write response FIRST — the caller is waiting on stdout
    json.dump(response, output_stream)
    output_stream.write("\n")

    # Trace recording (after stdout response)
    task_id = os.environ.get("TAO_TASK_ID")
    if task_id and on_trace is not None:
        subtask_index_raw = os.environ.get("TAO_SUBTASK_INDEX", "0")
        try:
            subtask_index = int(subtask_index_raw)
        except ValueError:
            logger.warning(
                "TAO_SUBTASK_INDEX has non-integer value: %r, defaulting to 0",
                subtask_index_raw,
            )
            subtask_index = 0

        trace: dict[str, Any] = {
            "subtask_index": subtask_index,
            "role": os.environ.get("TAO_ROLE", ""),
            "model": model,
            "tokens_in": response.get("tokens_in", 0),
            "tokens_out": response.get("tokens_out", 0),
            "cost_usd": response.get("cost_usd", 0.0),
            "elapsed_s": response.get("elapsed_s", 0.0),
            "success": response.get("success", True),
            "attempt": 1,
        }
        try:
            on_trace(trace)
        except Exception:
            logger.warning("trace callback failed", exc_info=True)

    return 0
