# HTTP API

TAO can run as a local HTTP service, allowing any app on the same machine to submit tasks, check progress, and manage the queue over HTTP.

> **OpenAPI spec**: [`openapi.yaml`](../openapi.yaml) — import into Postman, Insomnia, or use with any OpenAPI-compatible tool.

## Starting the server

```bash
tao serve --http                            # localhost:8321 (default)
tao serve --http --port 9000                # custom port
tao serve --http --host 0.0.0.0 --port 9000 # expose to network (no auth!)
```

The HTTP server runs **in the same process** as the queue loop. This is required because `stop` uses in-memory threading events. The server uses `ThreadingHTTPServer` for concurrent request handling.

Default bind: `127.0.0.1` (localhost only). Use `--host 0.0.0.0` explicitly to expose to the network — there is no authentication.

## Web Monitor

`GET /` and `GET /monitor` serve the built-in monitoring dashboard.
No external dependencies — works offline.

## Endpoints

| Method | Path | Description | Status |
|--------|------|-------------|--------|
| POST | `/tasks` | Submit a task | 201 |
| GET | `/tasks` | List tasks | 200 |
| GET | `/tasks/{id}` | Get task status | 200 |
| DELETE | `/tasks/{id}` | Delete completed task | 200 |
| POST | `/tasks/{id}/stop` | Request graceful stop (non-terminal, resumable) | 202 |
| POST | `/tasks/{id}/cancel` | Cancel a task (terminal, cannot be resumed) | 200 |
| POST | `/tasks/{id}/unblock` | Resume blocked/stopped/failed task | 200 |
| POST | `/tasks/{id}/restart` | Restart from scratch | 200 |
| GET | `/tasks/{id}/traces` | Get execution traces | 200 |
| GET | `/tasks/{id}/summary` | Get aggregated metrics | 200 |
| GET | `/health` | Health check | 200 |

All requests and responses use `Content-Type: application/json`.

---

## POST /tasks — Submit

The request body uses **the same JSON format as task files** passed to `tao run`. The server extracts `title`, `body`, and optionally `task_id` — everything else (`cwd`, `cycle`, `scope`, `max_retries`, `policies`, `tools`, etc.) becomes the task config.

> **Note**: `body_file` is not supported via HTTP API — inline the body text directly. `body_file` only works with `tao run` (resolved relative to the JSON file).

```bash
curl -X POST http://localhost:8321/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Build login page",
    "body": "Create a login page with email/password auth",
    "cwd": "/path/to/project",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
      {"id": "plan", "type": "llm", "prompt": "Plan the implementation.", "model_spec": "opus@claude"},
      {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude"}
    ]
  }'
```

> **Windows (Git Bash)**: Single-quoted JSON in curl doesn't work in MINGW64. Use double quotes with escaping, or save the JSON to a file and use `curl ... -d @task.json`.

```json
{"task_id": 1, "status": "queued"}
```

`task_id` is **optional** — if omitted, the server auto-generates one and returns it. If provided, it must be unique.

### Full example with config

```bash
curl -X POST http://localhost:8321/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Build feature X",
    "body": "Implement the widget system",
    "cwd": "/path/to/project",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
      {"id": "plan", "type": "llm", "prompt": "Plan the implementation.", "model_spec": "sonnet@claude"},
      {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude", "failover": ["sonnet@claude"], "next": "validate"},
      {"id": "fix", "type": "llm", "prompt": "Fix the errors.", "model_spec": "sonnet@claude", "next": "validate"},
      {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "python -m pytest tests/"], "on_fail": "fix"}
    ],
    "max_retries": 3,
    "policies": {"batch_size": 5}
  }'
```

See [config-reference.md](config-reference.md) for the full config schema.

---

## GET /tasks — List

```bash
curl http://localhost:8321/tasks
curl http://localhost:8321/tasks?status=running
```

```json
{
  "tasks": [
    {"task_id": 1, "title": "Build feature X", "status": "running", "created_at": "...", "updated_at": "..."},
    {"task_id": 2, "title": "Review PR #42", "status": "queued", "created_at": "...", "updated_at": "..."}
  ]
}
```

Does not include `config` or `body` — use `GET /tasks/{id}` for full details.

---

## GET /tasks/{id} — Status

```bash
curl http://localhost:8321/tasks/1
```

```json
{
  "task_id": 1,
  "title": "Build feature X",
  "body": "Implement the widget system",
  "status": "running",
  "config": {},
  "created_at": "2026-03-19 10:00:00",
  "updated_at": "2026-03-19 10:01:00"
}
```

When the task is **blocked**, the response includes `blocked_reason`:

```json
{
  "task_id": 1,
  "status": "blocked",
  "blocked_reason": "Need approval: deploy to prod?",
  "...": "..."
}
```

Use this to decide what context to provide in the unblock call.

---

## DELETE /tasks/{id} — Delete

```bash
curl -X DELETE http://localhost:8321/tasks/1
```

```json
{"deleted": true}
```

Only tasks in a terminal or stopped state (`completed`, `failed`, `cancelled`, `stopped`) can be deleted. Deletes the task, its checkpoint, and all traces permanently.

Note: `stopped` is non-terminal (resumable via `/unblock`) but is also deletable.

Returns `409` if the task is still running, queued, or blocked.

---

## POST /tasks/{id}/stop — Stop

