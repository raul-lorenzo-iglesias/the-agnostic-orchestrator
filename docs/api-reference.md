# API Reference

The `Engine` class in `tao.api` is the public Python API. All CLI commands are thin wrappers over Engine methods.

## `Engine`

```python
from src.api import Engine
```

### Constructor

```python
Engine(
    config: dict | None = None,
    config_path: str | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `config` | dict \| None | `None` | Configuration dict (same structure as parsed TOML). Takes precedence over `config_path`. |
| `config_path` | str \| None | `None` | Path to a TOML config file. Ignored if `config` is given. |

If both are `None`, uses empty config (in-memory DB, no providers).

Supports context manager protocol:

```python
with Engine(config_path="tao.toml") as engine:
    ...
# engine.close() called automatically
```

---

## Task Lifecycle

### `engine.submit()`

```python
engine.submit(
    task_id: int | None = None,
    title: str = "",
    body: str = "",
    *,
    config: dict | None = None,
) -> int
```

Submit a task to the queue with status `queued`.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | int \| None | no | Unique task identifier. If `None`, auto-assigns an ID. |
| `title` | str | yes | Task title. |
| `body` | str | no | Task description/body. |
| `config` | dict | no | Task-level flow config. Key fields: `cwd` (working directory, required for scoped tasks), `scope` (model_spec for the scope step), `cycle` (array of step dicts with `id`, `type`, `model_spec`/`commands`, and optional `next`/`on_fail`), `max_retries`, `workspace`, `hooks`, `policies`. See [config-reference.md](config-reference.md). |

**Returns**: `int` — the task_id (provided or auto-generated).

**Raises**: `StoreError` if `task_id` already exists.

**Example:**

```python
task_id = engine.submit(
    title="Build login page",
    body="Create a login page with email/password auth",
    config={
        "cwd": "/path/to/project",
        "scope": {"model_spec": "sonnet@claude"},
        "cycle": [
            {"id": "plan", "type": "llm", "prompt": "Plan the implementation.", "model_spec": "opus@claude"},
            {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude"},
        ],
    },
)
```

---

### `engine.run_flow()`

```python
engine.run_flow(task_id: int) -> TaskStatus
```

Run a task's flow synchronously, bypassing the queue. The task must already exist in the store (call `submit()` first).

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | int | Task ID to run. |

**Returns**: `TaskStatus` — one of `"completed"`, `"failed"`, `"blocked"`, `"stopped"`.

**Raises**: `TaskNotFoundError` if `task_id` does not exist.

---

### `engine.unblock()`

```python
engine.unblock(
    task_id: int,
    context: dict | None = None,
    config: dict | None = None,
) -> None
```

Resume a blocked, stopped, or failed task. Optionally merge new context and/or update the task config.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | int | yes | Task ID to unblock. |
| `context` | dict | no | Key-value pairs merged into the checkpoint context. Passed to subsequent steps via `human_message`. |
| `config` | dict | no | If provided, replaces the task's stored config before resuming. Useful to adjust policies (e.g. increase `max_retries`) before retrying. |

**Raises**:
- `TaskNotFoundError` if `task_id` does not exist.
- `TaoError` if task cannot be unblocked (e.g. already terminal).

---

### `engine.stop()`

```python
engine.stop(task_id: int) -> None
```

Request graceful stop for a task. Works on any non-terminal task (queued, running, or blocked):
- Running: finishes the active step, saves checkpoint, then marks `stopped`.
- Queued/Blocked: marks `stopped` immediately.

`stopped` is **non-terminal** — use `engine.unblock()` to resume.

**Note**: For running tasks, this only works when dispatched by the queue (via `serve()`). Tasks launched directly with `run_flow()` run synchronously and cannot be stopped via this method.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | int | Task ID to stop. |

**Raises**: `TaoError` if task is already in a terminal status.

---

### `engine.cancel()`

```python
engine.cancel(task_id: int) -> None
```

Cancel a task permanently. Works on any non-terminal task. Unlike `stop()`, `cancelled` is **terminal** — the task cannot be resumed. If the task has a running thread, waits for the active step to finish before marking cancelled.

| Parameter | Type | Description |
|-----------|------|-------------|
| `task_id` | int | Task ID to cancel. |

**Raises**:
- `TaskNotFoundError` if `task_id` does not exist.
- `TaoError` if task is already in a terminal status.

---

## Observability

### `engine.get_status()`

```python
engine.get_status(task_id: int) -> dict
```

Fetch task record with parsed config.

**Returns**:
```python
{
    "task_id": 1,
    "title": "Build feature X",
    "body": "...",
    "status": "completed",
    "config": {...},
    "created_at": "2025-03-19 10:00:00",
    "updated_at": "2025-03-19 10:05:00",
}
```

**Raises**: `TaskNotFoundError` if `task_id` does not exist.

---

### `engine.get_traces()`

```python
engine.get_traces(task_id: int) -> list[dict]
```

Get all execution traces for a task, ordered by insertion.

**Returns**: List of trace dicts:
```python
[
    {
        "id": 1,
        "task_id": 1,
        "subtask_index": 0,
        "role": "implement",
        "model": "opus",
        "tokens_in": 1500,
        "tokens_out": 800,
        "cost_usd": 0.05,
        "elapsed_s": 12.3,
        "success": True,
        "attempt": 1,
        "created_at": "2025-03-19 10:01:00",
    }
]
```

---

### `engine.summary()`

```python
engine.summary(task_id: int) -> dict
```

Get aggregated metrics across all traces for a task.

**Returns**:
```python
{
    "task_id": 1,
    "total_cost_usd": 0.15,
    "total_elapsed_s": 45.2,
    "total_tokens_in": 5000,
    "total_tokens_out": 3000,
    "steps_succeeded": 4,
    "steps_failed": 0,
    "trace_count": 4,
}
```

Returns zeroed values if no traces exist (not an error).

---

### `engine.list_tasks()`

```python
engine.list_tasks(status: TaskStatus | str | None = None) -> list[dict]
```

List all tasks, optionally filtered by status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `status` | TaskStatus \| str \| None | no | Filter by status (e.g., `"queued"`, `"running"`). `None` returns all. |

**Returns**: List of task dicts (same structure as `get_status()`), ordered by `created_at`.

---

## Server

### `engine.serve()`

```python
engine.serve() -> None
```

Start the queue polling loop and block until `KeyboardInterrupt` (Ctrl+C). The queue dispatches tasks respecting `max_concurrent`.

Internally: starts a daemon thread for polling, then blocks the main thread. On interrupt, calls `shutdown()` on the queue manager.

---

## Lifecycle

### `engine.close()`

```python
engine.close() -> None
```

Shut down the queue and close the database connection. Idempotent — safe to call multiple times.

---

## Standalone functions

### `load_config()`

```python
from src.api import load_config

config = load_config("tao.toml")  # -> dict
```

Load and parse a TOML configuration file.

**Raises**: `TaoError` if file not found or contains invalid TOML.

---

## CLI Commands

All CLI commands map to Engine methods:

| Command | Engine method | Description |
|---------|--------------|-------------|
| `tao serve` | `engine.serve()` | Start queue loop |
| `tao submit --title "..." [--cwd ...] [--cycle '[]']` | `engine.submit()` | Add task |
| `tao unblock N [--context '{}']` | `engine.unblock()` | Resume blocked/stopped/failed task |
| `tao stop N` | `engine.stop()` | Graceful stop (non-terminal, resumable) |
| `tao cancel N` | `engine.cancel()` | Cancel task (terminal, cannot be resumed) |
| `tao status N` | `engine.get_status()` | Task state + metadata |
| `tao traces N` | `engine.get_traces()` | Execution trace log |
| `tao summary N` | `engine.summary()` | Aggregated metrics |
| `tao llm` | — | LLM service for subprocess steps. JSON stdin/stdout bridge to provider pool. See [step protocol](step-protocol.md). |

### Global flag

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | `tao.toml` | Path to TOML config file. |
