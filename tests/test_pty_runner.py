"""Tests for the standalone pty runner (_pty_runner.py).

The ConPTY layer is mocked via ``PtyProcess`` so the startup-dialog navigation and the
sentinel/output file loop are testable without a real terminal.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.providers import _pty_runner


class _FakeAgentPty:
    """Simulates the agent: on prompt injection, writes the output + sentinel files."""

    def __init__(self, out_path: str, done_path: str, response: str):
        self.pid = 4242
        self._out = out_path
        self._done = done_path
        self._resp = response
        self.terminated = False

    def read(self, _n: int) -> str:
        return ""

    def write(self, data: str) -> int:
        if "OUTPUT PROTOCOL" in data:
            with open(self._out, "w", encoding="utf-8") as f:
                f.write(self._resp)
            with open(self._done, "w", encoding="utf-8") as f:
                f.write("")
        return len(data)

    def terminate(self, force: bool = False) -> None:
        self.terminated = True

    def close(self, force: bool = False) -> None:
        pass


class _FakeNavPty:
    """Records injected keys; clears the dialog once bypass is accepted."""

    def __init__(self, buf: list[str]):
        self.buf = buf
        self.keys: list[str] = []

    def write(self, data: str) -> int:
        self.keys.append(data)
        if data == "\r" and "\x1b[B" in self.keys:
            self.buf.clear()
            self.buf.append("  >  ? for shortcuts")
        return len(data)


def _spec(tmp_path, **over):
    spec = {
        "argv": ["claude"],
        "cwd": None,
        "wrapped_prompt": "task\n\n----- OUTPUT PROTOCOL -----\nwrite files",
        "done_path": str(tmp_path / "done.flag"),
        "out_path": str(tmp_path / "out.txt"),
        "timeout": 30,
        "startup_timeout": 5,
    }
    spec.update(over)
    return spec


class TestRun:
    def test_pywinpty_missing_returns_error(self, tmp_path):
        with patch("src.providers._pty_runner.PtyProcess", None):
            r = _pty_runner.run(_spec(tmp_path))
        assert r["success"] is False
        assert "pywinpty" in r["error"]

    @patch("src.providers._pty_runner.time.sleep", lambda *a, **k: None)
    def test_run_success_reads_output(self, tmp_path):
        spec = _spec(tmp_path)
        fake = _FakeAgentPty(spec["out_path"], spec["done_path"], "AGENT RESULT")
        with patch("src.providers._pty_runner.PtyProcess") as Pty, patch.object(
            _pty_runner, "_navigate_startup", return_value=None
        ), patch("src.providers._pty_runner.subprocess.run"):
            Pty.spawn.return_value = fake
            r = _pty_runner.run(spec)
        assert r["success"] is True
        assert r["output"] == "AGENT RESULT"
        assert fake.terminated is True

    @pytest.mark.slow
    def test_run_timeout_when_no_sentinel(self, tmp_path):
        fake = MagicMock()
        fake.read.return_value = ""
        fake.pid = 1
        with patch("src.providers._pty_runner.PtyProcess") as Pty, patch.object(
            _pty_runner, "_navigate_startup", return_value=None
        ), patch("src.providers._pty_runner.subprocess.run"):
            Pty.spawn.return_value = fake
            r = _pty_runner.run(_spec(tmp_path, timeout=1, startup_timeout=0))
        assert r["success"] is False
        assert "timed out" in r["error"]


class TestNavigateStartup:
    @patch("src.providers._pty_runner.time.sleep", lambda *a, **k: None)
    def test_navigates_bypass_then_detects_ready(self):
        buf = ["WARNING: Claude Code running in Bypass Permissions mode\n2. Yes, I accept"]
        proc = _FakeNavPty(buf)
        _pty_runner._navigate_startup(proc, buf, 30)
        assert "\x1b[B" in proc.keys  # Down arrow to move off "No, exit"
        assert "\r" in proc.keys  # Enter to accept
