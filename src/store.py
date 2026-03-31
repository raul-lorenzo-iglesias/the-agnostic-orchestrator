"""SQLite store — tasks, checkpoints, traces.


Internal to the engine, created automatically. Three levels of persistence:
1. Task record with config and status
2. Checkpoint data (per batch, for resume)
3. Trace records (per LLM invocation, for observability)

One connection per Store instance. WAL mode enabled for concurrent reads.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from src.models import StoreError, TaskNotFoundError, TaskStatus

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tasks (
    task_id      INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'queued',
    current_step TEXT NOT NULL DEFAULT '',
    subtasks     TEXT NOT NULL DEFAULT '[]',
    config       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS checkpoints (
    task_id    INTEGER PRIMARY KEY REFERENCES tasks(task_id),
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS traces (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id        INTEGER NOT NULL REFERENCES tasks(task_id),
    subtask_index  INTEGER NOT NULL DEFAULT 0,
    role           TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    tokens_in      INTEGER NOT NULL DEFAULT 0,
    tokens_out     INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0.0,
    elapsed_s      REAL NOT NULL DEFAULT 0.0,
    success        INTEGER NOT NULL DEFAULT 1,
    attempt        INTEGER NOT NULL DEFAULT 1,
    error          TEXT NOT NULL DEFAULT '',
    label          TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...] = (),
    max_retries: int = 3,
    delay: float = 0.1,
) -> sqlite3.Cursor:
    """Execute SQL with retry on lock contention.

    Uses linear backoff: delay * (attempt + 1) between retries.
    """
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise
    # Unreachable — last attempt either returns or raises
    raise sqlite3.OperationalError("unreachable")  # pragma: no cover


class Store:
    """SQLite-backed persistence for tasks, checkpoints, and traces.

    One connection per instance. WAL mode enabled on init.
    Call close() when done.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._init_schema()
        self._check_schema_version()

    def close(self) -> None:
        """Close the database connection. Idempotent."""
        try:
            self._conn.close()
        except Exception:
            pass

    def _init_schema(self) -> None:
        """Create tables if they don't exist and seed schema version."""
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
            ("schema_version", str(SCHEMA_VERSION)),
        )
        self._conn.commit()

    def _check_schema_version(self) -> None:
        """Verify DB schema version and migrate if needed."""
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", ("schema_version",)
        ).fetchone()
        if row is None:
            raise StoreError("schema_version not found in meta table")
        version = int(row["value"])
        if version > SCHEMA_VERSION:
            raise StoreError(
                f"schema version {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            self._migrate(version)

    def _migrate(self, from_version: int) -> None:
        """Apply schema migrations sequentially."""
        raise StoreError(
            f"schema version {from_version} is outdated (current: {SCHEMA_VERSION}). "
            "Delete the database and restart."
        )

    def _parse_json_column(self, raw: str | None, column_name: str) -> Any:
        """Safely parse a JSON column, returning {} on failure."""
        if raw is None:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("corrupt JSON in column %s: %r", column_name, raw)
            return {}

    def _row_to_task(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a Row to a plain dict with parsed config and subtasks."""
        d = dict(row)
        d["config"] = self._parse_json_column(d.get("config"), "config")
        raw_subtasks = d.get("subtasks", "[]")
        parsed = self._parse_json_column(raw_subtasks, "subtasks")
        d["subtasks"] = parsed if isinstance(parsed, list) else []
        return d

    def recover_running_tasks(self) -> int:
        """Reset tasks stuck in ``running`` back to ``queued``.

        Called on startup to recover from unclean shutdowns. Returns
        the number of tasks recovered.
        """
        with self._lock:
            cursor = _execute_with_retry(
                self._conn,
                "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE status = ?",
                (TaskStatus.QUEUED.value, TaskStatus.RUNNING.value),
            )
            self._conn.commit()
            count = cursor.rowcount
        if count:
            logger.info("recovered %d running task(s) → queued", count)
        return count

    # --- Task CRUD ---

    def create_task(
        self,
        task_id: int,
        title: str,
        body: str = "",
        config: dict[str, Any] | None = None,
    ) -> None:
        """Insert a new task.

        Raises:
            StoreError: if task_id already exists.
        """
        config_json = json.dumps(config or {})
        with self._lock:
            try:
                _execute_with_retry(
                    self._conn,
                    "INSERT INTO tasks (task_id, title, body, config) VALUES (?, ?, ?, ?)",
                    (task_id, title, body, config_json),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as e:
                raise StoreError(f"task {task_id} already exists") from e

    def create_task_auto_id(
        self,
        title: str,
        body: str = "",
        config: dict[str, Any] | None = None,
    ) -> int:
        """Insert a new task with an auto-generated ID.

        Lets SQLite assign the rowid automatically.

        Returns:
            The auto-generated task_id.

        Raises:
            StoreError: on persistence failure.
        """
        config_json = json.dumps(config or {})
        with self._lock:
            try:
                cursor = _execute_with_retry(
                    self._conn,
                    "INSERT INTO tasks (title, body, config) VALUES (?, ?, ?)",
                    (title, body, config_json),
                )
                self._conn.commit()
                return cursor.lastrowid
            except sqlite3.IntegrityError as e:
                raise StoreError(f"failed to create task: {e}") from e

    def delete_task(self, task_id: int) -> None:
        """Delete a task and all its related data (traces, checkpoints).

        Deletes from traces, checkpoints, and tasks in that order to
        respect foreign key relationships.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        with self._lock:
            # Check existence first
            row = self._conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task {task_id} not found")

            _execute_with_retry(
                self._conn,
                "DELETE FROM traces WHERE task_id = ?",
                (task_id,),
            )
            _execute_with_retry(
                self._conn,
                "DELETE FROM checkpoints WHERE task_id = ?",
                (task_id,),
            )
            _execute_with_retry(
                self._conn,
                "DELETE FROM tasks WHERE task_id = ?",
                (task_id,),
            )
            self._conn.commit()

    def get_task(self, task_id: int) -> dict[str, Any]:
        """Fetch a task by ID.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            return self._row_to_task(row)

    def update_task_status(self, task_id: int, status: TaskStatus | str) -> None:
        """Update a task's status and updated_at timestamp.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        status_val = status.value if isinstance(status, TaskStatus) else status
        with self._lock:
            cursor = _execute_with_retry(
                self._conn,
                "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE task_id = ?",
                (status_val, task_id),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                raise TaskNotFoundError(f"task {task_id} not found")

    def update_current_step(self, task_id: int, step: str) -> None:
        """Update the current_step field for a running task.

        Used to indicate what step is currently executing. Set to empty
        string when the task finishes, fails, or is stopped.
        """
        with self._lock:
            _execute_with_retry(
                self._conn,
                "UPDATE tasks SET current_step = ?, updated_at = datetime('now') WHERE task_id = ?",
                (step, task_id),
            )
            self._conn.commit()

    def update_subtasks(self, task_id: int, subtasks: list[dict[str, Any]]) -> None:
        """Store the subtask list for a task (from scope output).

        Called after scope completes so the monitor can show subtask progress.
        """
        with self._lock:
            _execute_with_retry(
                self._conn,
                "UPDATE tasks SET subtasks = ?, updated_at = datetime('now') WHERE task_id = ?",
                (json.dumps(subtasks), task_id),
            )
            self._conn.commit()

    def update_task_config(self, task_id: int, config: dict[str, Any]) -> None:
        """Merge new config into the task's existing config.

        Performs a shallow merge: top-level keys in ``config`` overwrite
        the corresponding keys in the stored config. Keys not present in
        ``config`` are left unchanged.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row is None:
                raise TaskNotFoundError(f"task {task_id} not found")
            existing = self._parse_json_column(dict(row).get("config"), "config")
            existing.update(config)
            _execute_with_retry(
                self._conn,
                "UPDATE tasks SET config = ?, updated_at = datetime('now') WHERE task_id = ?",
                (json.dumps(existing), task_id),
            )
            self._conn.commit()

    def list_tasks(self, status: TaskStatus | str | None = None) -> list[dict[str, Any]]:
        """List all tasks, optionally filtered by status."""
        with self._lock:
            if status is not None:
                status_val = status.value if isinstance(status, TaskStatus) else status
                rows = self._conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC",
                    (status_val,),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            return [self._row_to_task(row) for row in rows]

    # --- Checkpoints ---

    def save_checkpoint(self, task_id: int, data: dict[str, Any]) -> None:
        """Save or overwrite checkpoint data for a task."""
        data_json = json.dumps(data)
        with self._lock:
            _execute_with_retry(
                self._conn,
                "INSERT OR REPLACE INTO checkpoints (task_id, data, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (task_id, data_json),
            )
            self._conn.commit()

    def load_checkpoint(self, task_id: int) -> dict[str, Any] | None:
        """Load checkpoint data for a task. Returns None if no checkpoint exists."""
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM checkpoints WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                return None
            return self._parse_json_column(row["data"], "checkpoint.data")

    def delete_checkpoint(self, task_id: int) -> None:
        """Delete checkpoint data for a task."""
        with self._lock:
            _execute_with_retry(
                self._conn,
                "DELETE FROM checkpoints WHERE task_id = ?",
                (task_id,),
            )
            self._conn.commit()

    def delete_traces(self, task_id: int) -> None:
        """Delete all traces for a task."""
        with self._lock:
            _execute_with_retry(
                self._conn,
                "DELETE FROM traces WHERE task_id = ?",
                (task_id,),
            )
            self._conn.commit()

    # --- Traces ---

    def record_trace(self, task_id: int, trace: dict[str, Any]) -> None:
        """Record a trace entry for an LLM invocation.

        Missing keys in ``trace`` get sensible defaults.
        Boolean ``success`` is converted to int for SQLite storage.
        """
        success = trace.get("success", True)
        success_int = 1 if success else 0
        with self._lock:
            _execute_with_retry(
                self._conn,
                "INSERT INTO traces "
                "(task_id, subtask_index, role, model, tokens_in, tokens_out, "
                "cost_usd, elapsed_s, success, attempt, error, label) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    trace.get("subtask_index", 0),
                    trace.get("role", ""),
                    trace.get("model", ""),
                    trace.get("tokens_in", 0),
                    trace.get("tokens_out", 0),
                    trace.get("cost_usd", 0.0),
                    trace.get("elapsed_s", 0.0),
                    success_int,
                    trace.get("attempt", 1),
                    trace.get("error", ""),
                    trace.get("label", ""),
                ),
            )
            self._conn.commit()

    def get_traces(self, task_id: int) -> list[dict[str, Any]]:
        """Get all traces for a task, ordered by insertion."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM traces WHERE task_id = ? ORDER BY id ASC", (task_id,)
            ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["success"] = bool(d["success"])
                results.append(d)
            return results

    def get_summary(self, task_id: int) -> dict[str, Any]:
        """Aggregate trace data for a task.

        Returns zeroed summary if no traces exist (not an error).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT "
                "  COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd, "
                "  COALESCE(SUM(elapsed_s), 0.0) AS total_elapsed_s, "
                "  COALESCE(SUM(tokens_in), 0) AS total_tokens_in, "
                "  COALESCE(SUM(tokens_out), 0) AS total_tokens_out, "
                "  COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS steps_succeeded, "
                "  COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS steps_failed, "
                "  COUNT(*) AS trace_count "
                "FROM traces WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return {
                "task_id": task_id,
                "total_cost_usd": 0.0,
                "total_elapsed_s": 0.0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "steps_succeeded": 0,
                "steps_failed": 0,
                "trace_count": 0,
            }
        return {
            "task_id": task_id,
            "total_cost_usd": row["total_cost_usd"],
            "total_elapsed_s": row["total_elapsed_s"],
            "total_tokens_in": row["total_tokens_in"],
            "total_tokens_out": row["total_tokens_out"],
            "steps_succeeded": row["steps_succeeded"],
            "steps_failed": row["steps_failed"],
            "trace_count": row["trace_count"],
        }
