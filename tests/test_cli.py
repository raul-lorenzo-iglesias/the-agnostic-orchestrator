"""Tests for tao.cli — argparse CLI with 8 subcommands."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.cli import main
from src.models import TaskStatus
from src.store import Store


@pytest.fixture
def cli_config(tmp_path):
    """Create a temp TOML config file and return (config_path, db_path)."""
    db_path = str(tmp_path / "cli_test.db").replace("\\", "/")
    cfg = tmp_path / "src.toml"
    cfg.write_text(f'[engine]\ndb_path = "{db_path}"\n')
    return str(cfg), db_path


# ---------------------------------------------------------------------------
# TestCliParser
# ---------------------------------------------------------------------------


class TestCliParser:
    def test_cli_no_args_shows_help(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "usage" in captured.err.lower() or "tao" in captured.err.lower()

    def test_cli_unknown_command_exits(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["badcmd"])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# TestCliSubmit
# ---------------------------------------------------------------------------


class TestCliSubmit:
    def test_cli_submit_creates_task(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        main(
            [
                "--config",
                cfg_path,
                "submit",
                "--task-id",
                "1",
                "--title",
                "Test task",
                "--pack",
                "/tmp/p",
            ]
        )
        captured = capsys.readouterr()
        assert "submitted task 1" in captured.out

        # Verify task exists in store
        store = Store(db_path)
        try:
            task = store.get_task(1)
            assert task["title"] == "Test task"
            assert task["status"] == TaskStatus.QUEUED
        finally:
            store.close()

    def test_cli_submit_with_task_config(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        main(
            [
                "--config",
                cfg_path,
                "submit",
                "--task-id",
                "2",
                "--title",
                "Configured",
                "--pack",
                "/tmp/p",
                "--task-config",
                '{"key": "val"}',
            ]
        )

        store = Store(db_path)
        try:
            task = store.get_task(2)
            assert task["config"]["key"] == "val"
        finally:
            store.close()

    def test_cli_submit_invalid_json_config(self, cli_config, capsys):
        cfg_path, _ = cli_config
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--config",
                    cfg_path,
                    "submit",
                    "--task-id",
                    "3",
                    "--title",
                    "Bad config",
                    "--pack",
                    "/tmp/p",
                    "--task-config",
                    "not json",
                ]
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "invalid JSON" in captured.err

    def test_cli_submit_auto_id(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        main(
            ["--config", cfg_path, "submit", "--title", "Auto ID", "--pack", "/tmp/p"]
        )
        captured = capsys.readouterr()
        assert "submitted task" in captured.out

        store = Store(db_path)
        try:
            tasks = store.list_tasks()
            assert len(tasks) == 1
            assert tasks[0]["title"] == "Auto ID"
        finally:
            store.close()

    def test_cli_submit_duplicate_exits_1(self, cli_config, capsys):
        cfg_path, _ = cli_config
        main(
            [
                "--config",
                cfg_path,
                "submit",
                "--task-id",
                "1",
                "--title",
                "First",
                "--pack",
                "/tmp/p",
            ]
        )
        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--config",
                    cfg_path,
                    "submit",
                    "--task-id",
                    "1",
                    "--title",
                    "Duplicate",
                    "--pack",
                    "/tmp/p",
                ]
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "already exists" in captured.err


# ---------------------------------------------------------------------------
# TestCliRun
# ---------------------------------------------------------------------------


class TestCliRun:
    def test_cli_run_creates_task_from_json(self, cli_config, tmp_path, capsys):
        cfg_path, db_path = cli_config
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "title": "JSON task",
            "body": "Do something",
            "cwd": str(tmp_path),
            "step_configs": {"execute": {"model_spec": "sonnet"}},
        }))

        # Patch engine.serve to avoid blocking
        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            mock_engine.submit.return_value = 1
            main(["--config", cfg_path, "run", str(task_file)])

        captured = capsys.readouterr()
        assert "submitted task 1" in captured.out
        mock_engine.submit.assert_called_once_with(
            None, "JSON task", "Do something",
            config={
                "cwd": str(tmp_path),
                "step_configs": {"execute": {"model_spec": "sonnet"}},
            },
        )
        mock_engine.serve.assert_called_once()

    def test_cli_run_with_task_id(self, cli_config, tmp_path, capsys):
        cfg_path, _ = cli_config
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "task_id": 42,
            "title": "Explicit ID",
            "body": "",
            "cwd": "/tmp",
            "step_configs": {"execute": {"model_spec": "sonnet"}},
        }))

        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            mock_engine.submit.return_value = 42
            main(["--config", cfg_path, "run", str(task_file)])

        mock_engine.submit.assert_called_once_with(
            42, "Explicit ID", "",
            config={
                "cwd": "/tmp",
                "step_configs": {"execute": {"model_spec": "sonnet"}},
            },
        )

    def test_cli_run_body_file(self, cli_config, tmp_path, capsys):
        cfg_path, _ = cli_config
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Research about SEO.\n\nBlock 1: fundamentals\nBlock 2: audit")
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "title": "Research task",
            "body_file": "prompt.md",
            "cwd": str(tmp_path),
            "step_configs": {"execute": {"model_spec": "opus"}},
        }))

        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            mock_engine.submit.return_value = 1
            main(["--config", cfg_path, "run", str(task_file)])

        call_args = mock_engine.submit.call_args
        assert call_args[0][1] == "Research task"
        assert "Block 1: fundamentals" in call_args[0][2]
        assert "Block 2: audit" in call_args[0][2]

    def test_cli_run_body_file_not_found(self, cli_config, tmp_path, capsys):
        cfg_path, _ = cli_config
        task_file = tmp_path / "task.json"
        task_file.write_text(json.dumps({
            "title": "Bad ref",
            "body_file": "nonexistent.md",
            "cwd": str(tmp_path),
            "step_configs": {"execute": {"model_spec": "opus"}},
        }))
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "run", str(task_file)])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "body_file not found" in captured.err

    def test_cli_run_multiple_files(self, cli_config, tmp_path, capsys):
        cfg_path, _ = cli_config
        files = []
        for i in range(3):
            f = tmp_path / f"task{i}.json"
            f.write_text(json.dumps({
                "title": f"Task {i}",
                "body": f"Do thing {i}",
                "cwd": str(tmp_path),
                "step_configs": {"execute": {"model_spec": "sonnet"}},
            }))
            files.append(str(f))

        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            mock_engine.submit.side_effect = [10, 11, 12]
            main(["--config", cfg_path, "run"] + files)

        assert mock_engine.submit.call_count == 3
        captured = capsys.readouterr()
        assert "submitted task 10" in captured.out
        assert "submitted task 11" in captured.out
        assert "submitted task 12" in captured.out
        mock_engine.serve.assert_called_once()

    def test_cli_run_file_not_found(self, cli_config, capsys):
        cfg_path, _ = cli_config
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "run", "/nonexistent/task.json"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "file not found" in captured.err

    def test_cli_run_invalid_json(self, cli_config, tmp_path, capsys):
        cfg_path, _ = cli_config
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("not json {{{")
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "run", str(bad_file)])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "invalid JSON" in captured.err


# ---------------------------------------------------------------------------
# TestCliStatus
# ---------------------------------------------------------------------------


class TestCliStatus:
    def test_cli_status_outputs_json(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        # Create task directly via store
        store = Store(db_path)
        store.create_task(1, "Test task", body="desc")
        store.close()

        main(["--config", cfg_path, "--json", "status", "1"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["task_id"] == 1
        assert data["title"] == "Test task"
        assert data["status"] == "queued"

    def test_cli_status_not_found_exits_1(self, cli_config, capsys):
        cfg_path, _ = cli_config
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "status", "999"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_cli_status_detail_human(self, cli_config, capsys):
        """status <id> without --json produces human-readable card."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test task")
        store.close()

        main(["--config", cfg_path, "status", "1"])
        captured = capsys.readouterr()
        assert "Task #1" in captured.out
        assert "Test task" in captured.out
        assert "queued" in captured.out

    def test_cli_status_list_human(self, cli_config, capsys):
        """status without task_id produces human-readable list."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "First task")
        store.create_task(2, "Second task")
        store.close()

        main(["--config", cfg_path, "status"])
        captured = capsys.readouterr()
        assert "#1" in captured.out
        assert "#2" in captured.out
        assert "First task" in captured.out
        assert "Second task" in captured.out
        assert "2 tasks:" in captured.out

    def test_cli_status_list_empty(self, cli_config, capsys):
        """status with no tasks shows empty-state message."""
        cfg_path, _ = cli_config
        main(["--config", cfg_path, "status"])
        captured = capsys.readouterr()
        assert "No tasks" in captured.out

    def test_cli_status_list_json(self, cli_config, capsys):
        """--json status (no task_id) returns JSON array."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "First")
        store.create_task(2, "Second")
        store.close()

        main(["--config", cfg_path, "--json", "status"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_cli_status_list_filter(self, cli_config, capsys):
        """--filter limits which statuses appear."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Queued")
        store.create_task(2, "Blocked")
        store.update_task_status(2, TaskStatus.BLOCKED)
        store.close()

        main(["--config", cfg_path, "status", "--filter", "blocked"])
        captured = capsys.readouterr()
        assert "#2" in captured.out
        assert "#1" not in captured.out


# ---------------------------------------------------------------------------
# TestCliTraces
# ---------------------------------------------------------------------------


class TestCliTraces:
    def test_cli_traces_outputs_json(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.record_trace(
            1,
            {
                "role": "execute",
                "model": "opus",
                "tokens_in": 100,
                "tokens_out": 200,
                "cost_usd": 0.01,
                "elapsed_s": 5.0,
                "success": True,
            },
        )
        store.close()

        main(["--config", cfg_path, "--json", "traces", "1"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["model"] == "opus"

    def test_cli_traces_empty_list(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.close()

        main(["--config", cfg_path, "--json", "traces", "1"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == []

    def test_cli_traces_human(self, cli_config, capsys):
        """traces without --json produces a table."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.record_trace(
            1,
            {
                "role": "execute",
                "model": "opus",
                "tokens_in": 100,
                "tokens_out": 200,
                "cost_usd": 0.01,
                "elapsed_s": 5.0,
                "success": True,
            },
        )
        store.close()

        main(["--config", cfg_path, "traces", "1"])
        captured = capsys.readouterr()
        assert "ROLE" in captured.out
        assert "execute" in captured.out
        assert "opus" in captured.out

    def test_cli_traces_empty_human(self, cli_config, capsys):
        """Empty traces shows message, not empty JSON."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.close()

        main(["--config", cfg_path, "traces", "1"])
        captured = capsys.readouterr()
        assert "No traces" in captured.out


# ---------------------------------------------------------------------------
# TestCliSummary
# ---------------------------------------------------------------------------


class TestCliSummary:
    def test_cli_summary_outputs_json(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.record_trace(
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
        store.record_trace(
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
        store.close()

        main(["--config", cfg_path, "--json", "summary", "1"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["task_id"] == 1
        assert data["trace_count"] == 2
        assert data["total_tokens_in"] == 700

    def test_cli_summary_human(self, cli_config, capsys):
        """summary without --json produces a card."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.record_trace(
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
        store.close()

        main(["--config", cfg_path, "summary", "1"])
        captured = capsys.readouterr()
        assert "Summary" in captured.out
        assert "Task #1" in captured.out
        assert "$0.05" in captured.out


# ---------------------------------------------------------------------------
# TestCliUnblock
# ---------------------------------------------------------------------------


class TestCliUnblock:
    def test_cli_unblock_moves_to_queued(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.update_task_status(1, TaskStatus.BLOCKED)
        store.close()

        main(["--config", cfg_path, "unblock", "1"])
        captured = capsys.readouterr()
        assert "unblocked task 1" in captured.out

        store = Store(db_path)
        try:
            task = store.get_task(1)
            assert task["status"] == TaskStatus.QUEUED
        finally:
            store.close()

    def test_cli_unblock_with_context(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.update_task_status(1, TaskStatus.BLOCKED)
        store.save_checkpoint(1, {"context": {"old": "data"}})
        store.close()

        main(
            [
                "--config",
                cfg_path,
                "unblock",
                "1",
                "--context",
                '{"answer": "yes"}',
            ]
        )

        store = Store(db_path)
        try:
            task = store.get_task(1)
            assert task["status"] == TaskStatus.QUEUED
            cp = store.load_checkpoint(1)
            assert cp["context"]["answer"] == "yes"
            assert cp["context"]["old"] == "data"
        finally:
            store.close()

    def test_cli_unblock_invalid_context_json(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.update_task_status(1, TaskStatus.BLOCKED)
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            main(
                [
                    "--config",
                    cfg_path,
                    "unblock",
                    "1",
                    "--context",
                    "bad json",
                ]
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "invalid JSON" in captured.err

    def test_cli_unblock_not_blocked_exits_1(self, cli_config, capsys):
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "unblock", "1"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "cannot be unblocked" in captured.err


# ---------------------------------------------------------------------------
# TestCliStop
# ---------------------------------------------------------------------------


class TestCliStop:
    def test_cli_stop_queued_task(self, cli_config, capsys):
        """Stop a queued task via CLI → prints confirmation."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.close()

        main(["--config", cfg_path, "stop", "1"])
        captured = capsys.readouterr()
        assert "stopped" in captured.out

    def test_cli_stop_terminal_exits_1(self, cli_config, capsys):
        """Stop a completed task via CLI → error exit."""
        cfg_path, db_path = cli_config
        store = Store(db_path)
        store.create_task(1, "Test")
        store.update_task_status(1, TaskStatus.COMPLETED)
        store.close()

        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "stop", "1"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "terminal" in captured.err


# ---------------------------------------------------------------------------
# TestCliServe
# ---------------------------------------------------------------------------


class TestCliServe:
    def test_cli_serve_calls_engine_serve(self, cli_config):
        cfg_path, _ = cli_config
        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            main(["--config", cfg_path, "serve"])
            mock_engine.serve.assert_called_once()


# ---------------------------------------------------------------------------
# TestCliLlm
# ---------------------------------------------------------------------------


class TestCliLlm:
    def test_cli_llm_delegates_to_service(self, cli_config):
        cfg_path, _ = cli_config
        with (
            patch("src.cli.load_config", return_value={}),
            patch("src.cli._build_provider_pool") as mock_build,
            patch("src.cli.run_llm_service", return_value=0) as mock_svc,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(["--config", cfg_path, "llm"])
            assert exc_info.value.code == 0
            mock_build.assert_called_once_with({})
            mock_svc.assert_called_once()
            call_kwargs = mock_svc.call_args
            assert call_kwargs.kwargs["on_trace"] is None

    def test_cli_llm_with_task_id_env(self, cli_config, tmp_path, monkeypatch):
        cfg_path, _ = cli_config
        db_path = str(tmp_path / "llm_trace.db").replace("\\", "/")
        config = {"engine": {"db_path": db_path}}

        monkeypatch.setenv("TAO_TASK_ID", "42")

        with (
            patch("src.cli.load_config", return_value=config),
            patch("src.cli._build_provider_pool"),
            patch("src.cli.run_llm_service", return_value=0) as mock_svc,
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(["--config", cfg_path, "llm"])
            assert exc_info.value.code == 0
            call_kwargs = mock_svc.call_args
            assert call_kwargs.kwargs["on_trace"] is not None

    def test_cli_llm_nonzero_exit(self, cli_config):
        cfg_path, _ = cli_config
        with (
            patch("src.cli.load_config", return_value={}),
            patch("src.cli._build_provider_pool"),
            patch("src.cli.run_llm_service", return_value=1),
        ):
            with pytest.raises(SystemExit) as exc_info:
                main(["--config", cfg_path, "llm"])
            assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# TestCliErrorHandling
# ---------------------------------------------------------------------------


class TestCliErrorHandling:
    def test_cli_expected_error_exits_1(self, cli_config, capsys):
        """TaoError subclasses produce exit code 1 with clean message."""
        cfg_path, _ = cli_config
        # Task not found is an TaoError subclass
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", cfg_path, "status", "999"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "error:" in captured.err
        assert "not found" in captured.err

    def test_cli_unexpected_error_exits_2(self, cli_config, capsys):
        """Non-TaoError exceptions produce exit code 2."""
        cfg_path, _ = cli_config
        with patch("src.cli.Engine") as MockEngine:
            mock_engine = MockEngine.return_value.__enter__.return_value
            mock_engine.get_status.side_effect = RuntimeError("boom")
            with pytest.raises(SystemExit) as exc_info:
                main(["--config", cfg_path, "status", "1"])
            assert exc_info.value.code == 2

    def test_cli_config_not_found_exits_1(self, capsys):
        """Missing config file produces exit code 1."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--config", "/nonexistent/tao.toml", "status", "1"])
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "config file not found" in captured.err
