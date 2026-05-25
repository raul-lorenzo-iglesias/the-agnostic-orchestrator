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


def test_gates_run_gate_command_default_timeout_at_least_step_default():
    """Default timeout must match (or exceed) CycleStep.timeout default (1800s).

    Regression guard: a too-tight default here silently clips long-running
    cycle command steps (full-gate verify, e2e suites, etc.) when callers
    forget to pass `timeout`. Aligns the default with `CycleStep.timeout`
    (models.py) so a step author who relies on the schema default still gets
    the full 30 min.
    """
    import inspect

    from src.models import CycleStep

    gate_default = inspect.signature(run_gate_command).parameters["timeout"].default
    step_default = CycleStep(id="_", type="command").timeout
    assert gate_default >= step_default, (
        f"run_gate_command default ({gate_default}s) must be >= CycleStep.timeout "
        f"default ({step_default}s); otherwise long verify steps get clipped silently."
    )
