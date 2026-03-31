"""Threading-based task scheduler with polling loop.


One daemon thread runs the polling loop. Each task runs in its own thread.
Stop signals use flow.request_stop() (threading.Event per task).

The queue manager is the bridge between the public API (submit/unblock/stop)
and the flow engine (run_flow). It handles concurrency, scheduling, and
lifecycle management.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from src.flow import request_stop, run_flow
from src.models import TERMINAL_STATUSES, TaoError, TaskNotFoundError, TaskStatus
from src.store import Store

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 5
DEFAULT_POLL_INTERVAL = 2.0


class QueueManager:
    """Threading-based task scheduler with polling loop.

    One daemon thread runs the polling loop. Each task runs in its own thread.
    Stop signals use flow.request_stop() (threading.Event per task).
    """

    def __init__(
        self,
        store: Store,
        pool: Any,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        flow_runner: Callable[..., Any] | None = None,
        config_path: str = "",
    ) -> None:
        self._store = store
        self._pool = pool
        self._max_concurrent = max_concurrent
        self._poll_interval = poll_interval
        self._flow_runner: Callable[..., Any] = flow_runner or run_flow
        self._config_path = config_path
        self._running: dict[int, threading.Thread] = {}
        self._lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the polling loop. Idempotent — safe to call multiple times."""
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._shutdown_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="tao-queue-poll"
        )
        self._poll_thread.start()

    def shutdown(self, timeout: float = 10.0) -> None:
        """Shut down the queue manager gracefully.

        Signals the poll loop to stop, then requests stop for all running tasks.
        Joins threads with the given timeout.
        """
        self._shutdown_event.set()

        if self._poll_thread is not None:
            self._poll_thread.join(timeout=timeout)
            if self._poll_thread.is_alive():
                logger.warning("poll thread did not exit within %.1fs", timeout)
            self._poll_thread = None

        # Request stop for all running tasks
        with self._lock:
            task_ids = list(self._running.keys())
            threads = dict(self._running)

        for tid in task_ids:
            request_stop(tid)

        # Join task threads with remaining time
        for tid, thread in threads.items():
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("task %d thread did not exit after shutdown", tid)

        with self._lock:
            self._running.clear()

    def submit(
        self,
        task_id: int | None = None,
        title: str = "",
        body: str = "",
        *,
        config: dict[str, Any] | None = None,
    ) -> int:
        """Submit a task to the queue.

        Creates the task in the store with status ``queued``. The poll loop
        picks it up when capacity is available.

        Args:
            task_id: Unique task identifier. If None, auto-assigns an ID.
            title: Task title.
            body: Task body/description.
            pack_path: Path to the pack directory with step manifests.
            config: Optional flow configuration dict.

        Returns:
            The task_id (provided or auto-generated).

        Raises:
            StoreError: if task_id already exists.
        """
        full_config = dict(config or {})
        if task_id is None:
            task_id = self._store.create_task_auto_id(title, body, config=full_config)
        else:
            self._store.create_task(task_id, title, body, config=full_config)
        return task_id

    def unblock(
        self,
        task_id: int,
        context: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        """Unblock a blocked, stopped, or failed task.

        Optionally merges new context into the checkpoint and/or updates
        the task config before re-queuing.

        Args:
            task_id: Task to unblock.
            context: Optional context dict to merge into the checkpoint.
            config: Optional config dict to update the task config in DB.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task cannot be unblocked from its current status.
        """
        task = self._store.get_task(task_id)
        status = TaskStatus(task["status"])
        if status not in (TaskStatus.BLOCKED, TaskStatus.STOPPED, TaskStatus.FAILED):
            raise TaoError(f"task {task_id} cannot be unblocked (status: {status})")

        if context is not None:
            checkpoint = self._store.load_checkpoint(task_id)
            if checkpoint is not None:
                human_message = context.get("human_message", "")
                if human_message:
                    # Route human_message to the appropriate context level:
                    # - subtask_context present → subtask failed → execute will see it
                    # - otherwise → task_context → scope/re-scope will see it
                    if "subtask_context" in checkpoint:
                        sub_ctx = checkpoint.get("subtask_context", {})
                        sub_ctx["human_message"] = human_message
                        checkpoint["subtask_context"] = sub_ctx
                    else:
                        task_ctx = checkpoint.get("task_context", {})
                        task_ctx["human_message"] = human_message
                        checkpoint["task_context"] = task_ctx

                # Merge any other context fields (backward compat)
                other = {k: v for k, v in context.items() if k != "human_message"}
                if other:
                    cp_context = checkpoint.get("context", {})
                    cp_context.update(other)
                    checkpoint["context"] = cp_context

                self._store.save_checkpoint(task_id, checkpoint)

        if config is not None:
            self._store.update_task_config(task_id, config)

        logger.info("[task %d] unblocked → queued", task_id)
        self._store.update_task_status(task_id, TaskStatus.QUEUED)

    def stop_task(self, task_id: int) -> None:
        """Request graceful stop for a task.

        Works on queued, running, or blocked tasks:
        - Running: finishes active phase, then marks stopped.
        - Queued/Blocked: marks stopped directly.

        Args:
            task_id: Task to stop.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is in a terminal status and has no running thread.
        """
        task = self._store.get_task(task_id)
        status = TaskStatus(task["status"])

        with self._lock:
            thread = self._running.get(task_id)

        if thread is None and status in TERMINAL_STATUSES:
            raise TaoError(f"task {task_id} is already terminal (status: {status})")

        if thread is not None:
            # Running — graceful stop, then set stopped
            request_stop(task_id)
            thread.join(timeout=30.0)
            if thread.is_alive():
                logger.warning("task %d thread did not exit after stop request (30s)", task_id)
            self._store.update_task_status(task_id, TaskStatus.STOPPED)
        else:
            # Queued or blocked — mark stopped directly
            self._store.update_task_status(task_id, TaskStatus.STOPPED)
            logger.info("[task %d] stopped (was %s)", task_id, status)

    def cancel_task(self, task_id: int) -> None:
        """Cancel a task. Terminal — cannot be resumed.

        Works on any non-terminal task: queued, running, or blocked.
        If running (has a thread), waits for the active phase to finish
        before marking cancelled.

        Args:
            task_id: Task to cancel.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is already in a terminal status and has no
                running thread.
        """
        task = self._store.get_task(task_id)
        status = TaskStatus(task["status"])

        # Check for a running thread — the task may have a thread even if
        # DB status hasn't been updated to RUNNING yet.
        with self._lock:
            thread = self._running.get(task_id)

        if thread is None and status in TERMINAL_STATUSES:
            raise TaoError(
                f"task {task_id} is already terminal (status: {status})"
            )

        if thread is not None:
            request_stop(task_id)
            thread.join(timeout=30.0)
            if thread.is_alive():
                logger.warning(
                    "task %d thread did not exit after cancel (30s)", task_id
                )

        self._store.update_task_status(task_id, TaskStatus.CANCELLED)
        logger.info("[task %d] cancelled", task_id)

    def restart_task(self, task_id: int) -> None:
        """Restart a task from scratch — clears checkpoint, traces, and re-queues.

        Works on any non-running task. If running, stop it first.

        Args:
            task_id: Task to restart.

        Raises:
            TaskNotFoundError: if task_id does not exist.
            TaoError: if task is currently running.
        """
        task = self._store.get_task(task_id)
        status = TaskStatus(task["status"])

        with self._lock:
            thread = self._running.get(task_id)

        if thread is not None and thread.is_alive():
            raise TaoError(f"task {task_id} is running — stop it first")

        self._store.delete_checkpoint(task_id)
        self._store.delete_traces(task_id)
        self._store.update_current_step(task_id, "")
        self._store.update_subtasks(task_id, [])
        self._store.update_task_status(task_id, TaskStatus.QUEUED)
        logger.info("[task %d] restarted → queued", task_id)

    @property
    def running_count(self) -> int:
        """Number of tasks currently running."""
        with self._lock:
            return len(self._running)

    # --- Private methods ---

    def _poll_loop(self) -> None:
        """Main polling loop — runs in a daemon thread."""
        consecutive_errors = 0
        while not self._shutdown_event.is_set():
            try:
                self._cleanup_finished()
                with self._lock:
                    current = len(self._running)
                if current < self._max_concurrent:
                    tasks = self._store.list_tasks(status=TaskStatus.QUEUED)
                    if tasks:
                        self._launch_task(tasks[0])
                consecutive_errors = 0
            except Exception:
                consecutive_errors += 1
                logger.exception("error in poll loop (consecutive: %d)", consecutive_errors)
            wait = (
                self._poll_interval
                if consecutive_errors == 0
                else min(self._poll_interval * consecutive_errors, 30.0)
            )
            self._shutdown_event.wait(wait)

    def _launch_task(self, task: dict[str, Any]) -> None:
        """Launch a task in its own thread."""
        task_id = task["task_id"]
        task_config = dict(task.get("config", {}))
        task_config["_config_path"] = self._config_path

        logger.info("[task %d] launching from queue", task_id)
        thread = threading.Thread(
            target=self._run_task,
            args=(task_id, task_config),
            daemon=True,
            name=f"tao-task-{task_id}",
        )
        with self._lock:
            self._running[task_id] = thread
        thread.start()

    def _run_task(self, task_id: int, config: dict[str, Any]) -> None:
        """Execute a task's flow. Called in a dedicated thread."""
        try:
            self._flow_runner(
                task_id,
                store=self._store,
                pool=self._pool,
                config=config,
            )
        except Exception as exc:
            logger.exception("unexpected error running task %d", task_id)
            try:
                # Record the error as a trace so it's visible via tao traces / HTTP API
                self._store.record_trace(task_id, {
                    "subtask_index": 0, "role": "error", "model": "",
                    "tokens_in": 0, "tokens_out": 0, "cost_usd": 0, "elapsed_s": 0,
                    "success": False, "attempt": 1, "error": str(exc),
                    "label": f"config error: {exc}",
                })
                self._store.update_task_status(task_id, TaskStatus.FAILED)
            except TaskNotFoundError:
                logger.debug(
                    "[task %d] not found during status update (cleaned up?)",
                    task_id,
                )

    def _cleanup_finished(self) -> None:
        """Remove finished threads from the running dict."""
        with self._lock:
            finished = [tid for tid, thread in self._running.items() if not thread.is_alive()]
            for tid in finished:
                del self._running[tid]
