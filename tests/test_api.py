"""Tests for tao.api — Engine class, load_config, _build_provider_pool."""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest
from src.api import Engine, _build_provider_pool, load_config
from src.models import StoreError, TaoError, TaskNotFoundError, TaskStatus

# ---------------------------------------------------------------------------
# TestLoadConfig
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_api_load_config_valid_toml(self, tmp_path):
        cfg = tmp_path / "engine.toml"
        cfg.write_text(
            "[engine]\nmax_concurrent = 3\n\n"
            '[providers.my_claude]\ntype = "claude_cli"\n'
            'models = {opus = "claude-opus-4-6"}\n'
        )
        result = load_config(str(cfg))
        assert result["engine"]["max_concurrent"] == 3
        assert "my_claude" in result["providers"]
        assert result["providers"]["my_claude"]["type"] == "claude_cli"

    def test_api_load_config_file_not_found(self):
        with pytest.raises(TaoError, match="config file not found"):
            load_config("/nonexistent/path/engine.toml")

    def test_api_load_config_invalid_toml(self, tmp_path):
        cfg = tmp_path / "bad.toml"
        cfg.write_text("this is not valid [[[toml")
        with pytest.raises(TaoError, match="invalid TOML"):
            load_config(str(cfg))

    def test_api_load_config_no_providers(self, tmp_path):
        cfg = tmp_path / "minimal.toml"
        cfg.write_text("[engine]\nmax_concurrent = 2\n")
        result = load_config(str(cfg))
        assert result["engine"]["max_concurrent"] == 2
        assert "providers" not in result


# ---------------------------------------------------------------------------
# TestBuildProviderPool
# ---------------------------------------------------------------------------


class TestBuildProviderPool:
    def test_api_build_pool_claude_provider(self):
        config = {
            "providers": {
                "my_claude": {
                    "type": "claude_cli",
                    "models": {"opus": "claude-opus-4-6"},
                }
            }
        }
        pool = _build_provider_pool(config)
        assert "my_claude" in pool._providers
        assert pool._model_map["opus"] == ["my_claude"]

    def test_api_build_pool_copilot_provider(self):
        config = {
            "providers": {
                "my_copilot": {
                    "type": "copilot_cli",
                    "models": {"codex": "gpt-4"},
                }
            }
        }
        pool = _build_provider_pool(config)
        assert "my_copilot" in pool._providers
        assert pool._model_map["codex"] == ["my_copilot"]

    def test_api_build_pool_multiple_providers(self):
        config = {
            "providers": {
                "claude_main": {
                    "type": "claude_cli",
                    "models": {"opus": "claude-opus-4-6", "sonnet": "claude-sonnet-4-6"},
                },
                "copilot_backup": {
                    "type": "copilot_cli",
                    "models": {"opus": "gpt-4"},
                },
            }
        }
        pool = _build_provider_pool(config)
        assert len(pool._providers) == 2
        # opus should route to both, with claude_main first
        assert pool._model_map["opus"] == ["claude_main", "copilot_backup"]
        # sonnet only routes to claude_main
        assert pool._model_map["sonnet"] == ["claude_main"]

    def test_api_build_pool_unknown_type_raises(self):
        config = {"providers": {"bad": {"type": "unknown_provider", "models": {}}}}
        with pytest.raises(TaoError, match="unknown provider type"):
            _build_provider_pool(config)

    def test_api_build_pool_empty_providers(self):
        pool = _build_provider_pool({})
        assert pool._providers == {}
        assert pool._model_map == {}

    def test_api_build_pool_no_providers_key(self):
        pool = _build_provider_pool({"engine": {"max_concurrent": 1}})
        assert pool._providers == {}


# ---------------------------------------------------------------------------
# TestEngineInit
# ---------------------------------------------------------------------------


class TestEngineInit:
    def test_api_engine_init_defaults(self, tmp_path):
        db_path = str(tmp_path / "engine.db")
        config = {"engine": {"db_path": db_path}}
        engine = Engine(config=config)
        try:
            assert engine._store is not None
            assert engine._pool is not None
            assert engine._queue is not None
        finally:
            engine.close()

    def test_api_engine_init_from_config_dict(self, tmp_path):
        db_path = str(tmp_path / "engine.db")
        config = {"engine": {"db_path": db_path, "max_concurrent": 3}}
        engine = Engine(config=config)
        try:
            assert engine._queue._max_concurrent == 3
        finally:
            engine.close()

    def test_api_engine_init_from_config_path(self, tmp_path):
        db_path = str(tmp_path / "engine.db").replace("\\", "/")
        cfg_file = tmp_path / "engine.toml"
        cfg_file.write_text(f'[engine]\ndb_path = "{db_path}"\nmax_concurrent = 7\n')
        engine = Engine(config_path=str(cfg_file))
        try:
            assert engine._queue._max_concurrent == 7
        finally:
            engine.close()

    def test_api_engine_close(self, tmp_path):
        db_path = str(tmp_path / "engine.db")
        engine = Engine(config={"engine": {"db_path": db_path}})
        engine.close()
        # Double close should not raise
        engine.close()

    def test_api_engine_context_manager(self, tmp_path):
        db_path = str(tmp_path / "engine.db")
        with Engine(config={"engine": {"db_path": db_path}}) as engine:
            assert engine._store is not None
        # After exit, store is closed (further ops would fail)


# ---------------------------------------------------------------------------
# TestEngineSubmit
# ---------------------------------------------------------------------------


