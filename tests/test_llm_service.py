"""Tests for tao.providers.llm_service — stdin/stdout LLM bridge."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

from src.models import ProviderError
from src.providers.llm_service import run_llm_service
from src.providers.pool import ProviderPool

from tests.conftest import FakeProvider


def _make_pool(responses=None):
    """Create a ProviderPool with a FakeProvider."""
    provider = FakeProvider(responses=responses)
    pool = ProviderPool(
        providers=[provider],
        model_map={"opus": ["fake"], "sonnet": ["fake"]},
    )
    return pool, provider


def _run(pool, request_data=None, *, raw_input=None, on_trace=None):
    """Helper: run the service with StringIO streams, return (exit_code, response_dict)."""
    if raw_input is not None:
        in_stream = io.StringIO(raw_input)
    else:
        in_stream = io.StringIO(json.dumps(request_data))
    out_stream = io.StringIO()

    exit_code = run_llm_service(
        pool, input_stream=in_stream, output_stream=out_stream, on_trace=on_trace
    )

    out_stream.seek(0)
    output_text = out_stream.read().strip()
    response = json.loads(output_text) if output_text else {}
    return exit_code, response


class TestLlmService:
    def test_llm_service_happy_path(self):
        pool, provider = _make_pool()
        request = {"prompt": "hello", "model": "opus"}
        exit_code, response = _run(pool, request)

        assert exit_code == 0
        assert response["success"] is True
        assert response["output"] == "ok"
        assert len(provider.calls) == 1
        assert provider.calls[0]["prompt"] == "hello"
        assert provider.calls[0]["model"] == "opus"

    def test_llm_service_invalid_json_input(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, raw_input="not json {{{")

        assert exit_code == 1
        assert response["success"] is False
        assert "invalid JSON" in response["error"]

    def test_llm_service_empty_input(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, raw_input="")

        assert exit_code == 1
        assert response["success"] is False
        assert "empty input" in response["error"]

    def test_llm_service_missing_prompt(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, {"model": "opus"})

        assert exit_code == 1
        assert response["success"] is False
        assert "prompt" in response["error"]

    def test_llm_service_missing_model(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, {"prompt": "hello"})

        assert exit_code == 1
        assert response["success"] is False
        assert "model" in response["error"]

    def test_llm_service_empty_prompt(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, {"prompt": "", "model": "opus"})

        assert exit_code == 1
        assert response["success"] is False
        assert "prompt" in response["error"]

    def test_llm_service_empty_model(self):
        pool, _ = _make_pool()
        exit_code, response = _run(pool, {"prompt": "hello", "model": ""})

        assert exit_code == 1
        assert response["success"] is False
        assert "model" in response["error"]

    def test_llm_service_provider_error(self):
        pool, provider = _make_pool()
        provider.responses = None  # clear default behavior

        def raising_call(**kwargs):
            raise ProviderError("all providers down")

        pool.call = raising_call

        exit_code, response = _run(pool, {"prompt": "hello", "model": "opus"})

        assert exit_code == 1
        assert response["success"] is False
        assert "all providers down" in response["error"]

    def test_llm_service_unexpected_error(self):
        pool, _ = _make_pool()

        def raising_call(**kwargs):
            raise RuntimeError("segfault in provider")

        pool.call = raising_call

        exit_code, response = _run(pool, {"prompt": "hello", "model": "opus"})

        assert exit_code == 2
        assert response["success"] is False
        assert "unexpected error" in response["error"]
        assert "segfault" in response["error"]

    def test_llm_service_default_optional_fields(self):
        pool, provider = _make_pool()
        exit_code, _ = _run(pool, {"prompt": "hello", "model": "opus"})

        assert exit_code == 0
        call = provider.calls[0]
        assert call["tools"] == []
        assert call["timeout"] == 300
        assert call["cwd"] is None
        assert call["resume_session_id"] is None

    def test_llm_service_all_fields_forwarded(self):
        pool, provider = _make_pool()
        request = {
            "prompt": "do something",
            "model": "sonnet",
            "tools": ["Read", "Write"],
            "timeout": 600,
            "cwd": "/tmp/workspace",
            "resume_session_id": "sess-123",
        }
        exit_code, _ = _run(pool, request)

        assert exit_code == 0
        call = provider.calls[0]
        assert call["prompt"] == "do something"
        assert call["model"] == "sonnet"
        assert call["tools"] == ["Read", "Write"]
        assert call["timeout"] == 600
        assert call["cwd"] == "/tmp/workspace"
        assert call["resume_session_id"] == "sess-123"

    def test_llm_service_trace_callback_called(self, monkeypatch):
        monkeypatch.setenv("TAO_TASK_ID", "42")
        monkeypatch.setenv("TAO_SUBTASK_INDEX", "3")
        monkeypatch.setenv("TAO_ROLE", "execute")

        pool, _ = _make_pool()
        on_trace = MagicMock()

        exit_code, _ = _run(pool, {"prompt": "hello", "model": "opus"}, on_trace=on_trace)

        assert exit_code == 0
        on_trace.assert_called_once()
        trace = on_trace.call_args[0][0]
        assert trace["subtask_index"] == 3
        assert trace["role"] == "execute"
        assert trace["model"] == "opus"
        assert trace["success"] is True
        assert trace["attempt"] == 1
        assert "tokens_in" in trace
        assert "tokens_out" in trace

    def test_llm_service_trace_no_task_id(self, monkeypatch):
        monkeypatch.delenv("TAO_TASK_ID", raising=False)

        pool, _ = _make_pool()
        on_trace = MagicMock()

        exit_code, _ = _run(pool, {"prompt": "hello", "model": "opus"}, on_trace=on_trace)

        assert exit_code == 0
        on_trace.assert_not_called()

    def test_llm_service_trace_no_callback(self, monkeypatch):
        monkeypatch.setenv("TAO_TASK_ID", "42")

        pool, _ = _make_pool()
        # on_trace=None (default) — should not error
        exit_code, response = _run(pool, {"prompt": "hello", "model": "opus"})

        assert exit_code == 0
        assert response["success"] is True

    def test_llm_service_trace_failure_nonfatal(self, monkeypatch):
        monkeypatch.setenv("TAO_TASK_ID", "42")

        pool, _ = _make_pool()
        on_trace = MagicMock(side_effect=RuntimeError("DB is down"))

        exit_code, response = _run(pool, {"prompt": "hello", "model": "opus"}, on_trace=on_trace)

        # Response was still written, exit code is still 0
        assert exit_code == 0
        assert response["success"] is True
        on_trace.assert_called_once()

    def test_llm_service_extra_fields_ignored(self):
        pool, provider = _make_pool()
        request = {
            "prompt": "hello",
            "model": "opus",
            "unknown_field": "should be ignored",
            "another": 42,
        }
        exit_code, response = _run(pool, request)

        assert exit_code == 0
        assert response["success"] is True
        assert len(provider.calls) == 1
