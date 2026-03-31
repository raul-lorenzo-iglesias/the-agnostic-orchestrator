"""Tests for tao.step_runner — subprocess execution (escape hatch)."""

from __future__ import annotations

import stat
import sys
import textwrap

import pytest
from src.models import (
    StepManifest,
    StepStatus,
    StepTimeoutError,
    TaoError,
)
from src.step_runner import (
    format_template_cmd,
    run_step,
    validate_context,
)

from tests.factories import create_manifest


def _write_step_script(path, code: str) -> str:
    """Write a Python script to path and return the absolute path as a string."""
    path.write_text(textwrap.dedent(code), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# --- LLM-direct mode ---


def test_step_runner_is_llm_direct():
    """Empty command indicates LLM-direct mode."""
    m = create_manifest(command="")
    assert m.is_llm_direct

    m2 = create_manifest(command="python run.py")
    assert not m2.is_llm_direct


# --- Context validation tests ---


def test_step_runner_validate_context_all_present():
    m = create_manifest(needs=["a", "b"])
    validate_context(m, {"a": 1, "b": 2, "c": 3})  # extra keys OK


def test_step_runner_validate_context_missing_keys():
    m = create_manifest(needs=["a", "b", "c"])
    with pytest.raises(TaoError, match="missing context keys.*'b'.*'c'"):
        validate_context(m, {"a": 1})


# --- Template formatting tests ---


def test_step_runner_format_template_cmd_basic():
    result = format_template_cmd("{task_id}", {"task_id": "42"})
    # shlex.quote("42") returns "42" (no wrapping needed for safe strings)
    import shlex

    assert result == shlex.quote("42")


def test_step_runner_format_template_cmd_escapes_special():
    result = format_template_cmd("{val}", {"val": "hello world; rm -rf /"})
    # shlex.quote wraps in single quotes and escapes
    assert "hello world" in result
    assert result.startswith("'")


def test_step_runner_format_template_cmd_unknown_key():
    with pytest.raises(ValueError, match="unknown placeholder"):
        format_template_cmd("{missing}", {"other": "val"})


# --- run_step tests ---

# Each test writes a small Python script to tmp_path and runs it via run_step.
# The manifest command uses the current Python interpreter for cross-platform compat.

_PYTHON = sys.executable


def test_step_runner_run_step_success(tmp_path):
    """Step that writes valid StepResult JSON to stdout."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import json, sys
        data = json.load(sys.stdin)
        result = {
            "status": "succeeded",
            "output": "all good",
            "data": {"key": "value"},
            "blocked_reason": "",
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "elapsed_s": 1.5,
            "session_id": ""
        }
        json.dump(result, sys.stdout)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(manifest, ctx={"x": 1}, config={}, pack_path=str(tmp_path))

    assert result.status == StepStatus.SUCCEEDED
    assert result.output == "all good"
    assert result.data == {"key": "value"}
    assert result.elapsed_s == 1.5


def test_step_runner_run_step_receives_ctx_and_config(tmp_path):
    """Step receives ctx and config via stdin JSON."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import json, sys
        payload = json.load(sys.stdin)
        ctx = payload["ctx"]
        config = payload["config"]
        result = {
            "status": "succeeded",
            "output": "round-trip ok",
            "data": {"got_title": ctx["title"], "got_model": config["model_spec"]},
            "blocked_reason": "",
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "elapsed_s": 0.0,
            "session_id": ""
        }
        json.dump(result, sys.stdout)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(
        manifest,
        ctx={"title": "test task"},
        config={"model_spec": "opus"},
        pack_path=str(tmp_path),
    )

    assert result.status == StepStatus.SUCCEEDED
    assert result.data["got_title"] == "test task"
    assert result.data["got_model"] == "opus"


def test_step_runner_run_step_invalid_json_output(tmp_path):
    """Step writes non-JSON to stdout → FAILED with message."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        print("not json at all")
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(manifest, ctx={}, config={}, pack_path=str(tmp_path))

    assert result.status == StepStatus.FAILED
    assert "invalid JSON" in result.output


def test_step_runner_run_step_timeout(tmp_path):
    """Step that sleeps beyond timeout raises StepTimeoutError."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import time
        time.sleep(60)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=1)

    with pytest.raises(StepTimeoutError, match="timed out"):
        run_step(manifest, ctx={}, config={}, pack_path=str(tmp_path))


def test_step_runner_run_step_env_extras(tmp_path):
    """env_extras are available to the step subprocess."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import json, os, sys
        result = {
            "status": "succeeded",
            "output": "ok",
            "data": {
                "task_id": os.environ.get("TAO_TASK_ID", ""),
                "role": os.environ.get("TAO_ROLE", ""),
            },
            "blocked_reason": "",
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "elapsed_s": 0.0,
            "session_id": ""
        }
        json.dump(result, sys.stdout)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(
        manifest,
        ctx={},
        config={},
        pack_path=str(tmp_path),
        env_extras={"TAO_TASK_ID": "42", "TAO_ROLE": "execute"},
    )

    assert result.status == StepStatus.SUCCEEDED
    assert result.data["task_id"] == "42"
    assert result.data["role"] == "execute"


def test_step_runner_run_step_nonzero_exit_no_output(tmp_path):
    """Step exits non-zero with no stdout → FAILED with exit code."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import sys
        sys.exit(1)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(manifest, ctx={}, config={}, pack_path=str(tmp_path))

    assert result.status == StepStatus.FAILED
    assert "exited with code 1" in result.output


def test_step_runner_run_step_nonzero_exit_with_result(tmp_path):
    """Step writes valid StepResult then exits non-zero → parsed result is used."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import json, sys
        result = {
            "status": "succeeded",
            "output": "did the work before crashing",
            "data": {"partial": True},
            "blocked_reason": "",
            "cost_usd": 0.01,
            "tokens_in": 100,
            "tokens_out": 200,
            "elapsed_s": 2.0,
            "session_id": ""
        }
        json.dump(result, sys.stdout)
        sys.stdout.flush()
        sys.exit(1)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(manifest, ctx={}, config={}, pack_path=str(tmp_path))

    # The parsed StepResult is returned, not a synthetic failure
    assert result.status == StepStatus.SUCCEEDED
    assert result.output == "did the work before crashing"
    assert result.data == {"partial": True}


def test_step_runner_run_step_stderr_captured(tmp_path):
    """stderr doesn't affect result parsing — only logged."""
    script = _write_step_script(
        tmp_path / "step.py",
        """\
        import json, sys
        print("debug info here", file=sys.stderr)
        result = {
            "status": "succeeded",
            "output": "ok",
            "data": {},
            "blocked_reason": "",
            "cost_usd": 0.0,
            "tokens_in": 0,
            "tokens_out": 0,
            "elapsed_s": 0.0,
            "session_id": ""
        }
        json.dump(result, sys.stdout)
        """,
    )
    manifest = create_manifest(command=f"{_PYTHON} {script}", timeout=30)
    result = run_step(manifest, ctx={}, config={}, pack_path=str(tmp_path))

    assert result.status == StepStatus.SUCCEEDED
    assert result.output == "ok"