class TestEngineSubmit:
    def test_api_engine_submit(self, engine):
        engine.submit(1, "Test task")
        task = engine.get_status(1)
        assert task["status"] == TaskStatus.QUEUED
        assert task["title"] == "Test task"

    def test_api_engine_submit_with_config(self, engine):
        cfg = {"step_configs": {"scope": {"model_spec": "opus"}}}
        engine.submit(2, "Configured task", config=cfg)
        task = engine.get_status(2)
        assert task["config"]["step_configs"]["scope"]["model_spec"] == "opus"

    def test_api_engine_submit_duplicate_raises(self, engine):
        engine.submit(1, "First")
        with pytest.raises(StoreError, match="already exists"):
            engine.submit(1, "Duplicate")


# ---------------------------------------------------------------------------
# TestEngineRunFlow
# ---------------------------------------------------------------------------


class TestEngineRunFlow:
    def test_api_engine_run_flow_delegates(self, engine):
        engine.submit(1, "Test")
        with patch("src.api._run_flow", return_value=TaskStatus.COMPLETED) as mock_rf:
            result = engine.run_flow(1)

        assert result == TaskStatus.COMPLETED
        mock_rf.assert_called_once()
        call_kwargs = mock_rf.call_args
        assert call_kwargs.args[0] == 1
        assert call_kwargs.kwargs["store"] is engine._store
        assert call_kwargs.kwargs["pool"] is engine._pool

    def test_api_engine_run_flow_delegates_to_flow(self, engine):
        engine.submit(1, "Test")
        with patch("src.api._run_flow", return_value=TaskStatus.COMPLETED) as mock_flow:
            result = engine.run_flow(1)
        # run_flow delegates to _run_flow which handles status transitions
        assert result == TaskStatus.COMPLETED
        mock_flow.assert_called_once()

    def test_api_engine_run_flow_task_not_found(self, engine):
        with pytest.raises(TaskNotFoundError):
            engine.run_flow(999)


# ---------------------------------------------------------------------------
# TestEngineUnblockStop
# ---------------------------------------------------------------------------


class TestEngineUnblockStop:
    def test_api_engine_unblock_delegates(self, engine):
        engine.submit(1, "Test")
        engine._store.update_task_status(1, TaskStatus.BLOCKED)
        engine.unblock(1, context={"answer": "yes"})
        task = engine.get_status(1)
        assert task["status"] == TaskStatus.QUEUED

    def test_api_engine_unblock_not_blocked_raises(self, engine):
        engine.submit(1, "Test")
        with pytest.raises(TaoError, match="cannot be unblocked"):
            engine.unblock(1)

    def test_api_engine_stop_not_found_raises(self, engine):
        from src.models import TaskNotFoundError
        with pytest.raises(TaskNotFoundError):
            engine.stop(999)


# ---------------------------------------------------------------------------
# TestEngineObservability
# ---------------------------------------------------------------------------


class TestEngineObservability:
    def test_api_engine_get_status(self, engine):
        engine.submit(1, "Test task", body="desc")
        status = engine.get_status(1)
        assert status["task_id"] == 1
        assert status["title"] == "Test task"
        assert status["body"] == "desc"
        assert status["status"] == TaskStatus.QUEUED

    def test_api_engine_get_traces(self, engine):
        engine.submit(1, "Test")
        engine._store.record_trace(
            1,
            {
                "subtask_index": 0,
                "role": "execute",
                "model": "opus",
                "tokens_in": 100,
                "tokens_out": 200,
                "cost_usd": 0.01,
                "elapsed_s": 5.0,
                "success": True,
                "attempt": 1,
            },
        )
        traces = engine.get_traces(1)
        assert len(traces) == 1
        assert traces[0]["model"] == "opus"
        assert traces[0]["success"] is True

    def test_api_engine_summary(self, engine):
        engine.submit(1, "Test")
        engine._store.record_trace(
            1,
            {
                "role": "execute",
                "cost_usd": 0.05,
                "tokens_in": 500,
                "tokens_out": 1000,
                "elapsed_s": 10.0,
                "success": True,
            },
        )
        engine._store.record_trace(
            1,
            {
                "role": "review",
                "cost_usd": 0.02,
                "tokens_in": 200,
                "tokens_out": 400,
                "elapsed_s": 3.0,
                "success": True,
            },
        )
        summary = engine.summary(1)
        assert summary["task_id"] == 1
        assert summary["total_cost_usd"] == pytest.approx(0.07)
        assert summary["total_tokens_in"] == 700
        assert summary["total_tokens_out"] == 1400
        assert summary["trace_count"] == 2

    def test_api_engine_list_tasks(self, engine):
        engine.submit(1, "Task 1")
        engine.submit(2, "Task 2")
        tasks = engine.list_tasks()
        assert len(tasks) == 2

    def test_api_engine_list_tasks_filtered(self, engine):
        engine.submit(1, "Task 1")
        engine.submit(2, "Task 2")
        engine._store.update_task_status(2, TaskStatus.RUNNING)
        queued = engine.list_tasks(status=TaskStatus.QUEUED)
        running = engine.list_tasks(status=TaskStatus.RUNNING)
        assert len(queued) == 1
        assert queued[0]["task_id"] == 1
        assert len(running) == 1
        assert running[0]["task_id"] == 2


# ---------------------------------------------------------------------------
# TestEngineServe
# ---------------------------------------------------------------------------


class TestEngineServe:
    def test_api_engine_serve_starts_queue(self, engine):
        started = threading.Event()
        original_start = engine._queue.start

        def patched_start():
            original_start()
            started.set()

        engine._queue.start = patched_start

        def run_serve():
            try:
                engine.serve()
            except KeyboardInterrupt:
                pass

        t = threading.Thread(target=run_serve, daemon=True)
        t.start()
        assert started.wait(timeout=5.0), "queue.start() was not called"
        # Clean up: shutdown the queue so the serve loop exits
        engine._queue.shutdown()
        t.join(timeout=5.0)
