"""Tests for tao.flow — orchestration loop with configurable cycles.

The flow calls the provider pool directly (no subprocess packs).
FakeProvider from conftest.py returns canned responses. Pass explicit
``responses`` lists to control per-call behaviour.
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from typing import Any

import pytest

from src.flow import request_stop, run_flow
from src.models import (
    FlowPolicies,
    HooksConfig,
    TaoError,
    TaskStatus,
    WorkspaceConfig,
)
from src.providers.pool import ProviderPool
from tests.conftest import FakeProvider

# --- Helpers ---

_PYTHON = sys.executable


def _ok_response(output: str = "ok") -> dict:
    """Default success response for FakeProvider."""
    return {
        "success": True,
        "output": output,
        "elapsed_s": 0.1,
        "cost_usd": 0.0,
        "tokens_in": 10,
        "tokens_out": 20,
        "session_id": "",
    }


def _scope_response(subtasks: list[dict]) -> dict:
    """Scope success response — output is a JSON array of subtasks."""
    return _ok_response(json.dumps(subtasks))


def _fail_response(error: str = "step failed") -> dict:
    """Failure response for FakeProvider."""
    return {
        "success": False,
        "error": error,
        "output": "",
        "elapsed_s": 0.1,
        "cost_usd": 0.0,
        "tokens_in": 10,
        "tokens_out": 20,
        "session_id": "",
    }


def _make_pool(responses: list[dict] | None = None) -> ProviderPool:
    """Create a ProviderPool with a FakeProvider using explicit responses."""
    provider = FakeProvider(responses=responses)
    return ProviderPool(
        providers=[provider],
        model_map={"opus": ["fake"], "sonnet": ["fake"]},
    )


_DEFAULT_CWD = object()


def _base_config(cwd: str | object = _DEFAULT_CWD, **overrides: Any) -> dict[str, Any]:
    """Return a minimal flow config dict with sensible defaults.

    Default: scope + two-step cycle (plan + implement).
    """
    if cwd is _DEFAULT_CWD:
        cwd = tempfile.gettempdir()
    cfg: dict[str, Any] = {
        "cwd": cwd,
        "workspace": WorkspaceConfig(),
        "hooks": HooksConfig(),
        "policies": FlowPolicies(),
        "scope": {"model_spec": "opus", "timeout": 300},
        "cycle": [
            {
                "id": "plan", "type": "llm",
                "prompt": "Plan this subtask.", "model_spec": "opus",
            },
            {
                "id": "implement", "type": "llm",
                "prompt": "Implement.", "model_spec": "sonnet",
            },
        ],
    }
    cfg.update(overrides)
    return cfg


def _one_shot_config(cwd: str | object = _DEFAULT_CWD, **overrides: Any) -> dict[str, Any]:
    """Return config for one-shot execution: no scope, single implement step."""
    if cwd is _DEFAULT_CWD:
        cwd = tempfile.gettempdir()
    cfg: dict[str, Any] = {
        "cwd": cwd,
        "workspace": WorkspaceConfig(),
        "hooks": HooksConfig(),
        "policies": FlowPolicies(),
        "cycle": [
            {
                "id": "implement", "type": "llm",
                "prompt": "Implement.", "model_spec": "sonnet",
            },
        ],
    }
    cfg.update(overrides)
    return cfg


# --- Tests ---


class TestFlowHappyPath:
    def test_flow_basic_happy_path(self, store, tmp_path):
        """scope → plan → implement → re-scope(empty) = COMPLETED."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),  # scope
            _ok_response("planned"),                                     # plan
            _ok_response("implemented"),                                 # implement
            _scope_response([]),                                         # re-scope → empty → done
        ])

        store.create_task(task_id=1, title="Test task", body="body")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)

        assert result == TaskStatus.COMPLETED
        task = store.get_task(1)
        assert task["status"] == "completed"

        # Verify traces: scope + plan + implement + re-scope = 4
        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "scope" in roles
        assert "plan" in roles
        assert "implement" in roles

    def test_flow_no_scope_single_subtask(self, store, mock_pool, tmp_path):
        """No scope → single subtask from task title/body."""
        store.create_task(task_id=1, title="Simple task", body="do something")
        config = _one_shot_config()

        result = run_flow(1, store=store, pool=mock_pool, config=config)

        assert result == TaskStatus.COMPLETED

    def test_flow_scope_empty_subtasks(self, store, tmp_path):
        """Scope returns empty subtasks → trivially COMPLETED."""
        pool = _make_pool([
            _scope_response([]),  # scope → empty → done immediately
        ])

        store.create_task(task_id=1, title="Empty scope", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED


class TestFlowSubtaskPersistence:
    def test_flow_saves_subtasks_after_scope(self, store, mock_pool, tmp_path):
        """Subtask list is persisted to store after scope completes."""
        store.create_task(task_id=1, title="Test task", body="body")
        config = _base_config()

        run_flow(1, store=store, pool=mock_pool, config=config)

        task = store.get_task(1)
        assert isinstance(task["subtasks"], list)
        assert len(task["subtasks"]) > 0
        assert "title" in task["subtasks"][0]

    def test_flow_saves_subtasks_no_scope(self, store, mock_pool, tmp_path):
        """Without scope, single subtask from task title/body is persisted."""
        store.create_task(task_id=1, title="Simple task", body="do something")
        config = _one_shot_config()

        run_flow(1, store=store, pool=mock_pool, config=config)

        task = store.get_task(1)
        assert len(task["subtasks"]) == 1
        assert task["subtasks"][0]["title"] == "Simple task"


class TestFlowWorkspace:
    def test_flow_workspace_mandatory(self, store, mock_pool, tmp_path):
        """Missing cwd → TaoError."""
        store.create_task(task_id=1, title="No workspace", body="")
        config = _base_config(cwd="")

        with pytest.raises(TaoError, match="workspace.*required"):
            run_flow(1, store=store, pool=mock_pool, config=config)

    def test_flow_cwd_not_a_directory(self, store, mock_pool, tmp_path):
        """cwd pointing to non-existent path → TaoError."""
        store.create_task(task_id=1, title="Bad cwd", body="")
        config = _base_config(cwd="/nonexistent/path/that/does/not/exist")

        with pytest.raises(TaoError, match="does not exist"):
            run_flow(1, store=store, pool=mock_pool, config=config)

    def test_flow_no_workspace_create(self, store, tmp_path):
        """Empty workspace config → flow works without workspace commands."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])

        store.create_task(task_id=1, title="No ws", body="")
        config = _base_config(workspace=WorkspaceConfig())

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

    def test_flow_workspace_create_reads_stdout(self, store, mock_pool, tmp_path):
        """Create command stdout becomes workspace_path in step context."""
        pack_dir = tmp_path / "pack"
        pack_dir.mkdir()

        ws_dir = tmp_path / "workspace"
        ws_dir.mkdir()

        create_script = pack_dir / "create_ws.py"
        create_script.write_text(f"print({repr(str(ws_dir))})")

        store.create_task(task_id=1, title="WS test", body="")
        config = _one_shot_config(
            workspace=WorkspaceConfig(create=f"{_PYTHON} {create_script}"),
        )

        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.COMPLETED

    def test_flow_workspace_create_failure(self, store, mock_pool, tmp_path):
        """Create fails (exit 1) → task FAILED."""
        pack_dir = tmp_path / "pack"
        pack_dir.mkdir()

        fail_script = pack_dir / "fail_create.py"
        fail_script.write_text("import sys; sys.exit(1)")

        store.create_task(task_id=1, title="Fail ws", body="")
        config = _base_config(
            workspace=WorkspaceConfig(create=f"{_PYTHON} {fail_script}"),
        )

        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.FAILED

    def test_flow_workspace_persist_nonfatal(self, store, mock_pool, tmp_path):
        """Persist fails → warning logged, flow continues."""
        pack_dir = tmp_path / "pack"
        pack_dir.mkdir()

        fail_script = pack_dir / "fail_persist.py"
        fail_script.write_text("import sys; sys.exit(1)")

        store.create_task(task_id=1, title="Persist fail", body="")
        config = _one_shot_config(
            workspace=WorkspaceConfig(persist=f"{_PYTHON} {fail_script}"),
        )

        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.COMPLETED

    def test_flow_deliver_failure(self, store, mock_pool, tmp_path):
        """Deliver fails (exit 1) → task FAILED."""
        pack_dir = tmp_path / "pack"
        pack_dir.mkdir()

        fail_script = pack_dir / "fail_deliver.py"
        fail_script.write_text("import sys; sys.exit(1)")

        store.create_task(task_id=1, title="Deliver fail", body="")
        config = _one_shot_config(
            workspace=WorkspaceConfig(deliver=f"{_PYTHON} {fail_script}"),
        )

        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.FAILED


class TestFlowFailures:
    def test_flow_step_failure_immediate(self, store, tmp_path):
        """LLM returns success=false on implement → FAILED immediately."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _fail_response("broken"),                                    # implement fails
        ])

        store.create_task(task_id=1, title="Fail task", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED

    def test_flow_iteration_limit_checkpoints(self, store, tmp_path):
        """max_iterations=0 → checkpoint (BLOCKED) on first iteration."""
        pool = _make_pool()

        store.create_task(task_id=1, title="Loop task", body="")
        config = _base_config(policies=FlowPolicies(max_iterations=0))

        result = run_flow(1, store=store, pool=pool, config=config)

        assert result == TaskStatus.BLOCKED
        task = store.get_task(1)
        assert task["status"] == "blocked"
        checkpoint = store.load_checkpoint(1)
        assert checkpoint is not None
        assert "iteration limit" in checkpoint.get("blocked_reason", "")


class TestFlowStop:
    def test_flow_stop_between_subtasks(self, store, mock_pool, tmp_path):
        """Stop event set → checkpoint saved, STOPPED."""
        store.create_task(task_id=42, title="Stoppable", body="")
        config = _one_shot_config()

        def _set_stop():
            import time
            time.sleep(0.3)
            request_stop(42)

        t = threading.Thread(target=_set_stop)
        t.start()

        result = run_flow(42, store=store, pool=mock_pool, config=config)
        t.join()

        assert result in (TaskStatus.STOPPED, TaskStatus.COMPLETED)

    def test_flow_request_stop_unit(self):
        """request_stop sets the event for a registered task_id."""
        from src.flow import _stop_events

        event = threading.Event()
        _stop_events[77] = event
        try:
            assert not event.is_set()
            request_stop(77)
            assert event.is_set()
        finally:
            _stop_events.pop(77, None)

    def test_flow_request_stop_noop_unknown(self):
        """request_stop on unknown task_id does nothing (no error)."""
        request_stop(999999)


class TestFlowRescope:
    def test_flow_rescope_empty_completes(self, store, tmp_path):
        """After batch completes, re-scope returns empty → task COMPLETED."""
        pool = _make_pool([
            _scope_response([{"title": "Step 1", "description": "first"}]),
            _ok_response("plan"),
            _ok_response("exec"),
            _scope_response([]),
        ])

        store.create_task(task_id=1, title="Rescope task", body="")
        config = _base_config(policies=FlowPolicies(max_iterations=5))

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        scope_traces = [t for t in traces if t["role"] == "scope"]
        assert len(scope_traces) == 2  # initial + re-scope

    def test_flow_scope_rescope_two_batches(self, store, tmp_path):
        """Scope → batch 1 → re-scope → batch 2 → re-scope empty → done."""
        provider = FakeProvider(responses=[
            _scope_response([{"title": "Step 1", "description": "first"}]),
            _ok_response("plan"),
            _ok_response("exec"),
            _scope_response([{"title": "Step 2", "description": "second"}]),
            _ok_response("plan"),
            _ok_response("exec"),
            _scope_response([]),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Multi-batch", body="needs 2 batches")
        config = _base_config(policies=FlowPolicies(batch_size=1, max_iterations=5))

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        scope_traces = [t for t in traces if t["role"] == "scope"]
        assert len(scope_traces) == 3  # initial + 2 re-scopes


class TestParseScopeFromLlm:
    """Unit tests for _parse_scope_from_llm."""

    def test_json_object_backward_compat(self):
        from src.flow import _parse_scope_from_llm
        text = '{"subtasks": [{"title": "A", "description": "a"}]}'
        subtasks = _parse_scope_from_llm(text)
        assert len(subtasks) == 1
        assert subtasks[0]["title"] == "A"

    def test_json_object_extra_keys_ignored(self):
        from src.flow import _parse_scope_from_llm
        text = '{"subtasks": [{"title": "A", "description": "a"}], "more_work": false}'
        subtasks = _parse_scope_from_llm(text)
        assert len(subtasks) == 1

    def test_plain_json_array(self):
        from src.flow import _parse_scope_from_llm
        text = '[{"title": "A", "description": "a"}, {"title": "B", "description": "b"}]'
        subtasks = _parse_scope_from_llm(text)
        assert len(subtasks) == 2

    def test_json_with_surrounding_text(self):
        from src.flow import _parse_scope_from_llm
        text = 'Sure:\n[{"title": "X", "description": "x"}]\n'
        subtasks = _parse_scope_from_llm(text)
        assert len(subtasks) == 1

    def test_json_object_in_surrounding_text(self):
        from src.flow import _parse_scope_from_llm
        text = (
            'Here is the result:\n'
            '{"subtasks": [{"title": "X", "description": "x"}]}'
            '\nDone.'
        )
        subtasks = _parse_scope_from_llm(text)
        assert len(subtasks) == 1

    def test_invalid_json_returns_empty(self):
        from src.flow import _parse_scope_from_llm
        subtasks = _parse_scope_from_llm("not json at all")
        assert subtasks == []


class TestFlowCheckpoint:
    def test_flow_checkpoint_resume(self, store, mock_pool, tmp_path):
        """Pre-populate checkpoint → flow resumes from correct subtask."""
        store.create_task(task_id=1, title="Resume task", body="")

        store.save_checkpoint(1, {
            "workspace_path": str(tmp_path),
            "completed_subtasks": [{"title": "sub0", "description": "d0"}],
            "pending_subtasks": [
                {"title": "sub0", "description": "d0"},
                {"title": "sub1", "description": "d1"},
            ],
            "task_context": {
                "completed_summaries": "",
                "iteration": 1,
            },
            "batch_number": 1,
            "subtask_context": {
                "subtask_index": 1,
                "step_index": 0,
                "last_llm_output": "",
            },
        })

        # No scope → has_scope=False, no re-scope after batch
        config = _one_shot_config()
        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        assert len(traces) == 1  # implement for sub1 only

    def test_flow_checkpoint_has_task_context(self, store, tmp_path):
        """Checkpoint uses task_context for task-level state."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _fail_response("implement crashed"),
        ])

        store.create_task(task_id=1, title="Ctx task", body="body text")
        config = _base_config()

        run_flow(1, store=store, pool=pool, config=config)

        checkpoint = store.load_checkpoint(1)
        assert checkpoint is not None
        task_ctx = checkpoint.get("task_context", {})
        assert "completed_summaries" in task_ctx
        assert "iteration" in task_ctx
        assert task_ctx["iteration"] == 1
        assert "context" not in checkpoint

    def test_flow_checkpoint_has_subtask_context(self, store, tmp_path):
        """Mid-subtask failure saves subtask_context with step_index."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _fail_response("implement crashed"),
        ])

        store.create_task(task_id=1, title="Sub ctx", body="")
        config = _base_config()

        run_flow(1, store=store, pool=pool, config=config)

        checkpoint = store.load_checkpoint(1)
        sub_ctx = checkpoint.get("subtask_context", {})
        assert sub_ctx["subtask_index"] == 0
        assert "step_index" in sub_ctx
        assert "last_llm_output" in sub_ctx


class TestFlowFailureRetry:
    """Tests for failure retry from checkpoint."""

    def test_flow_subtask_fail_saves_checkpoint(self, store, tmp_path):
        """Subtask failure saves checkpoint for retry."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _fail_response("implement crashed"),
        ])

        store.create_task(task_id=1, title="Fail task", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED

        checkpoint = store.load_checkpoint(1)
        assert checkpoint is not None
        assert "task_context" in checkpoint
        sub_ctx = checkpoint.get("subtask_context", {})
        assert sub_ctx.get("subtask_index") == 0

    def test_flow_scope_fail_saves_retry_scope(self, store, tmp_path):
        """Scope failure saves checkpoint with retry_scope flag."""
        pool = _make_pool([_fail_response("scope LLM error")])

        store.create_task(task_id=1, title="Scope fail", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED

        checkpoint = store.load_checkpoint(1)
        assert checkpoint is not None
        assert checkpoint.get("retry_scope") is True

    def test_flow_retry_scope_on_resume(self, store, tmp_path):
        """Unblock after scope failure → retries scope, then proceeds."""
        pool1 = _make_pool([_fail_response("scope error")])
        store.create_task(task_id=1, title="Retry scope", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool1, config=config)
        assert result == TaskStatus.FAILED

        store.update_task_status(1, TaskStatus.QUEUED)

        pool2 = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])

        result = run_flow(1, store=store, pool=pool2, config=config)
        assert result == TaskStatus.COMPLETED

    def test_flow_retry_subtask_on_resume(self, store, tmp_path):
        """Unblock after subtask failure → retries from checkpoint step."""
        pool1 = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _fail_response("implement error"),
        ])
        store.create_task(task_id=1, title="Retry subtask", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool1, config=config)
        assert result == TaskStatus.FAILED

        store.update_task_status(1, TaskStatus.QUEUED)

        # Resume: checkpoint has step_index from the failed step
        # The cycle replays from that step_index
        pool2 = _make_pool([
            _ok_response("planned v2"),         # plan (from resume step_index)
            _ok_response("implemented v2"),     # implement
            _scope_response([]),                # re-scope → done
        ])

        result = run_flow(1, store=store, pool=pool2, config=config)
        assert result == TaskStatus.COMPLETED


class TestFlowHumanMessage:
    """Tests for human message routing through scope."""

    def test_flow_human_message_in_scope_prompt(self, store, tmp_path):
        """Human message in task_context reaches scope prompt."""
        store.create_task(task_id=1, title="Scope msg", body="original task")
        store.save_checkpoint(1, {
            "workspace_path": str(tmp_path),
            "completed_subtasks": [],
            "pending_subtasks": [],
            "task_context": {
                "completed_summaries": "", "iteration": 1,
                "human_message": "use pandas",
            },
            "retry_scope": True,
        })

        provider = FakeProvider()
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        config = _base_config()
        run_flow(1, store=store, pool=pool, config=config)

        scope_call = provider.calls[0]
        assert "use pandas" in scope_call["prompt"]


class TestFlowFailover:
    """Tests for per-step failover."""

    def test_flow_failover_primary_succeeds(self, store, tmp_path):
        """Primary model works → failover not tried."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])

        store.create_task(task_id=1, title="Primary OK", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

    def test_flow_failover_primary_fails_fallback_succeeds(self, store, tmp_path):
        """Primary fails → fallback succeeds → task completes."""
        from tests.conftest import FailingProvider

        failing = FailingProvider()
        failing.name = "failing_provider"
        ok_provider = FakeProvider(responses=[
            _ok_response("implemented"),
            _scope_response([]),
        ])
        ok_provider.name = "ok_provider"

        pool = ProviderPool(
            providers=[failing, ok_provider],
            model_map={"opus": ["failing_provider"], "sonnet": ["ok_provider"]},
        )

        store.create_task(task_id=1, title="Failover task", body="")
        # One-shot with failover on implement step
        config = _one_shot_config(
            cycle=[{
                "id": "implement",
                "type": "llm",
                "prompt": "Implement.",
                "model_spec": "opus@failing_provider",
                "failover": ["sonnet@ok_provider"],
                "timeout": 600,
            }],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED
        assert len(ok_provider.calls) >= 1


class TestFlowNoSessionChaining:
    """Tests that session chaining is not used — each call is independent."""

    def test_flow_no_resume_session_id(self, store, tmp_path):
        """No call should have resume_session_id."""
        provider = FakeProvider(responses=[
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="No chain", body="")
        config = _base_config()

        run_flow(1, store=store, pool=pool, config=config)

        for call in provider.calls:
            assert call.get("resume_session_id") is None


# --- Cycle interpreter tests ---


class TestCycleLinear:
    """Tests for linear cycle execution (no jumps)."""

    def test_cycle_linear_two_llm_steps(self, store, tmp_path):
        """Plan + implement without jumps → COMPLETED."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])

        store.create_task(task_id=1, title="Linear", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "plan" in roles
        assert "implement" in roles

    def test_cycle_one_shot(self, store, mock_pool, tmp_path):
        """Cycle with a single step → COMPLETED."""
        store.create_task(task_id=1, title="One shot", body="do it")
        config = _one_shot_config()

        result = run_flow(1, store=store, pool=mock_pool, config=config)
        assert result == TaskStatus.COMPLETED

    def test_cycle_research_pattern(self, store, tmp_path):
        """Gather + write lineal → COMPLETED."""
        pool = _make_pool([
            _scope_response([{"title": "topic 1", "description": "research this"}]),
            _ok_response("findings..."),
            _ok_response("document written"),
            _scope_response([]),
        ])

        store.create_task(task_id=1, title="Research", body="")
        config = _base_config(
            cycle=[
                {
                    "id": "gather", "type": "llm",
                    "prompt": "Gather findings.", "model_spec": "opus",
                },
                {
                    "id": "write", "type": "llm",
                    "prompt": "Write document.", "model_spec": "sonnet",
                },
            ],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "gather" in roles
        assert "write" in roles

    def test_cycle_llm_failure_immediate(self, store, tmp_path):
        """LLM step fails → subtask fails immediately."""
        pool = _make_pool([
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _fail_response("plan failed"),     # plan fails
        ])

        store.create_task(task_id=1, title="Fail", body="")
        config = _base_config()

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED


class TestCycleCommandSteps:
    """Tests for command steps in the cycle."""

    def test_cycle_command_step_pass(self, store, tmp_path):
        """Command step passes → advance linearly."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        val_script = ws_dir / "validate.py"
        val_script.write_text("pass")  # exit 0

        pool = _make_pool([
            _ok_response("implemented"),
            # no scope → one-shot
        ])

        store.create_task(task_id=1, title="Cmd pass", body="")
        config = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "sonnet"},
                {"id": "validate", "type": "command", "commands": [f"{_PYTHON} {val_script}"]},
            ],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "implement" in roles
        assert "validate" in roles

    def test_cycle_command_on_fail_jump(self, store, tmp_path):
        """Command step fails with on_fail → jumps to fix step."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        # Alternating script: first call fails, second passes
        counter_file = ws_dir / "counter"
        counter_file.write_text("0")

        alt_script = ws_dir / "alt_val.py"
        alt_script.write_text(f"""
import sys
counter_path = {repr(str(counter_file))}
with open(counter_path) as f:
    n = int(f.read().strip())
with open(counter_path, 'w') as f:
    f.write(str(n + 1))
if n == 0:
    print("FAIL first time")
    sys.exit(1)
else:
    print("PASS second time")
    sys.exit(0)
""")

        store.create_task(task_id=1, title="Fix loop", body="")
        config2 = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "implement", "type": "llm", "prompt": "Implement.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix errors.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "validate", "type": "command",
                 "commands": [f"{_PYTHON} {alt_script}"],
                 "on_fail": "fix"},
            ],
        )

        provider2 = FakeProvider(responses=[
            _ok_response("implemented"),
            _ok_response("fixed"),
        ])
        pool2 = ProviderPool(
            providers=[provider2],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        result = run_flow(1, store=store, pool=pool2, config=config2)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "implement" in roles
        assert "fix" in roles
        assert "validate" in roles

    def test_cycle_command_fail_no_on_fail(self, store, tmp_path):
        """Command step fails without on_fail → subtask fails."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        fail_script = ws_dir / "fail_val.py"
        fail_script.write_text("import sys; sys.exit(1)")

        pool = _make_pool([_ok_response("implemented")])

        store.create_task(task_id=1, title="Cmd fail", body="")
        config = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "sonnet"},
                {"id": "validate", "type": "command",
                 "commands": [f"{_PYTHON} {fail_script}"]},
            ],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED


class TestCycleMaxRetries:
    """Tests for max_retries exhaustion."""

    def test_cycle_max_retries_exhausted(self, store, tmp_path):
        """Loop hits max_retries → subtask fails."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        fail_script = ws_dir / "always_fail.py"
        fail_script.write_text("import sys; print('always fails'); sys.exit(1)")

        # max_retries=2: implement → validate(fail) → fix →
        # validate(fail) → fix → validate(fail) → FAIL (3 jumps > 2)
        provider = FakeProvider(responses=[
            _ok_response("implemented"),     # implement
            _ok_response("fixed v1"),        # fix (after 1st validate fail)
            _ok_response("fixed v2"),        # fix (after 2nd validate fail) — won't reach here
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Max retries", body="")
        config = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "implement", "type": "llm", "prompt": "Implement.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "validate", "type": "command",
                 "commands": [f"{_PYTHON} {fail_script}"],
                 "on_fail": "fix"},
            ],
            max_retries=2,
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.FAILED


class TestCycleContextInjection:
    """Tests for prompt context injection rules."""

    def test_cycle_first_step_gets_description(self, store, tmp_path):
        """First cycle step receives subtask description in prompt."""
        provider = FakeProvider(responses=[
            _scope_response([{"title": "sub0", "description": "Build a widget"}]),
            _ok_response("planned"),
            _ok_response("implemented"),
            _scope_response([]),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Ctx test", body="")
        config = _base_config()

        run_flow(1, store=store, pool=pool, config=config)

        # First cycle call is plan (after scope)
        plan_call = provider.calls[1]  # [0]=scope, [1]=plan
        assert "Build a widget" in plan_call["prompt"]

    def test_cycle_subsequent_step_gets_previous_output(self, store, tmp_path):
        """Second cycle step receives previous LLM output in prompt."""
        provider = FakeProvider(responses=[
            _scope_response([{"title": "sub0", "description": "d0"}]),
            _ok_response("THE PLAN OUTPUT"),
            _ok_response("implemented"),
            _scope_response([]),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Ctx test 2", body="")
        config = _base_config()

        run_flow(1, store=store, pool=pool, config=config)

        # Second cycle call is implement
        implement_call = provider.calls[2]  # [0]=scope, [1]=plan, [2]=implement
        assert "THE PLAN OUTPUT" in implement_call["prompt"]

    def test_cycle_on_fail_step_gets_errors(self, store, tmp_path):
        """Fix step (reached via on_fail) gets validation errors in prompt."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        counter_file = ws_dir / "counter"
        counter_file.write_text("0")

        val_script = ws_dir / "val.py"
        val_script.write_text(f"""
import sys
counter_path = {repr(str(counter_file))}
with open(counter_path) as f:
    n = int(f.read().strip())
with open(counter_path, 'w') as f:
    f.write(str(n + 1))
if n == 0:
    print("ERROR: test_widget failed")
    sys.exit(1)
sys.exit(0)
""")

        provider = FakeProvider(responses=[
            _ok_response("implemented"),
            _ok_response("fixed"),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Error ctx", body="")
        config = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "implement", "type": "llm", "prompt": "Implement.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix the errors.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "validate", "type": "command",
                 "commands": [f"{_PYTHON} {val_script}"],
                 "on_fail": "fix"},
            ],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        # Fix call should contain the validation error
        fix_call = provider.calls[1]  # [0]=implement, [1]=fix
        assert "test_widget failed" in fix_call["prompt"]


