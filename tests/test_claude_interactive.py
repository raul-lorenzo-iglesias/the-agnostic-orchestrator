"""Tests for the claude_interactive provider.

The pty work lives in the out-of-process runner (tested in test_pty_runner.py), so here
``_run_turn`` is exercised by mocking ``subprocess.run`` — the provider just launches the
runner and parses its JSON verdict.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.models import LLMProvider, ProviderError
from src.providers.claude_interactive import ClaudeInteractiveProvider


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> MagicMock:
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestBuildAndWrap:
    def test_satisfies_protocol(self):
        assert isinstance(ClaudeInteractiveProvider(), LLMProvider)

    def test_build_argv_resolves_model_and_uses_bypass(self):
        p = ClaudeInteractiveProvider(models={"opus": "claude-opus-4-8"})
        argv = p._build_argv(session_id="sid-1", model="opus", tools=[])
        assert argv[0] == "claude"
        assert argv[argv.index("--session-id") + 1] == "sid-1"
        assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
        assert "--dangerously-skip-permissions" in argv
        assert "--allowedTools" not in argv

    def test_build_argv_passthrough_alias_and_allowed_tools(self):
        p = ClaudeInteractiveProvider()
        argv = p._build_argv(session_id="s", model="sonnet", tools=["Read", "Write"])
        assert argv[argv.index("--model") + 1] == "sonnet"  # unknown alias passes through
        assert "--allowedTools" in argv
        assert "Read,Write" in argv

    def test_custom_command(self):
        p = ClaudeInteractiveProvider(command="/opt/claude-beta")
        argv = p._build_argv(session_id="s", model="opus", tools=[])
        assert argv[0] == "/opt/claude-beta"

    def test_wrap_prompt_includes_task_and_absolute_paths(self):
        p = ClaudeInteractiveProvider()
        w = p._wrap_prompt("Do the thing.", out_path=r"C:\t\out.txt", done_path=r"C:\t\done.flag")
        assert "Do the thing." in w
        assert r"C:\t\out.txt" in w
        assert r"C:\t\done.flag" in w
        assert "OUTPUT PROTOCOL" in w


class TestCall:
    def test_call_success_returns_response_dict(self):
        p = ClaudeInteractiveProvider()
        with patch.object(ClaudeInteractiveProvider, "_run_turn", return_value="THE OUTPUT") as rt:
            resp = p.call("hi", model="opus", tools=[], timeout=60, cwd=None)
        rt.assert_called_once()
        assert resp["success"] is True
        assert resp["output"] == "THE OUTPUT"
        assert resp["cost_usd"] == 0.0
        assert resp["tokens_in"] == 0
        assert resp["tokens_out"] == 0
        assert resp["session_id"]  # non-empty uuid
        assert resp["elapsed_s"] >= 0

    def test_call_propagates_provider_error(self):
        p = ClaudeInteractiveProvider()
        with patch.object(
            ClaudeInteractiveProvider, "_run_turn", side_effect=ProviderError("timed out")
        ):
            with pytest.raises(ProviderError, match="timed out"):
                p.call("hi", model="opus", tools=[], timeout=1, cwd=None)

    def test_call_ignores_resume_session_id(self):
        p = ClaudeInteractiveProvider()
        with patch.object(ClaudeInteractiveProvider, "_run_turn", return_value="ok"):
            resp = p.call("hi", model="opus", tools=[], timeout=5, resume_session_id="sess_x")
        assert resp["output"] == "ok"

    def test_call_creates_claude_md_in_cwd(self, tmp_path):
        p = ClaudeInteractiveProvider()
        ws = tmp_path / "ws"
        ws.mkdir()
        with patch.object(ClaudeInteractiveProvider, "_run_turn", return_value="ok"):
            p.call("hi", model="opus", tools=[], timeout=5, cwd=str(ws))
        assert (ws / "CLAUDE.md").exists()


class TestRunTurn:
    """_run_turn launches the runner subprocess and parses its JSON verdict."""

    def _call_run_turn(self, p):
        return p._run_turn(
            ["claude"], cwd=None, wrapped_prompt="x",
            done_path="d", out_path="o", timeout=30,
        )

    def test_returns_runner_output_and_invokes_runner_script(self):
        p = ClaudeInteractiveProvider()
        fake = _completed('{"success": true, "output": "AGENT RESULT"}')
        with patch("src.providers.claude_interactive.subprocess.run", return_value=fake) as sr:
            out = self._call_run_turn(p)
        assert out == "AGENT RESULT"
        cmd = sr.call_args[0][0]
        assert cmd[0] == sys.executable
        assert cmd[1].endswith("_pty_runner.py")

    def test_runner_reports_failure_raises(self):
        p = ClaudeInteractiveProvider()
        fake = _completed('{"success": false, "error": "boom in runner"}')
        with patch("src.providers.claude_interactive.subprocess.run", return_value=fake):
            with pytest.raises(ProviderError, match="boom in runner"):
                self._call_run_turn(p)

    def test_invalid_json_raises(self):
        p = ClaudeInteractiveProvider()
        fake = _completed("not json at all")
        with patch("src.providers.claude_interactive.subprocess.run", return_value=fake):
            with pytest.raises(ProviderError, match="invalid JSON"):
                self._call_run_turn(p)

    def test_empty_output_raises(self):
        p = ClaudeInteractiveProvider()
        fake = _completed("", returncode=1, stderr="runner crashed")
        with patch("src.providers.claude_interactive.subprocess.run", return_value=fake):
            with pytest.raises(ProviderError, match="no output"):
                self._call_run_turn(p)

    def test_subprocess_timeout_raises(self):
        p = ClaudeInteractiveProvider()
        with patch(
            "src.providers.claude_interactive.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="runner", timeout=1),
        ):
            with pytest.raises(ProviderError, match="did not return"):
                self._call_run_turn(p)
