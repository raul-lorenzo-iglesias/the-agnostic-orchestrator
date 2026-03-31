"""Tests for tao.store — SQLite persistence for tasks, checkpoints, traces."""

from __future__ import annotations

import pytest

from src.models import StoreError, TaskNotFoundError, TaskStatus
from src.store import SCHEMA_VERSION, Store
from tests.factories import create_task_in_store, create_trace

# --- Group 1: Schema & initialization ---


def test_store_creates_tables(store):
    """All 4 tables exist after init."""
    rows = store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    names = {row["name"] for row in rows}
    assert {"tasks", "checkpoints", "traces", "meta"} <= names


def test_store_wal_mode_enabled(store):
    """WAL journal mode is active."""
    row = store._conn.execute("PRAGMA journal_mode").fetchone()
    assert row[0] == "wal"


def test_store_schema_version_set(store):
    """Meta table contains current schema_version."""
    row = store._conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    assert row["value"] == str(SCHEMA_VERSION)


def test_store_reopen_existing_db(tmp_path):
    """Closing and reopening the same DB path works."""
    db_path = str(tmp_path / "reopen.db")
    s1 = Store(db_path)
    s1.create_task(1, "task one")
    s1.close()

    s2 = Store(db_path)
    task = s2.get_task(1)
    assert task["title"] == "task one"
    s2.close()


def test_store_schema_version_mismatch(tmp_path):
    """Opening a DB with wrong schema version raises StoreError."""
    db_path = str(tmp_path / "mismatch.db")
    s = Store(db_path)
    s._conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    s._conn.commit()
    s.close()

    with pytest.raises(StoreError, match="schema version 99 is newer"):
        Store(db_path)


# --- Group 2: Task CRUD ---


def test_store_create_and_get_task(store):
    """Round-trip: create a task and get it back."""
    store.create_task(42, "Build engine", body="Full implementation")
    task = store.get_task(42)
    assert task["task_id"] == 42
    assert task["title"] == "Build engine"
    assert task["body"] == "Full implementation"
    assert task["status"] == "queued"
    assert task["config"] == {}
    assert "created_at" in task
    assert "updated_at" in task


def test_store_create_task_with_config(store):
    """Config dict survives JSON round-trip."""
    cfg = {"model": "opus", "tools": ["Read", "Write"], "nested": {"a": 1}}
    store.create_task(1, "Task with config", config=cfg)
    task = store.get_task(1)
    assert task["config"] == cfg


def test_store_create_task_duplicate_id(store):
    """Inserting a duplicate task_id raises StoreError."""
    store.create_task(1, "First")
    with pytest.raises(StoreError, match="already exists"):
        store.create_task(1, "Duplicate")


def test_store_get_task_not_found(store):
    """Getting a non-existent task raises TaskNotFoundError."""
    with pytest.raises(TaskNotFoundError, match="task 999"):
        store.get_task(999)


def test_store_update_task_status(store):
    """Updating status changes both status and updated_at."""
    create_task_in_store(store, task_id=1)
    store.update_task_status(1, TaskStatus.RUNNING)
    task = store.get_task(1)
    assert task["status"] == "running"


def test_store_update_task_status_not_found(store):
    """Updating a non-existent task raises TaskNotFoundError."""
    with pytest.raises(TaskNotFoundError, match="task 999"):
        store.update_task_status(999, TaskStatus.RUNNING)


def test_store_list_tasks_all(store):
    """List all tasks without filter."""
    for i in range(1, 4):
        store.create_task(i, f"Task {i}")
    tasks = store.list_tasks()
    assert len(tasks) == 3
    assert [t["task_id"] for t in tasks] == [1, 2, 3]


def test_store_list_tasks_by_status(store):
    """Filter tasks by status."""
    store.create_task(1, "Queued task")
    store.create_task(2, "Running task")
    store.update_task_status(2, TaskStatus.RUNNING)
    store.create_task(3, "Another queued")

    queued = store.list_tasks(status=TaskStatus.QUEUED)
    assert len(queued) == 2
    assert all(t["status"] == "queued" for t in queued)

    running = store.list_tasks(status=TaskStatus.RUNNING)
    assert len(running) == 1
    assert running[0]["task_id"] == 2


