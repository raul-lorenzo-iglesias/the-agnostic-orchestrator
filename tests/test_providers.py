"""Tests for tao.providers — ProviderPool, ClaudeCliProvider, CopilotCliProvider."""

from __future__ import annotations

import json
import logging
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from src.models import LLMProvider, ProviderError
from src.providers.claude import ClaudeCliProvider
from src.providers.copilot import CopilotCliProvider
from src.providers.pool import ProviderPool

from tests.conftest import FailingProvider, FakeProvider
from tests.factories import create_provider_pool

# ============================================================
# ProviderPool tests
# ============================================================


class TestProviderPool:
    def test_pool_call_routes_to_correct_provider(self):
        """Two providers with different model maps — verify correct one called."""
        p1 = FakeProvider(responses=[{"success": True, "output": "from-p1"}])
        p1.name = "provider_one"
        p2 = FakeProvider(responses=[{"success": True, "output": "from-p2"}])
        p2.name = "provider_two"

        pool = ProviderPool(
            providers=[p1, p2],
            model_map={"opus": ["provider_one"], "sonnet": ["provider_two"]},
        )

        result = pool.call(prompt="test", model="opus", tools=[], timeout=30)
        assert result["output"] == "from-p1"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 0

        result = pool.call(prompt="test", model="sonnet", tools=[], timeout=30)
        assert result["output"] == "from-p2"
        assert len(p2.calls) == 1

    def test_pool_call_unknown_model_falls_back_to_any_provider(self):
        """Model not in model_map → tries all providers with alias pass-through."""
        p1 = FakeProvider(responses=[{"success": True, "output": "fallback-ok"}])
        pool = ProviderPool(providers=[p1], model_map={"opus": ["fake"]})
        result = pool.call(prompt="test", model="gpt-5", tools=[], timeout=30)
        assert result["output"] == "fallback-ok"

    def test_pool_call_no_providers_raises(self):
        pool = ProviderPool(providers=[], model_map={})
        with pytest.raises(ProviderError, match="no providers registered"):
            pool.call(prompt="test", model="opus", tools=[], timeout=30)

    def test_pool_failover_to_next_provider(self):
        """First provider raises, second succeeds."""
        failing = FailingProvider()
        good = FakeProvider(responses=[{"success": True, "output": "fallback"}])

        pool = ProviderPool(
            providers=[failing, good],
            model_map={"opus": ["failing", "fake"]},
        )

        result = pool.call(prompt="test", model="opus", tools=[], timeout=30)
        assert result["output"] == "fallback"
        assert len(good.calls) == 1

    def test_pool_all_providers_fail_raises(self):
        """All providers raise → ProviderError."""
        f1 = FailingProvider()
        f2 = FailingProvider()
        f2.name = "failing2"

        pool = ProviderPool(
            providers=[f1, f2],
            model_map={"opus": ["failing", "failing2"]},
        )

        with pytest.raises(ProviderError, match="all providers failed"):
            pool.call(prompt="test", model="opus", tools=[], timeout=30)

    def test_pool_register_adds_provider(self):
        """register() adds a provider and model bindings post-init."""
        pool = ProviderPool(providers=[], model_map={})
        provider = FakeProvider()
        pool.register(provider, models=["opus", "sonnet"])

        result = pool.call(prompt="hi", model="opus", tools=[], timeout=30)
        assert result["success"] is True
        assert len(provider.calls) == 1

    def test_pool_empty_raises(self):
        """No providers for a model → ProviderError."""
        pool = ProviderPool(providers=[], model_map={})
        with pytest.raises(ProviderError, match="no providers registered"):
            pool.call(prompt="test", model="opus", tools=[], timeout=30)

    def test_pool_call_passes_all_kwargs(self):
        """Verify all kwargs are forwarded to the provider."""
        provider = FakeProvider()
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"]},
        )

        pool.call(
            prompt="hello",
            model="opus",
            tools=["Read", "Write"],
            timeout=120,
            cwd="/tmp/ws",
            resume_session_id="sess_123",
        )

        assert len(provider.calls) == 1
        call = provider.calls[0]
        assert call["prompt"] == "hello"
        assert call["model"] == "opus"
        assert call["tools"] == ["Read", "Write"]
        assert call["timeout"] == 120
        assert call["cwd"] == "/tmp/ws"
        assert call["resume_session_id"] == "sess_123"

    def test_pool_failover_logs_warning(self, caplog):
        """Verify warning logged when a provider fails and we fall over."""
        failing = FailingProvider()
        good = FakeProvider()

        pool = ProviderPool(
            providers=[failing, good],
            model_map={"opus": ["failing", "fake"]},
        )

        with caplog.at_level(logging.WARNING, logger="src.providers.pool"):
            pool.call(prompt="test", model="opus", tools=[], timeout=30)

        assert any("failing" in r.message and "failed" in r.message for r in caplog.records)

    def test_pool_init_rejects_unknown_provider_in_model_map(self):
        """model_map references a provider not in the list → ProviderError."""
        with pytest.raises(ProviderError, match="unknown provider 'ghost'"):
            ProviderPool(
                providers=[FakeProvider()],
                model_map={"opus": ["ghost"]},
            )

    def test_pool_register_does_not_duplicate(self):
        """Registering the same provider twice for a model doesn't duplicate."""
        pool = ProviderPool(providers=[], model_map={})
        provider = FakeProvider()
        pool.register(provider, models=["opus"])
        pool.register(provider, models=["opus"])

        assert pool._model_map["opus"] == ["fake"]


