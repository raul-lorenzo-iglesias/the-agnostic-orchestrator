"""Tests for the HTTP API (src/server.py).

Each test uses a real Engine + SQLite store in a temp directory, with an HTTP
server running in a daemon thread on a random port. No mocks — exercises the
full request → handler → engine → store chain.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import src
from src.api import Engine
from src.models import TaskStatus
from src.server import start_http_server


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


@pytest.fixture
def api(tmp_path):
    db_path = str(tmp_path / "test.db")
    engine = Engine(config={"engine": {"db_path": db_path}})

    port = _find_free_port()
    t = threading.Thread(
        target=start_http_server, args=(engine, "127.0.0.1", port), daemon=True
    )
    t.start()
    time.sleep(0.3)

    class Api:
        base = f"http://127.0.0.1:{port}"
        _engine = engine

        def get(self, path):
            return json.loads(urlopen(f"{self.base}{path}").read())

        def post(self, path, body=None):
            data = json.dumps(body).encode() if body else None
            req = Request(
                f"{self.base}{path}",
                method="POST",
                data=data,
                headers={"Content-Type": "application/json"} if data else {},
            )
            return json.loads(urlopen(req).read())

        def delete(self, path):
            req = Request(f"{self.base}{path}", method="DELETE")
            return json.loads(urlopen(req).read())

        def request(self, method, path, body=None):
            """Raw request that returns (status_code, response_dict)."""
            data = json.dumps(body).encode() if body else None
            headers = {"Content-Type": "application/json"} if data else {}
            req = Request(
                f"{self.base}{path}", method=method, data=data, headers=headers
            )
            try:
                resp = urlopen(req)
                return resp.status, json.loads(resp.read())
            except HTTPError as e:
                return e.code, json.loads(e.read())

    yield Api()
    engine.close()


# ---------------------------------------------------------------------------
# Helper — submit a task via the API and return the task_id
# ---------------------------------------------------------------------------

def _submit(api, *, task_id=None, title="test task"):
    """Submit a task through the HTTP API. Returns the response dict."""
    body = {"title": title}
    if task_id is not None:
        body["task_id"] = task_id
    return api.post("/tasks", body)


# ===========================================================================
# Health
# ===========================================================================


def test_server_health(api):
    """GET /health returns status, version, queue_running, queue_max_concurrent."""
    status, data = api.request("GET", "/health")
    assert status == 200
    assert data["status"] == "ok"
    assert data["version"] == src.__version__
    assert "queue_running" in data
    assert "queue_max_concurrent" in data


# ===========================================================================
# Submit
# ===========================================================================


def test_server_submit_auto_id(api):
    """POST /tasks without task_id auto-generates an integer ID."""
    status, data = api.request("POST", "/tasks", {
        "title": "auto id task",
    })
    assert status == 201
    assert isinstance(data["task_id"], int)
    assert data["status"] == "queued"


def test_server_submit_explicit_id(api):
    """POST /tasks with task_id=42 returns that exact ID."""
    status, data = api.request("POST", "/tasks", {
        "title": "explicit id task",
        "task_id": 42,
    })
    assert status == 201
    assert data["task_id"] == 42


def test_server_submit_duplicate(api):
    """Submitting the same task_id twice returns 409 TASK_ALREADY_EXISTS."""
    _submit(api, task_id=100, title="first")
    status, data = api.request("POST", "/tasks", {
        "title": "second",
        "task_id": 100,
    })
    assert status == 409
    assert data["code"] == "TASK_ALREADY_EXISTS"


def test_server_submit_missing_title(api):
    """Missing title field returns 400 MISSING_FIELD."""
    status, data = api.request("POST", "/tasks", {"body": "no title here"})
    assert status == 400
    assert data["code"] == "MISSING_FIELD"


def test_server_submit_with_cwd(api, tmp_path):
    """Submit with cwd sets it in config."""
    status, data = api.request("POST", "/tasks", {
        "title": "with cwd",
        "cwd": str(tmp_path),
    })
    assert status == 201


# ===========================================================================
# List
# ===========================================================================


def test_server_list_empty(api):
    """No tasks → {"tasks": []}."""
    data = api.get("/tasks")
    assert data == {"tasks": []}


def test_server_list_tasks(api):
    """Submit 2 tasks, list returns 2 items without config/body/subtasks."""
    _submit(api, task_id=1, title="task one")
    _submit(api, task_id=2, title="task two")
    data = api.get("/tasks")
    assert len(data["tasks"]) == 2
    for item in data["tasks"]:
        assert "config" not in item
        assert "body" not in item
        assert "subtasks" not in item
        assert "task_id" in item
        assert "status" in item


def test_server_list_filter_status(api):
    """Filter by status=queued returns only queued tasks."""
    _submit(api, task_id=10, title="queued task")
    _submit(api, task_id=11, title="another queued")
    # Manually mark one as completed
    api._engine._store.update_task_status(11, TaskStatus.COMPLETED)

    data = api.get("/tasks?status=queued")
    assert len(data["tasks"]) == 1
    assert data["tasks"][0]["task_id"] == 10


# ===========================================================================
# Get
# ===========================================================================


def test_server_get_task(api):
    """Submit then get — verify all expected fields present."""
    _submit(api, task_id=5, title="get me")
    data = api.get("/tasks/5")
    assert data["task_id"] == 5
    assert data["title"] == "get me"
    assert data["status"] in ("queued", TaskStatus.QUEUED)
    assert "config" in data
    assert "subtasks" in data
    assert "created_at" in data
    assert "updated_at" in data


def test_server_get_task_not_found(api):
    """GET /tasks/99999 → 404 TASK_NOT_FOUND."""
    status, data = api.request("GET", "/tasks/99999")
    assert status == 404
    assert data["code"] == "TASK_NOT_FOUND"


def test_server_get_task_invalid_id(api):
    """GET /tasks/abc → 404 (regex doesn't match \\d+, so route not found)."""
    status, data = api.request("GET", "/tasks/abc")
    assert status == 404


# ===========================================================================
# Delete
# ===========================================================================


def test_server_delete_task(api):
    """Submit, set to completed via store, delete → 200."""
    _submit(api, task_id=20, title="to delete")
    api._engine._store.update_task_status(20, TaskStatus.COMPLETED)
    status, data = api.request("DELETE", "/tasks/20")
    assert status == 200
    assert data["deleted"] is True

    # Verify it's gone
    status2, data2 = api.request("GET", "/tasks/20")
    assert status2 == 404


def test_server_delete_running_task(api):
    """Submit (queued), try delete → 409 INVALID_TASK_STATE."""
    _submit(api, task_id=21, title="still queued")
    status, data = api.request("DELETE", "/tasks/21")
    assert status == 409
    assert data["code"] == "INVALID_TASK_STATE"


def test_server_delete_not_found(api):
    """DELETE /tasks/99999 → 404."""
    status, data = api.request("DELETE", "/tasks/99999")
    assert status == 404
    assert data["code"] == "TASK_NOT_FOUND"


# ===========================================================================
# Stop
# ===========================================================================


def test_server_stop_queued_task(api):
    """Submit (queued), stop → 202 (queued tasks can be stopped directly)."""
    _submit(api, task_id=30, title="queued to stop")
    status, data = api.request("POST", "/tasks/30/stop")
    assert status == 202

    task = api.get("/tasks/30")
    assert task["status"] in ("stopped", TaskStatus.STOPPED)


def test_server_stop_terminal_task(api):
    """Stop a completed task → 409."""
    _submit(api, task_id=31, title="already done")
    api._engine._store.update_task_status(31, TaskStatus.COMPLETED)
    status, data = api.request("POST", "/tasks/31/stop")
    assert status == 409
    assert data["code"] == "INVALID_TASK_STATE"


def test_server_stop_not_found(api):
    """POST /tasks/99999/stop → 404 TASK_NOT_FOUND."""
    status, data = api.request("POST", "/tasks/99999/stop")
    assert status == 404
    assert data["code"] == "TASK_NOT_FOUND"


# ===========================================================================
# Cancel
# ===========================================================================


def test_server_cancel_queued_task(api):
    """Submit (queued), cancel → 200 cancelled."""
    _submit(api, task_id=35, title="to cancel")
    status, data = api.request("POST", "/tasks/35/cancel")
    assert status == 200
    assert data["status"] == "cancelled"

    # Verify it's cancelled
    task = api.get("/tasks/35")
    assert task["status"] in ("cancelled", TaskStatus.CANCELLED)


def test_server_cancel_terminal_task(api):
    """Cancel a completed task → 409."""
    _submit(api, task_id=36, title="already done")
    api._engine._store.update_task_status(36, TaskStatus.COMPLETED)
    status, data = api.request("POST", "/tasks/36/cancel")
    assert status == 409
    assert data["code"] == "INVALID_TASK_STATE"


def test_server_cancel_not_found(api):
    """POST /tasks/99999/cancel → 404."""
    status, data = api.request("POST", "/tasks/99999/cancel")
    assert status == 404
    assert data["code"] == "TASK_NOT_FOUND"


def test_server_delete_cancelled_task(api):
    """Cancelled task is terminal — can be deleted."""
    _submit(api, task_id=37, title="cancel then delete")
    api.post("/tasks/37/cancel")
    status, data = api.request("DELETE", "/tasks/37")
    assert status == 200
    assert data["deleted"] is True


# ===========================================================================
# Unblock
# ===========================================================================


def test_server_unblock_not_blocked(api):
    """Submit (queued), try unblock → 409."""
    _submit(api, task_id=40, title="not blocked")
    status, data = api.request("POST", "/tasks/40/unblock")
    assert status == 409
    assert data["code"] == "INVALID_TASK_STATE"


def test_server_unblock_with_context(api):
    """Manually set to blocked + checkpoint, unblock with context → 200."""
    _submit(api, task_id=41, title="blocked task")
    store = api._engine._store
    store.update_task_status(41, TaskStatus.BLOCKED)
    store.save_checkpoint(41, {
        "blocked_reason": "needs input",
        "context": {"existing": "data"},
    })

    status, data = api.request("POST", "/tasks/41/unblock", {
        "context": {"user_response": "go ahead"},
    })
    assert status == 200
    assert data["task_id"] == 41
    assert data["status"] == "queued"

    # Verify context was merged into checkpoint
    checkpoint = store.load_checkpoint(41)
    assert checkpoint["context"]["existing"] == "data"
    assert checkpoint["context"]["user_response"] == "go ahead"


def test_server_unblock_no_body(api):
    """Manually set to blocked, POST with no body → 200 (empty body allowed)."""
    _submit(api, task_id=42, title="blocked no body")
    store = api._engine._store
    store.update_task_status(42, TaskStatus.BLOCKED)
    store.save_checkpoint(42, {"blocked_reason": "waiting"})

    status, data = api.request("POST", "/tasks/42/unblock")
    assert status == 200
    assert data["status"] == "queued"


def test_server_unblock_not_found(api):
    """POST /tasks/99999/unblock → 404 TASK_NOT_FOUND."""
    status, data = api.request("POST", "/tasks/99999/unblock")
    assert status == 404
    assert data["code"] == "TASK_NOT_FOUND"


# ===========================================================================
# Traces + Summary
# ===========================================================================


def test_server_traces_empty(api):
    """No traces yet → {"task_id": N, "traces": []}."""
    _submit(api, task_id=50, title="no traces")
    data = api.get("/tasks/50/traces")
    assert data["task_id"] == 50
    assert data["traces"] == []


def test_server_summary_empty(api):
    """No traces → zeroed summary."""
    _submit(api, task_id=51, title="no traces summary")
    data = api.get("/tasks/51/summary")
    assert data["task_id"] == 51
    assert data["total_cost_usd"] == 0.0
    assert data["total_elapsed_s"] == 0.0
    assert data["total_tokens_in"] == 0
    assert data["total_tokens_out"] == 0
    assert data["steps_succeeded"] == 0
    assert data["steps_failed"] == 0
    assert data["trace_count"] == 0


# ===========================================================================
# Blocked reason
# ===========================================================================


def test_server_blocked_reason_in_status(api):
    """Blocked task with checkpoint blocked_reason → GET returns it."""
    _submit(api, task_id=60, title="blocked with reason")
    store = api._engine._store
    store.update_task_status(60, TaskStatus.BLOCKED)
    store.save_checkpoint(60, {"blocked_reason": "needs human review"})

    data = api.get("/tasks/60")
    assert data["status"] in ("blocked", TaskStatus.BLOCKED)
    assert data["blocked_reason"] == "needs human review"


# ===========================================================================
# Monitor
# ===========================================================================


def test_server_monitor_root(api):
    """GET / returns 200 with text/html containing <html."""
    resp = urlopen(f"{api.base}/")
    assert resp.status == 200
    content_type = resp.headers.get("Content-Type", "")
    assert "text/html" in content_type
    body = resp.read().decode()
    assert "<html" in body


def test_server_monitor_path(api):
    """GET /monitor returns 200 with text/html."""
    resp = urlopen(f"{api.base}/monitor")
    assert resp.status == 200
    content_type = resp.headers.get("Content-Type", "")
    assert "text/html" in content_type


def test_server_api_routes_unaffected(api):
    """GET /tasks still returns application/json after monitor routes added."""
    resp = urlopen(f"{api.base}/tasks")
    assert resp.status == 200
    content_type = resp.headers.get("Content-Type", "")
    assert "application/json" in content_type
