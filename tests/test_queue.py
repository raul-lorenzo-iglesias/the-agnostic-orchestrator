"""Tests for tao.queue — QueueManager scheduling, unblock, stop."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from src.models import StoreError, TaoError, TaskNotFoundError, TaskStatus
from src.queue import QueueManager

from tests.factories import create_task_in_store

# --- Test helpers ---


def _make_flow_runner(
    results: list[TaskStatus],
    *,
    event: threading.Event | None = None,
    block_event: threading.Event | None = None,
):
    """Create a mock flow_runner that sets task status from ``results``.

    Args:
        results: List of TaskStatus values to apply sequentially.
        event: If provided, set when the flow starts (for synchronization).
        block_event: If provided, wait on it before completing (simulate long tasks).
    """
    call_count = {"n": 0}

    def runner(
        task_id: int,
        *,
        store: Any,
        pool: Any,
        config: dict[str, Any],
    ) -> TaskStatus:
        if event:
            event.set()
        if block_event:
            block_event.wait(timeout=10)
        idx = min(call_count["n"], len(results) - 1)
        call_count["n"] += 1
        result = results[idx]
        store.update_task_status(task_id, result)
        return result

    return runner


def _make_failing_flow_runner(*, event: threading.Event | None = None):
    """Create a flow_runner that raises RuntimeError."""

    def runner(
        task_id: int,
        *,
        store: Any,
        pool: Any,
        config: dict[str, Any],
    ) -> None:
        if event:
            event.set()
        raise RuntimeError("simulated flow failure")

    return runner


# --- Submit ---


class TestQueueSubmit:
    def test_queue_submit_creates_task(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.submit(1, "Task 1", "body")

        task = store.get_task(1)
        assert task["status"] == TaskStatus.QUEUED
        assert task["title"] == "Task 1"

    def test_queue_submit_duplicate_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.submit(1, "Task 1")

        with pytest.raises(StoreError, match="already exists"):
            qm.submit(1, "Task 1 dup")

    def test_queue_submit_with_config(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.submit(1, "Task 1", config={"key": "value"})

        task = store.get_task(1)
        assert task["config"]["key"] == "value"


# --- Start / Shutdown ---


class TestQueueStartShutdown:
    def test_queue_start_idempotent(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.start()
        thread1 = qm._poll_thread
        qm.start()  # second call should be a no-op
        assert qm._poll_thread is thread1
        qm.shutdown(timeout=2.0)

    def test_queue_shutdown_without_start(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.shutdown(timeout=1.0)  # should not raise


# --- Execution ---


class TestQueueExecution:
    def test_queue_picks_up_queued_task(self, store, mock_pool):
        started = threading.Event()
        runner = _make_flow_runner([TaskStatus.COMPLETED], event=started)
        qm = QueueManager(store, mock_pool, poll_interval=0.05, flow_runner=runner)

        qm.submit(1, "Task 1")
        qm.start()

        assert started.wait(timeout=5.0), "flow_runner was never called"

        # Wait for task to complete
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            task = store.get_task(1)
            if task["status"] == TaskStatus.COMPLETED:
                break
            time.sleep(0.05)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.COMPLETED
        qm.shutdown(timeout=2.0)

    def test_queue_respects_max_concurrent(self, store, mock_pool):
        block = threading.Event()
        started = threading.Event()
        runner = _make_flow_runner([TaskStatus.COMPLETED], event=started, block_event=block)
        qm = QueueManager(
            store, mock_pool, max_concurrent=1, poll_interval=0.05, flow_runner=runner
        )

        qm.submit(1, "Task 1")
        qm.submit(2, "Task 2")
        qm.start()

        assert started.wait(timeout=5.0), "first task never started"
        # Give poll loop a couple cycles to potentially launch task 2
        time.sleep(0.2)

        assert qm.running_count == 1, "should only have 1 running with max_concurrent=1"

        # Release the block so tasks can finish
        block.set()
        qm.shutdown(timeout=5.0)

    def test_queue_flow_runner_exception(self, store, mock_pool):
        started = threading.Event()
        runner = _make_failing_flow_runner(event=started)
        qm = QueueManager(store, mock_pool, poll_interval=0.05, flow_runner=runner)

        qm.submit(1, "Task 1")
        qm.start()

        assert started.wait(timeout=5.0), "flow_runner was never called"

        # Wait for task to be marked failed
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            task = store.get_task(1)
            if task["status"] == TaskStatus.FAILED:
                break
            time.sleep(0.05)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.FAILED

        # Queue should still be alive — submit and run another task
        started2 = threading.Event()
        # Replace with a working runner for task 2
        runner2 = _make_flow_runner([TaskStatus.COMPLETED], event=started2)
        qm._flow_runner = runner2

        qm.submit(2, "Task 2")
        assert started2.wait(timeout=5.0), "queue died after exception"

        qm.shutdown(timeout=2.0)


# --- Unblock ---


class TestQueueUnblock:
    def test_queue_unblock_moves_to_queued(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Blocked task")
        store.update_task_status(1, TaskStatus.BLOCKED)

        qm.unblock(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.QUEUED

    def test_queue_unblock_merges_context(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Blocked task")
        store.update_task_status(1, TaskStatus.BLOCKED)
        store.save_checkpoint(
            1,
            {
                "context": {"existing_key": "value1"},
                "workspace_path": "/tmp/ws",
            },
        )

        qm.unblock(1, context={"new_key": "value2", "existing_key": "updated"})

        checkpoint = store.load_checkpoint(1)
        assert checkpoint is not None
        assert checkpoint["context"]["new_key"] == "value2"
        assert checkpoint["context"]["existing_key"] == "updated"
        assert checkpoint["workspace_path"] == "/tmp/ws"

    def test_queue_unblock_stopped_task(self, store, mock_pool):
        """Unblock a stopped task → QUEUED (stopped is resumable)."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Stopped task")
        store.update_task_status(1, TaskStatus.STOPPED)

        qm.unblock(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.QUEUED

    def test_queue_unblock_not_blocked_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Queued task")

        with pytest.raises(TaoError, match="cannot be unblocked"):
            qm.unblock(1)

    def test_queue_unblock_not_found_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)

        with pytest.raises(TaskNotFoundError):
            qm.unblock(999)

    def test_queue_unblock_without_checkpoint(self, store, mock_pool):
        """Unblock with context but no checkpoint — just moves to queued."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Blocked task")
        store.update_task_status(1, TaskStatus.BLOCKED)

        qm.unblock(1, context={"key": "value"})

        task = store.get_task(1)
        assert task["status"] == TaskStatus.QUEUED

    def test_queue_unblock_with_config_update(self, store, mock_pool):
        """Unblock with config updates the task config in DB."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Config update")
        store.update_task_status(1, TaskStatus.FAILED)

        qm.unblock(1, config={"step_configs": {"execute": {"model_spec": "sonnet"}}})

        task = store.get_task(1)
        assert task["status"] == TaskStatus.QUEUED
        assert task["config"]["step_configs"]["execute"]["model_spec"] == "sonnet"

    def test_queue_unblock_config_merges(self, store, mock_pool):
        """Config update merges into existing config, not replaces."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        store.create_task(1, "Merge test", config={"cwd": "/tmp", "step_configs": {"scope": {"model_spec": "opus"}}})
        store.update_task_status(1, TaskStatus.FAILED)

        qm.unblock(1, config={"step_configs": {"execute": {"model_spec": "sonnet"}}})

        task = store.get_task(1)
        assert task["config"]["cwd"] == "/tmp"  # preserved
        assert task["config"]["step_configs"]["execute"]["model_spec"] == "sonnet"  # updated


# --- Stop ---


class TestQueueStop:
    def test_queue_stop_task_running(self, store, mock_pool):
        block = threading.Event()
        started = threading.Event()
        runner = _make_flow_runner([TaskStatus.COMPLETED], event=started, block_event=block)
        qm = QueueManager(store, mock_pool, poll_interval=0.05, flow_runner=runner)

        qm.submit(1, "Task 1")
        qm.start()

        assert started.wait(timeout=5.0), "task never started"

        # Release the block so stop_task's join can complete
        block.set()
        qm.stop_task(1)

        qm.shutdown(timeout=2.0)

    def test_queue_stop_queued_task(self, store, mock_pool):
        """Stop a queued task → STOPPED directly."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.submit(1, "Task 1")

        qm.stop_task(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.STOPPED

    def test_queue_stop_blocked_task(self, store, mock_pool):
        """Stop a blocked task → STOPPED directly."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Blocked task")
        store.update_task_status(1, TaskStatus.BLOCKED)

        qm.stop_task(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.STOPPED

    def test_queue_stop_terminal_raises(self, store, mock_pool):
        """Stop a completed task → error."""
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Done task")
        store.update_task_status(1, TaskStatus.COMPLETED)

        with pytest.raises(TaoError, match="already terminal"):
            qm.stop_task(1)

    def test_queue_stop_not_found_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)

        with pytest.raises(TaskNotFoundError):
            qm.stop_task(999)


# --- Cancel ---


class TestQueueCancel:
    def test_queue_cancel_queued_task(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        qm.submit(1, "Task 1")

        qm.cancel_task(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.CANCELLED

    def test_queue_cancel_blocked_task(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Blocked task")
        store.update_task_status(1, TaskStatus.BLOCKED)

        qm.cancel_task(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.CANCELLED

    def test_queue_cancel_running_task(self, store, mock_pool):
        block = threading.Event()
        started = threading.Event()
        runner = _make_flow_runner([TaskStatus.STOPPED], event=started, block_event=block)
        qm = QueueManager(store, mock_pool, poll_interval=0.05, flow_runner=runner)

        qm.submit(1, "Task 1")
        qm.start()

        assert started.wait(timeout=5.0), "task never started"

        # Release the block so the runner finishes (sets STOPPED — non-terminal
        # would be overwritten, but STOPPED is terminal). We need cancel to
        # happen while the task is NOT yet terminal. Use a brief delay.
        def release():
            time.sleep(0.1)
            block.set()

        threading.Thread(target=release, daemon=True).start()
        qm.cancel_task(1)

        task = store.get_task(1)
        assert task["status"] == TaskStatus.CANCELLED
        qm.shutdown(timeout=2.0)

    def test_queue_cancel_terminal_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        create_task_in_store(store, task_id=1, title="Done task")
        store.update_task_status(1, TaskStatus.COMPLETED)

        with pytest.raises(TaoError, match="already terminal"):
            qm.cancel_task(1)

    def test_queue_cancel_not_found_raises(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)

        with pytest.raises(TaskNotFoundError):
            qm.cancel_task(999)


# --- Running count ---


class TestQueueRunningCount:
    def test_queue_running_count(self, store, mock_pool):
        qm = QueueManager(store, mock_pool, poll_interval=0.05)
        assert qm.running_count == 0

    def test_queue_running_count_with_task(self, store, mock_pool):
        block = threading.Event()
        started = threading.Event()
        runner = _make_flow_runner([TaskStatus.COMPLETED], event=started, block_event=block)
        qm = QueueManager(store, mock_pool, poll_interval=0.05, flow_runner=runner)

        qm.submit(1, "Task 1")
        qm.start()

        assert started.wait(timeout=5.0), "task never started"
        assert qm.running_count == 1

        block.set()

        # Wait for cleanup
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if qm.running_count == 0:
                break
            time.sleep(0.05)

        assert qm.running_count == 0
        qm.shutdown(timeout=2.0)