# ============================================================
# ClaudeCliProvider tests
# ============================================================


class TestClaudeCliProvider:
    def test_claude_provider_satisfies_protocol(self):
        provider = ClaudeCliProvider()
        assert isinstance(provider, LLMProvider)

    @patch("src.providers.claude.subprocess.run")
    def test_claude_call_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "done", "session_id": "s1"}),
            stderr="",
        )

        provider = ClaudeCliProvider(models={"opus": "claude-opus-4-6"})
        result = provider.call(
            "analyze this",
            model="opus",
            tools=["Read", "Glob"],
            timeout=300,
            cwd="/workspace",
        )

        mock_run.assert_called_once()
        args = mock_run.call_args
        cmd = args[0][0]  # first positional arg is the command list
        assert cmd[0] == "claude"
        assert "--model" in cmd
        assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6"
        assert "--output-format" in cmd
        assert "json" in cmd
        assert "-p" in cmd
        assert "analyze this" in cmd
        assert "--allowedTools" in cmd
        assert "Read,Glob" in cmd
        assert args[1]["cwd"] == "/workspace"
        assert args[1]["timeout"] == 300
        assert result["success"] is True
        assert result["output"] == "done"
        assert result["session_id"] == "s1"

    @patch("src.providers.claude.subprocess.run")
    def test_claude_model_resolution(self, mock_run):
        """Alias resolved from self._models; unknown alias passed through."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        provider = ClaudeCliProvider(models={"opus": "claude-opus-4-6"})

        # Alias resolution
        provider.call("test", model="opus", tools=[], timeout=30)
        cmd = mock_run.call_args[0][0]
        assert "claude-opus-4-6" in cmd

        # Pass-through for unknown alias
        provider.call("test", model="claude-sonnet-4-6", tools=[], timeout=30)
        cmd = mock_run.call_args[0][0]
        assert "claude-sonnet-4-6" in cmd

    @patch("src.providers.claude.subprocess.run")
    def test_claude_timeout_raises_provider_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=30)

        provider = ClaudeCliProvider()
        with pytest.raises(ProviderError, match="timed out"):
            provider.call("test", model="opus", tools=[], timeout=30)

    @patch("src.providers.claude.subprocess.run")
    def test_claude_nonzero_exit_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="authentication failed",
        )

        provider = ClaudeCliProvider()
        with pytest.raises(ProviderError, match="exited with code 1"):
            provider.call("test", model="opus", tools=[], timeout=30)

    @patch("src.providers.claude.subprocess.run")
    def test_claude_invalid_json_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not json at all",
            stderr="",
        )

        provider = ClaudeCliProvider()
        with pytest.raises(ProviderError, match="invalid JSON"):
            provider.call("test", model="opus", tools=[], timeout=30)

    @patch("src.providers.claude.subprocess.run")
    def test_claude_resume_session_id(self, mock_run):
        """resume_session_id adds --resume flag."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "resumed"}),
            stderr="",
        )

        provider = ClaudeCliProvider()
        provider.call(
            "continue",
            model="opus",
            tools=[],
            timeout=30,
            resume_session_id="sess_abc",
        )

        cmd = mock_run.call_args[0][0]
        assert "--resume" in cmd
        assert "sess_abc" in cmd

    @patch("src.providers.claude.subprocess.run")
    def test_claude_custom_command(self, mock_run):
        """Custom command binary name."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        provider = ClaudeCliProvider(command="/usr/local/bin/claude-beta")
        provider.call("test", model="opus", tools=[], timeout=30)

        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/local/bin/claude-beta"

    @patch("src.providers.claude.subprocess.run")
    def test_claude_response_defaults(self, mock_run):
        """Missing fields in CLI output get safe defaults."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "minimal"}),
            stderr="",
        )

        provider = ClaudeCliProvider()
        result = provider.call("test", model="opus", tools=[], timeout=30)

        assert result["success"] is True
        assert result["output"] == "minimal"
        assert result["cost_usd"] == 0.0
        assert result["tokens_in"] == 0
        assert result["tokens_out"] == 0
        assert result["session_id"] == ""
        assert result["elapsed_s"] > 0  # at least some time passed


