"""HTTP server — exposes Engine over REST API.

Runs in the same process as the queue loop. Uses ThreadingHTTPServer
for concurrent request handling. No external dependencies.
"""

from __future__ import annotations

import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import src
from src.models import StoreError, TaoError, TaskNotFoundError, TaskStatus

logger = logging.getLogger(__name__)


class TaoHandler(BaseHTTPRequestHandler):
    """HTTP request handler for TAO API.

    Subclassed by the factory so ``_engine`` is set as a class variable.
    """

    _engine: Any  # tao.api.Engine — set by make_handler()

    # --- Route table ---
    # Each entry: (HTTP method, compiled regex, handler method name)
    # Handler receives (match,) where match is the regex Match object.
    _routes: list[tuple[str, re.Pattern[str], str]] = [
        ("POST", re.compile(r"^/tasks$"), "_handle_submit"),
        ("GET", re.compile(r"^/tasks$"), "_handle_list"),
        ("GET", re.compile(r"^/tasks/(\d+)$"), "_handle_get"),
        ("DELETE", re.compile(r"^/tasks/(\d+)$"), "_handle_delete"),
        ("POST", re.compile(r"^/tasks/(\d+)/stop$"), "_handle_stop"),
        ("POST", re.compile(r"^/tasks/(\d+)/cancel$"), "_handle_cancel"),
        ("POST", re.compile(r"^/tasks/(\d+)/unblock$"), "_handle_unblock"),
        ("POST", re.compile(r"^/tasks/(\d+)/restart$"), "_handle_restart"),
        ("GET", re.compile(r"^/tasks/(\d+)/traces$"), "_handle_traces"),
        ("GET", re.compile(r"^/tasks/(\d+)/summary$"), "_handle_summary"),
        ("GET", re.compile(r"^/health$"), "_handle_health"),
        ("GET", re.compile(r"^/$"), "_handle_monitor"),
        ("GET", re.compile(r"^/monitor$"), "_handle_monitor"),
    ]

    # Suppress default access log — we log through the standard logger.
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("HTTP %s %s", self.command, self.path)

    # --- HTTP method dispatchers ---

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    # --- Routing ---

    def _route(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        for route_method, pattern, handler_name in self._routes:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                handler = getattr(self, handler_name)
                try:
                    handler(m)
                except TaskNotFoundError as e:
                    self._send_error(404, str(e), "TASK_NOT_FOUND")
                except StoreError as e:
                    self._send_error(409, str(e), "TASK_ALREADY_EXISTS")
                except TaoError as e:
                    # Extract current_status from the error message if possible
                    self._send_error(409, str(e), "INVALID_TASK_STATE")
                except json.JSONDecodeError as e:
                    self._send_error(400, f"invalid JSON: {e}", "INVALID_JSON")
                except ValueError as e:
                    self._send_error(400, str(e), "BAD_REQUEST")
                except Exception:
                    logger.exception("unhandled error on %s %s", method, self.path)
                    self._send_error(500, "internal server error", "INTERNAL_ERROR")
                return

        self._send_error(404, f"not found: {method} {path}", "BAD_REQUEST")

    # --- Endpoint handlers ---

    def _handle_submit(self, m: re.Match[str]) -> None:
        body = self._read_json_body()
        if body is None:
            return  # error already sent

        if not body.get("title"):
            self._send_error(400, "missing required field: title", "MISSING_FIELD")
            return

        task_id = body.pop("task_id", None)
        title = body.pop("title")
        task_body = body.pop("body", "")
        body.pop("body_file", None)  # not supported via HTTP, ignore

        # Detect nested {"config": {...}} wrapper — common mistake when users
        # follow the Python API signature instead of the flat task JSON format.
        if "config" in body and isinstance(body["config"], dict):
            nested = body.pop("config")
            # If the nested config has task-level keys (cwd, cycle), unwrap it
            if "cwd" in nested or "cycle" in nested:
                body.update(nested)
                logger.warning(
                    "HTTP POST /tasks: unwrapped nested 'config' object — "
                    "use flat format instead (cwd, cycle, etc. at top level)"
                )

        # Everything remaining becomes config (cwd, cycle, scope, policies, etc.)
        config = body

        if task_id is not None:
            if not isinstance(task_id, int) or task_id < 1:
                self._send_error(400, "task_id must be a positive integer", "BAD_REQUEST")
                return

        task_id = self._engine.submit(task_id, title, task_body, config=config)
        logger.info("HTTP POST /tasks → submitted task %d", task_id)
        self._send_json(201, {"task_id": task_id, "status": "queued"})

    def _handle_list(self, m: re.Match[str]) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        status_filter = qs.get("status", [None])[0]

        if status_filter is not None:
            try:
                TaskStatus(status_filter)
            except ValueError:
                self._send_error(400, f"invalid status: {status_filter}", "BAD_REQUEST")
                return

        tasks = self._engine.list_tasks(status=status_filter)
        # Strip config, body, and subtasks from list response
        stripped = []
        for t in tasks:
            stripped.append({
                k: v for k, v in t.items() if k not in ("config", "body", "subtasks")
            })
        self._send_json(200, {"tasks": stripped})

    def _handle_get(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        # Engine.get_status() already merges blocked_reason from checkpoint
        task = self._engine.get_status(task_id)
        self._send_json(200, task)

    def _handle_delete(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        self._engine.delete(task_id)
        logger.info("HTTP DELETE /tasks/%d → deleted", task_id)
        self._send_json(200, {"deleted": True})

    def _handle_stop(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        self._engine.stop(task_id)
        logger.info("HTTP POST /tasks/%d/stop → stop requested", task_id)
        self._send_json(202, {"task_id": task_id, "message": "stop requested"})

    def _handle_cancel(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        self._engine.cancel(task_id)
        logger.info("HTTP POST /tasks/%d/cancel → cancelled", task_id)
        self._send_json(200, {"task_id": task_id, "status": "cancelled"})

    def _handle_unblock(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        body = self._read_json_body(allow_empty=True)
        if body is None:
            body = {}
        context = body.get("context")
        config = body.get("config")
        self._engine.unblock(task_id, context, config=config)
        logger.info("HTTP POST /tasks/%d/unblock → queued", task_id)
        self._send_json(200, {"task_id": task_id, "status": "queued"})

    def _handle_restart(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        self._engine.restart(task_id)
        logger.info("HTTP POST /tasks/%d/restart → queued", task_id)
        self._send_json(200, {"task_id": task_id, "status": "queued"})

    def _handle_traces(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        # Verify the task exists (raises TaskNotFoundError if not)
        self._engine.get_status(task_id)
        traces = self._engine.get_traces(task_id)
        self._send_json(200, {"task_id": task_id, "traces": traces})

    def _handle_summary(self, m: re.Match[str]) -> None:
        task_id = self._parse_task_id(m.group(1))
        if task_id is None:
            return
        # Verify the task exists (raises TaskNotFoundError if not)
        self._engine.get_status(task_id)
        summary = self._engine.summary(task_id)
        self._send_json(200, summary)

    def _handle_monitor(self, m: re.Match[str]) -> None:
        html_path = Path(__file__).parent / "static" / "monitor.html"
        if not html_path.exists():
            self._send_error(404, "monitor.html not found", "NOT_FOUND")
            return
        body = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self, m: re.Match[str]) -> None:
        queue = self._engine._queue
        self._send_json(200, {
            "status": "ok",
            "version": src.__version__,
            "queue_running": queue.running_count,
            "queue_max_concurrent": queue._max_concurrent,
        })

    # --- Helpers ---

    def _parse_task_id(self, raw: str) -> int | None:
        """Parse and validate a task_id from a URL path segment.

        Returns the int task_id, or None if invalid (error already sent).
        """
        try:
            task_id = int(raw)
        except ValueError:
            self._send_error(
                400, "invalid task_id: must be a positive integer", "BAD_REQUEST"
            )
            return None
        if task_id < 1:
            self._send_error(
                400, "invalid task_id: must be a positive integer", "BAD_REQUEST"
            )
            return None
        return task_id

    def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any] | None:
        """Read and parse JSON from the request body.

        Returns the parsed dict, or None if parsing fails (error already sent).
        When ``allow_empty`` is True, returns ``{}`` for missing/empty body.
        """
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            if allow_empty:
                return {}
            self._send_error(400, "request body is empty", "INVALID_JSON")
            return None

        raw = self.rfile.read(length)
        if not raw.strip():
            if allow_empty:
                return {}
            self._send_error(400, "request body is empty", "INVALID_JSON")
            return None

        data = json.loads(raw)
        if not isinstance(data, dict):
            self._send_error(400, "request body must be a JSON object", "INVALID_JSON")
            return None
        return data

    def _send_json(self, status_code: int, data: Any) -> None:
        """Send a JSON response."""
        body = json.dumps(data, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(
        self,
        status_code: int,
        message: str,
        code: str,
        **extra: Any,
    ) -> None:
        """Send a JSON error response."""
        payload: dict[str, Any] = {"error": message, "code": code}
        payload.update(extra)
        self._send_json(status_code, payload)

def make_handler(engine: Any) -> type[TaoHandler]:
    """Create a handler class bound to the given Engine instance."""

    class Handler(TaoHandler):
        _engine = engine

    return Handler


def start_http_server(
    engine: Any,
    host: str = "127.0.0.1",
    port: int = 8321,
) -> None:
    """Start the HTTP server. Blocks until interrupted."""
    handler_cls = make_handler(engine)
    server = ThreadingHTTPServer((host, port), handler_cls)
    logger.info("HTTP server listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