def test_store_list_tasks_empty(store):
    """Returns empty list when no tasks exist."""
    assert store.list_tasks() == []


# --- Group 3: Checkpoints ---


def test_store_save_and_load_checkpoint(store):
    """Round-trip: save and load checkpoint data."""
    create_task_in_store(store, task_id=1)
    data = {"completed_subtasks": [0, 1], "batch_number": 1}
    store.save_checkpoint(1, data)
    loaded = store.load_checkpoint(1)
    assert loaded == data


def test_store_checkpoint_overwrite(store):
    """Saving a second checkpoint overwrites the first."""
    create_task_in_store(store, task_id=1)
    store.save_checkpoint(1, {"version": 1})
    store.save_checkpoint(1, {"version": 2})
    loaded = store.load_checkpoint(1)
    assert loaded == {"version": 2}


def test_store_load_checkpoint_not_found(store):
    """Loading a non-existent checkpoint returns None."""
    assert store.load_checkpoint(999) is None


def test_store_checkpoint_complex_data(store):
    """Nested dicts and lists survive JSON round-trip."""
    create_task_in_store(store, task_id=1)
    data = {
        "nested": {"deep": {"list": [1, 2, 3]}},
        "array": [{"a": 1}, {"b": 2}],
        "null_val": None,
    }
    store.save_checkpoint(1, data)
    loaded = store.load_checkpoint(1)
    assert loaded == data


# --- Group 4: Traces ---


def test_store_record_and_get_traces(store):
    """Record multiple traces and retrieve in order."""
    create_task_in_store(store, task_id=1)
    for i in range(3):
        trace = create_trace(subtask_index=i, role="execute")
        store.record_trace(1, trace)
    traces = store.get_traces(1)
    assert len(traces) == 3
    assert [t["subtask_index"] for t in traces] == [0, 1, 2]


def test_store_get_traces_empty(store):
    """Returns empty list when no traces exist for a task."""
    assert store.get_traces(999) == []


def test_store_trace_defaults(store):
    """Recording a trace with minimal dict applies defaults."""
    create_task_in_store(store, task_id=1)
    store.record_trace(1, {"role": "scope"})
    traces = store.get_traces(1)
    assert len(traces) == 1
    t = traces[0]
    assert t["role"] == "scope"
    assert t["model"] == ""
    assert t["tokens_in"] == 0
    assert t["tokens_out"] == 0
    assert t["cost_usd"] == 0.0
    assert t["elapsed_s"] == 0.0
    assert t["success"] is True
    assert t["attempt"] == 1
    assert t["subtask_index"] == 0


def test_store_trace_success_boolean_conversion(store):
    """success field converts between bool (Python) and int (SQLite)."""
    create_task_in_store(store, task_id=1)
    store.record_trace(1, create_trace(success=False))
    store.record_trace(1, create_trace(success=True))
    traces = store.get_traces(1)
    assert traces[0]["success"] is False
    assert traces[1]["success"] is True


def test_store_get_summary(store):
    """Summary aggregates trace data correctly."""
    create_task_in_store(store, task_id=1)
    store.record_trace(
        1,
        create_trace(cost_usd=0.10, elapsed_s=5.0, tokens_in=100, tokens_out=200, success=True),
    )
    store.record_trace(
        1,
        create_trace(cost_usd=0.05, elapsed_s=3.0, tokens_in=50, tokens_out=100, success=True),
    )
    store.record_trace(
        1,
        create_trace(cost_usd=0.02, elapsed_s=1.0, tokens_in=20, tokens_out=40, success=False),
    )

    summary = store.get_summary(1)
    assert summary["task_id"] == 1
    assert summary["total_cost_usd"] == pytest.approx(0.17)
    assert summary["total_elapsed_s"] == pytest.approx(9.0)
    assert summary["total_tokens_in"] == 170
    assert summary["total_tokens_out"] == 340
    assert summary["steps_succeeded"] == 2
    assert summary["steps_failed"] == 1
    assert summary["trace_count"] == 3


