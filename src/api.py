"""Python API — the engine's public interface.


The Engine class wires Store + ProviderPool + QueueManager together.
load_config() reads TOML. All CLI commands are thin wrappers over Engine methods.
"""

from __future__ import annotations

import logging
import os
import time
import tomllib
from pathlib import Path
from typing import Any

from src.flow import run_flow as _run_flow
from src.models import DELETABLE_STATUSES, TERMINAL_STATUSES, TaoError, TaskStatus
from src.providers import ClaudeCliProvider, CopilotCliProvider, ProviderPool
from src.queue import QueueManager
from src.store import Store

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = ".tao/engine.db"

_PROVIDER_TYPES: dict[str, type] = {
    "claude_cli": ClaudeCliProvider,
    "copilot_cli": CopilotCliProvider,
}


def load_config(path: str) -> dict[str, Any]:
    """Load engine configuration from a TOML file.

    Args:
        path: Path to the TOML configuration file.

    Returns:
        Parsed configuration dict.

    Raises:
        TaoError: If the file is not found or contains invalid TOML.
    """
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        raise TaoError(f"config file not found: {path}")

    try:
        return tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise TaoError(f"invalid TOML in config: {e}")


def _build_provider_pool(config: dict[str, Any]) -> ProviderPool:
    """Build a ProviderPool from the config's ``providers`` section.

    Each key under ``providers`` is a provider instance name. Each entry
    must have ``type`` (e.g. ``"claude_cli"``) and ``models`` (dict of
    alias → model_id).

    Args:
        config: Full engine config dict (reads ``config["providers"]``).

    Returns:
        Configured ProviderPool.

    Raises:
        TaoError: If a provider type is unknown.
    """
    providers_config = config.get("providers", {})
    if not providers_config:
        return ProviderPool(providers=[], model_map={})

    providers: list[Any] = []
    model_map: dict[str, list[str]] = {}

    for name, entry in providers_config.items():
        ptype = entry.get("type", "")
        cls = _PROVIDER_TYPES.get(ptype)
        if cls is None:
            raise TaoError(f"unknown provider type: {ptype}")

        models = entry.get("models", {})
        provider = cls(models=models)
        # Override class-level name with the config key
        provider.name = name
        providers.append(provider)

        for alias in models:
            if alias not in model_map:
                model_map[alias] = []
            model_map[alias].append(name)

    return ProviderPool(providers=providers, model_map=model_map)


class Engine:
    """Public API for the TAO engine.

    Wires together Store, ProviderPool, and QueueManager. All CLI commands
    delegate to Engine methods.

    Args:
        config: Configuration dict (same structure as parsed TOML).
        config_path: Path to a TOML config file. Ignored if ``config`` is given.
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        config_path: str | None = None,
    ) -> None:
        if config is None and config_path is not None:
            config = load_config(config_path)
        if config is None:
            config = {}

        self._config = config
        self._config_path = os.path.abspath(config_path) if config_path else ""
        engine_config = config.get("engine", {})
        db_path = engine_config.get("db_path", DEFAULT_DB_PATH)
        if not os.path.isabs(db_path) and config_path:
            db_path = os.path.join(os.path.dirname(self._config_path), db_path)
        max_concurrent = engine_config.get("max_concurrent", 5)

        self._store = Store(db_path)
        self._store.recover_running_tasks()
        self._pool = _build_provider_pool(config)
        self._queue = QueueManager(
            self._store, self._pool,
            max_concurrent=max_concurrent,
            config_path=self._config_path,
        )

    # --- Task lifecycle ---

    def submit(
        self,
        task_id: int | None = None,
        title: str = "",
        body: str = "",
        *,
        config: dict[str, Any] | None = None,
    ) -> int:
        """Submit a task to the queue.

        Args:
            task_id: Unique task identifier. If None, auto-assigns an ID.
            title: Task title.
            body: Task body/description.
            config: Flow configuration dict. Required keys: ``cwd`` (working
                directory) and ``cycle`` (step sequence). Optional: ``scope``,
                ``max_retries``, ``policies``, ``tools``.

        Returns:
            The task_id (provided or auto-generated).
        """
        return self._queue.submit(task_id, title, body, config=config)

    def run_flow(self, task_id: int) -> TaskStatus:
        """Run a task's flow synchronously (bypass the queue).

        The task must already exist in the store. Sets status to ``running``
        before executing.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        task = self._store.get_task(task_id)
        task_config = dict(task.get("config", {}))
        task_config["_config_path"] = self._config_path

        return _run_flow(
            task_id,
            store=self._store,
            pool=self._pool,
            config=task_config,
        )

    def unblock(
        self,
        task_id: int,
        context: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Unblock a task, optionally merging context and/or updating config.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task cannot be unblocked.
        """
        self._queue.unblock(task_id, context, config=config)

    def stop(self, task_id: int) -> None:
        """Request graceful stop for a running task.

        Raises:
            TaoError: if task is not currently running.
        """
        self._queue.stop_task(task_id)

    def cancel(self, task_id: int) -> None:
        """Cancel a task. Terminal — cannot be resumed.

        Works on any non-terminal task.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is already terminal.
        """
        self._queue.cancel_task(task_id)

    def restart(self, task_id: int) -> None:
        """Restart a task from scratch — clears checkpoint, traces, re-queues.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is currently running.
        """
        self._queue.restart_task(task_id)

    def delete(self, task_id: int) -> None:
        """Delete a task that is no longer active.

        Deletable statuses: completed, failed, cancelled, stopped.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is not in a deletable status.
        """
        task = self._store.get_task(task_id)
        status = TaskStatus(task["status"])
        if status not in DELETABLE_STATUSES:
            raise TaoError(
                f"cannot delete task {task_id}: status is {task['status']}"
            )
        self._store.delete_task(task_id)

    # --- Observability ---

    def get_status(self, task_id: int) -> dict[str, Any]:
        """Fetch task status and metadata.

        If the task is blocked, includes ``blocked_reason`` from the checkpoint
        when available.

        Raises:
            TaskNotFoundError: if task_id does not exist.
        """
        task = self._store.get_task(task_id)
        if task["status"] == TaskStatus.BLOCKED:
            checkpoint = self._store.load_checkpoint(task_id)
            if checkpoint is not None:
                reason = checkpoint.get("blocked_reason")
                if reason:
                    task["blocked_reason"] = reason
        return task

    def get_traces(self, task_id: int) -> list[dict[str, Any]]:
        """Get all execution traces for a task."""
        return self._store.get_traces(task_id)

    def summary(self, task_id: int) -> dict[str, Any]:
        """Get aggregated metrics for a task."""
        return self._store.get_summary(task_id)

    def list_tasks(self, status: TaskStatus | str | None = None) -> list[dict[str, Any]]:
        """List all tasks, optionally filtered by status."""
        return self._store.list_tasks(status)

    @property
    def queue_status(self) -> dict[str, int]:
        """Current queue state: running count and max concurrency."""
        return {
            "running": self._queue.running_count,
            "max_concurrent": self._queue._max_concurrent,
        }

    # --- Server ---

    def serve(self) -> None:
        """Start the queue and block until interrupted.

        The queue's poll loop runs in a daemon thread. This method blocks
        the main thread until ``KeyboardInterrupt``.
        """
        self._queue.start()
        logger.info("engine serving (max_concurrent=%d)", self._queue._max_concurrent)
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            self._queue.shutdown()

    # --- Lifecycle ---

    def close(self) -> None:
        """Shut down the queue and close the store. Idempotent."""
        self._queue.shutdown()
        self._store.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