```bash
curl -X POST http://localhost:8321/tasks/1/stop
```

```json
{"task_id": 1, "message": "stop requested"}
```

Returns **202 Accepted** — the stop is *requested*, not *completed*. The flow checks the stop event at the next safe point (between steps or subtasks) and exits cleanly.

Works on any non-terminal task (queued, running, or blocked). `stopped` is non-terminal and resumable — use `/unblock` to re-queue it.

Poll `GET /tasks/{id}` to confirm the status changed to `stopped`.

---

## POST /tasks/{id}/cancel — Cancel

```bash
curl -X POST http://localhost:8321/tasks/1/cancel
```

```json
{"task_id": 1, "status": "cancelled"}
```

Cancels a task. Unlike stop, `cancelled` is **terminal** — the task cannot be resumed. Works on any non-terminal task (queued, running, blocked, or stopped). If the task has a running thread, waits for the active step to finish before marking cancelled.

Returns `409` if the task is already in a terminal state (`completed`, `failed`, or `cancelled`).

---

## POST /tasks/{id}/unblock — Unblock

```bash
curl -X POST http://localhost:8321/tasks/1/unblock \
  -H "Content-Type: application/json" \
  -d '{"context": {"answer": "Yes, deploy to prod"}}'
```

```json
{"task_id": 1, "status": "queued"}
```

Works on blocked, stopped, and failed tasks — all three states can be unblocked with this endpoint. The `context` dict is optional. If no body is sent (or body is empty), the task is unblocked without additional context. If `context` is provided, it is merged into the checkpoint context before resuming. An optional `config` dict can be passed to update the task's config at the same time (e.g. to increase `max_retries` before retrying). The task returns to `queued` and the queue resumes it from its checkpoint.

---

---

## POST /tasks/{id}/restart — Restart

```bash
curl -X POST http://localhost:8321/tasks/1/restart
```

```json
{"task_id": 1, "status": "queued"}
```

Restarts a task from scratch — clears all checkpoints, traces, and subtask state. The task returns to `queued` with a clean slate. Works on any non-running task (completed, failed, stopped, cancelled, blocked, queued).

Returns `409` if the task is currently running (stop it first).

---

## GET /tasks/{id}/traces — Traces

```bash
curl http://localhost:8321/tasks/1/traces
```

```json
{
  "task_id": 1,
  "traces": [
    {
      "id": 1,
      "subtask_index": 0,
      "role": "plan",
      "model": "sonnet",
      "tokens_in": 500,
      "tokens_out": 200,
      "cost_usd": 0.01,
      "elapsed_s": 3.2,
      "success": true,
      "attempt": 1,
      "created_at": "2026-03-19 10:00:05"
    }
  ]
}
```

---

## GET /tasks/{id}/summary — Summary

```bash
curl http://localhost:8321/tasks/1/summary
```

```json
{
  "task_id": 1,
  "total_cost_usd": 0.15,
  "total_elapsed_s": 45.2,
  "total_tokens_in": 5000,
  "total_tokens_out": 3000,
  "steps_succeeded": 4,
  "steps_failed": 0,
  "trace_count": 4
}
```

---

## GET /health — Health check

```bash
curl http://localhost:8321/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "queue_running": 2,
  "queue_max_concurrent": 5
}
```

---

## Error format

All errors return a consistent JSON structure:

```json
{
  "error": "task 1 is not blocked (status: running)",
  "code": "INVALID_TASK_STATE",
  "current_status": "running"
}
```

| HTTP | Code | When |
|------|------|------|
| 400 | `BAD_REQUEST` | Invalid task_id format in URL |
| 400 | `INVALID_JSON` | Request body is not valid JSON |
| 400 | `MISSING_FIELD` | Required field missing (title) |
| 404 | `TASK_NOT_FOUND` | task_id doesn't exist |
| 409 | `TASK_ALREADY_EXISTS` | POST /tasks with duplicate task_id |
| 409 | `INVALID_TASK_STATE` | Operation not valid for current state (includes `current_status`) |
| 500 | `INTERNAL_ERROR` | Unexpected server error |

## Implementation notes

- **Server**: `http.server.ThreadingHTTPServer` (stdlib) — no framework dependencies
- **Thread safety**: Engine is already thread-safe (SQLite WAL + retry, Queue uses locks)
- **Same process**: HTTP server and queue run in the same process — required for stop to work
- **No auth**: Local service. Use a reverse proxy (nginx, caddy) if you need authentication
- **No CORS**: Not needed for backend-to-backend. Add `--cors` flag if a browser UI needs access

## Implementation status

All components are implemented and tested (265 tests, 26 for the HTTP API).

| Component | Status | Location |
|-----------|--------|----------|
| `src/server.py` | done | HTTP handler, 9 endpoints, ThreadingHTTPServer |
| `cli.py` `--http`, `--host`, `--port` | done | `tao serve --http [--host H --port P]` |
| `Store.delete_task()` | done | DELETE from traces + checkpoints + tasks in transaction |
| `Engine.delete()` | done | Validates terminal state, delegates to store |
| Auto-generate `task_id` | done | `Store.create_task_auto_id()` via `cursor.lastrowid` |
| `blocked_reason` in GET /tasks/{id} | done | `Engine.get_status()` merges from checkpoint |
| Strip `config`/`body` from list | done | Handler filters `list_tasks()` output |