def test_store_get_summary_no_traces(store):
    """Summary with no traces returns zeroed values."""
    summary = store.get_summary(999)
    assert summary["task_id"] == 999
    assert summary["total_cost_usd"] == 0.0
    assert summary["total_elapsed_s"] == 0.0
    assert summary["total_tokens_in"] == 0
    assert summary["total_tokens_out"] == 0
    assert summary["steps_succeeded"] == 0
    assert summary["steps_failed"] == 0
    assert summary["trace_count"] == 0


# --- Group 5: Edge cases & resilience ---


def test_store_corrupt_json_in_config(store):
    """Corrupt JSON in config column returns {} instead of crashing."""
    store._conn.execute(
        "INSERT INTO tasks (task_id, title, config) VALUES (?, ?, ?)",
        (99, "corrupt", "not-json{{{"),
    )
    store._conn.commit()
    task = store.get_task(99)
    assert task["config"] == {}
    assert task["title"] == "corrupt"


def test_store_corrupt_json_in_checkpoint(store):
    """Corrupt JSON in checkpoint data returns {} instead of crashing."""
    create_task_in_store(store, task_id=1)
    store._conn.execute(
        "INSERT OR REPLACE INTO checkpoints (task_id, data) VALUES (?, ?)",
        (1, "broken[[["),
    )
    store._conn.commit()
    loaded = store.load_checkpoint(1)
    assert loaded == {}


def test_store_close_idempotent(store):
    """Calling close() twice does not raise."""
    store.close()
    store.close()


def test_store_creates_parent_directories(tmp_path):
    """Store creates parent directories if they don't exist."""
    db_path = str(tmp_path / "nested" / "deep" / "store.db")
    s = Store(db_path)
    s.create_task(1, "works")
    assert s.get_task(1)["title"] == "works"
    s.close()


# --- current_step ---


def test_store_current_step_default_empty(store):
    """New tasks have empty current_step."""
    create_task_in_store(store)
    task = store.get_task(1)
    assert task["current_step"] == ""


def test_store_update_current_step(store):
    """update_current_step sets the field."""
    create_task_in_store(store)
    store.update_current_step(1, "execute:1 — Build feature")
    task = store.get_task(1)
    assert task["current_step"] == "execute:1 — Build feature"


def test_store_clear_current_step(store):
    """Setting current_step to empty string clears it."""
    create_task_in_store(store)
    store.update_current_step(1, "scope")
    store.update_current_step(1, "")
    task = store.get_task(1)
    assert task["current_step"] == ""


def test_store_schema_v3_has_current_step(store):
    """Schema v3 includes current_step column in tasks table."""
    row = store._conn.execute("PRAGMA table_info(tasks)").fetchall()
    columns = {r["name"] for r in row}
    assert "current_step" in columns


# --- Group 8: Subtasks ---


def test_store_update_subtasks(store):
    """update_subtasks stores JSON list, retrievable via get_task."""
    create_task_in_store(store)
    subtasks = [
        {"title": "Research keywords", "description": "Find relevant keywords"},
        {"title": "Analyze competitors", "description": "Check top 10"},
    ]
    store.update_subtasks(1, subtasks)
    task = store.get_task(1)
    assert task["subtasks"] == subtasks


def test_store_subtasks_default_empty_list(store):
    """New tasks have empty subtasks list by default."""
    create_task_in_store(store)
    task = store.get_task(1)
    assert task["subtasks"] == []


def test_store_update_subtasks_replaces(store):
    """Calling update_subtasks again replaces the previous list."""
    create_task_in_store(store)
    store.update_subtasks(1, [{"title": "A"}])
    store.update_subtasks(1, [{"title": "B"}, {"title": "C"}])
    task = store.get_task(1)
    assert len(task["subtasks"]) == 2
    assert task["subtasks"][0]["title"] == "B"


def test_store_schema_v4_has_subtasks(store):
    """Schema v4 includes subtasks column in tasks table."""
    row = store._conn.execute("PRAGMA table_info(tasks)").fetchall()
    columns = {r["name"] for r in row}
    assert "subtasks" in columns


def test_store_old_schema_raises(tmp_path):
    """Old schema version → clear error telling user to delete DB."""
    import sqlite3

    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE tasks (task_id INTEGER PRIMARY KEY, title TEXT NOT NULL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '0');
    """)
    conn.commit()
    conn.close()

    with pytest.raises(StoreError, match="outdated.*Delete"):
        Store(db_path)