# ============================================================
# CopilotCliProvider tests
# ============================================================


class TestCopilotCliProvider:
    def test_copilot_provider_satisfies_protocol(self):
        provider = CopilotCliProvider()
        assert isinstance(provider, LLMProvider)

    @patch("src.providers.copilot.subprocess.run")
    def test_copilot_call_builds_correct_command(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "done"}),
            stderr="",
        )

        provider = CopilotCliProvider(
            models={"codex": "gpt-4"},
            command="gh-copilot",
        )
        result = provider.call(
            "fix this",
            model="codex",
            tools=["Read"],
            timeout=300,
        )

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh-copilot"
        assert cmd[cmd.index("--model") + 1] == "gpt-4"
        assert "--tools" in cmd
        assert result["success"] is True
        assert result["output"] == "done"

    @patch("src.providers.copilot.subprocess.run")
    def test_copilot_timeout_raises_provider_error(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="copilot", timeout=30)

        provider = CopilotCliProvider()
        with pytest.raises(ProviderError, match="timed out"):
            provider.call("test", model="codex", tools=[], timeout=30)

    @patch("src.providers.copilot.subprocess.run")
    def test_copilot_nonzero_exit_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=2,
            stdout="",
            stderr="not found",
        )

        provider = CopilotCliProvider()
        with pytest.raises(ProviderError, match="exited with code 2"):
            provider.call("test", model="codex", tools=[], timeout=30)

    @patch("src.providers.copilot.subprocess.run")
    def test_copilot_invalid_json_raises(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="broken",
            stderr="",
        )

        provider = CopilotCliProvider()
        with pytest.raises(ProviderError, match="invalid JSON"):
            provider.call("test", model="codex", tools=[], timeout=30)

    @patch("src.providers.copilot.subprocess.run")
    def test_copilot_resume_session_logs_warning(self, mock_run, caplog):
        """resume_session_id triggers a warning for Copilot."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({"result": "ok"}),
            stderr="",
        )

        provider = CopilotCliProvider()
        with caplog.at_level(logging.WARNING, logger="src.providers.copilot"):
            provider.call(
                "test",
                model="codex",
                tools=[],
                timeout=30,
                resume_session_id="sess_xyz",
            )

        assert any("resume" in r.message.lower() for r in caplog.records)


# ============================================================
# Factory tests
# ============================================================


class TestProviderFactory:
    def test_create_provider_pool_defaults(self):
        """Factory creates a working pool with FakeProvider defaults."""
        pool = create_provider_pool()
        result = pool.call(prompt="test", model="opus", tools=[], timeout=30)
        assert result["success"] is True

    def test_create_provider_pool_custom_providers(self):
        """Factory accepts custom providers."""
        p = FakeProvider(responses=[{"success": True, "output": "custom"}])
        pool = create_provider_pool(providers=[p])
        result = pool.call(prompt="test", model="opus", tools=[], timeout=30)
        assert result["output"] == "custom"


# ============================================================
# parse_model_spec tests
# ============================================================


class TestParseModelSpec:
    def test_parse_model_only(self):
        from src.providers.pool import parse_model_spec
        model, provider = parse_model_spec("opus")
        assert model == "opus"
        assert provider is None

    def test_parse_model_at_provider(self):
        from src.providers.pool import parse_model_spec
        model, provider = parse_model_spec("opus@claude_cli")
        assert model == "opus"
        assert provider == "claude_cli"

    def test_parse_with_spaces(self):
        from src.providers.pool import parse_model_spec
        model, provider = parse_model_spec(" opus @ claude_cli ")
        assert model == "opus"
        assert provider == "claude_cli"

    def test_parse_empty_string(self):
        from src.providers.pool import parse_model_spec
        model, provider = parse_model_spec("")
        assert model == ""
        assert provider is None


class TestPoolDirectProvider:
    def test_pool_call_with_provider_param(self):
        """Explicit provider param routes directly, skips model_map."""
        p1 = FakeProvider(responses=[{"success": True, "output": "from-p1"}])
        p1.name = "p1"
        pool = ProviderPool(providers=[p1], model_map={})

        result = pool.call(prompt="test", model="opus", tools=[], timeout=30, provider="p1")
        assert result["output"] == "from-p1"

    def test_pool_call_unknown_provider_raises(self):
        """Explicit provider that doesn't exist → ProviderError."""
        pool = ProviderPool(providers=[], model_map={})
        with pytest.raises(ProviderError, match="unknown provider"):
            pool.call(prompt="test", model="opus", tools=[], timeout=30, provider="nonexistent")