class TestCycleDevPattern:
    """Test the full dev cycle pattern: plan → implement → validate → fix loop."""

    def test_cycle_dev_pattern(self, store, tmp_path):
        """Plan → implement → validate(pass) → COMPLETED."""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()

        pass_script = ws_dir / "pass_val.py"
        pass_script.write_text("print('all tests pass')")

        provider = FakeProvider(responses=[
            _ok_response("the plan"),
            _ok_response("implemented"),
        ])
        pool = ProviderPool(
            providers=[provider],
            model_map={"opus": ["fake"], "sonnet": ["fake"]},
        )

        store.create_task(task_id=1, title="Dev pattern", body="build feature X")
        config = _one_shot_config(
            cwd=str(ws_dir),
            cycle=[
                {"id": "plan", "type": "llm", "prompt": "Plan.",
                 "model_spec": "opus"},
                {"id": "implement", "type": "llm", "prompt": "Implement.",
                 "model_spec": "opus", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix.",
                 "model_spec": "sonnet", "next": "validate"},
                {"id": "validate", "type": "command",
                 "commands": [f"{_PYTHON} {pass_script}"],
                 "on_fail": "fix"},
            ],
        )

        result = run_flow(1, store=store, pool=pool, config=config)
        assert result == TaskStatus.COMPLETED

        traces = store.get_traces(1)
        roles = [t["role"] for t in traces]
        assert "plan" in roles
        assert "implement" in roles
        assert "validate" in roles
        assert "fix" not in roles  # validate passed, no fix needed
