"""Tests for tao.gates — command execution."""

from __future__ import annotations

import sys

from src.gates import run_gate_command

_PYTHON = sys.executable


def test_gates_run_gate_command_success(tmp_path):
    """Passing command returns (True, output)."""
    passed, output = run_gate_command(f"{_PYTHON} -c \"print('hello')\"", str(tmp_path))
    assert passed is True
    assert "hello" in output


def test_gates_run_gate_command_failure(tmp_path):
    """Failing command returns (False, output)."""
    passed, output = run_gate_command(
        f"{_PYTHON} -c \"import sys; print('fail'); sys.exit(1)\"",
        str(tmp_path),
    )
    assert passed is False
    assert "fail" in output


def test_gates_run_gate_command_timeout(tmp_path):
    """Command that exceeds timeout returns (False, 'timed out...')."""
    passed, output = run_gate_command(
        f'{_PYTHON} -c "import time; time.sleep(60)"',
        str(tmp_path),
        timeout=1,
    )
    assert passed is False
    assert "timed out" in output
