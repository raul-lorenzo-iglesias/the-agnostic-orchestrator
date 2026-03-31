# TAO — The Agnostic Orchestrator

Python >= 3.11 task orchestrator. Stdlib-only core — no Django, SQLAlchemy, Pydantic, or attrs.
LLM-direct execution by default — TAO calls the LLM provider directly. Subprocess steps are the escape hatch.

## Commands

```
pip install -e ".[dev]"    # install with dev deps
make test                  # pytest tests/ -v
make lint                  # ruff check src/ tests/
make format                # ruff format + ruff check --fix
```

## Module dependency DAG

Imports flow **down** this list. Never import upward. No circular imports.

```
models         <- stdlib only (no internal imports)
fmt            <- (stdlib only — CLI formatting)
store          <- models
policy         <- models
step_runner    <- models
gates          <- (no internal imports — command runner for cycle command steps)
flow           <- models, store, step_runner, policy, gates, providers/pool
queue          <- models, store, flow
api            <- models, store, queue, flow
server         <- models, api, flow (HTTP API)
cli            <- models, api, server, fmt
providers/*    <- models (LLMProvider protocol)
```

## Building blocks — use these, don't recreate

- `src/models.py` — enums, dataclasses, exceptions, LLMProvider protocol
- `tests/conftest.py` — shared fixtures: tmp_db, mock_pool, sample_task_config
- `tests/factories.py` — factory functions: create_task, create_step_result, create_manifest, etc.
- `.claude/rules/` — 5 rule files: subprocess, persistence, testing, models, CLI patterns

## Prohibitions

- No ORMs — use raw `sqlite3` with WAL mode
- No async/await — subprocess-based execution for escape hatch only
- No external task queues
- No Pydantic or attrs — use stdlib `dataclasses`
- No `dataclasses.asdict()` directly — use `to_dict()` with `_enum_dict_factory`
- No `shell=True` without documenting trust model and using `shlex.quote()`

## Quickstart — running a task

**1. Config** — `tao.toml` (in the repo root, or pass `--config /path/to/tao.toml`). Already has a `claude` provider (type `claude_cli`) with `opus`, `sonnet`, and `haiku` aliases (CLI shortnames that auto-resolve to the latest version of each tier).

**2. Define task** — create a JSON file (e.g. `task.json`).

The `cwd` field is the workspace directory where the LLM operates — **it must exist before running**. For code tasks, point to the project repo. For research/output tasks, create a dedicated directory first. The Claude provider auto-creates a minimal `CLAUDE.md` in the cwd if missing (so the CLI recognizes it as a workspace).

```json
{
    "title": "Add user authentication",
    "body": "Implement email/password login with session tokens and tests.",
    "cwd": "/path/to/project",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "plan", "type": "llm", "prompt": "Plan the implementation.", "model_spec": "opus@claude"},
        {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude", "next": "validate"},
        {"id": "fix", "type": "llm", "prompt": "Fix the errors.", "model_spec": "sonnet@claude", "next": "validate"},
        {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "python -m pytest tests/"], "on_fail": "fix"}
    ],
    "max_retries": 3,
    "policies": {
        "batch_size": 5,
        "max_iterations": 10
    }
}
```

For long prompts, use `body_file` instead of `body` — path relative to the JSON file:

```json
{
    "title": "Research: SEO analysis",
    "body_file": "prompt.md",
    "cwd": "/path/to/workspace",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "gather", "type": "llm", "prompt": "Research this topic.", "model_spec": "opus@claude"},
        {"id": "write", "type": "llm", "prompt": "Write the findings.", "model_spec": "sonnet@claude"}
    ]
}
```

**3. Run**:

```bash
tao run task.json                        # single task
tao run task1.json task2.json task3.json  # multiple tasks in parallel
tao --config /path/to/tao.toml run task.json  # custom config path
```

Submits all tasks and starts the queue in a single process. Exits automatically when all tasks finish. Ctrl+C to stop early. Add `-v` before the subcommand for verbose logging: `tao -v run task.json`.

**4. Monitor** — `tao run` writes to `.tao/engine.db`. Query from another terminal:

```bash
tao status              # list all tasks
tao status <id>         # detail for one task
tao traces <id>         # per-step cost/time
tao summary <id>        # aggregated metrics
```

When using `tao serve --http`, a web monitor is available at `http://127.0.0.1:8321/` with real-time task status, subtask progress, and trace details.

### Modes

- **Scoped** (scope + cycle): scope decomposes into subtasks, the cycle runs for each. Re-scope after each batch — empty array = done.
- **One-shot** (cycle only): no decomposition, body becomes the single subtask. Use when the task is already atomic.

### Task JSON by task type

Code task — full dev cycle with validation loop:
```json
{
    "title": "...", "body": "...", "cwd": "...",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "plan", "type": "llm", "prompt": "Plan.", "model_spec": "sonnet@claude"},
        {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude", "next": "validate"},
        {"id": "fix", "type": "llm", "prompt": "Fix errors.", "model_spec": "sonnet@claude", "next": "validate"},
        {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "pytest tests/"], "on_fail": "fix"}
    ],
    "max_retries": 3,
    "policies": {"batch_size": 3}
}
```

Research — scoped, linear cycle:
```json
{
    "title": "...", "body_file": "prompt.md", "cwd": "...",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "gather", "type": "llm", "prompt": "Research.", "model_spec": "opus@claude"},
        {"id": "write", "type": "llm", "prompt": "Write document.", "model_spec": "sonnet@claude"}
    ],
    "policies": {"batch_size": 5, "max_iterations": 2}
}
```

Quick one-shot:
```json
{
    "title": "...", "body": "...", "cwd": "...",
    "cycle": [
        {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "sonnet@claude"}
    ]
}
```

### Cycle step types

| Type | Fields | Behavior |
|------|--------|----------|
| `llm` | `prompt`, `model_spec`, `failover`, `timeout` | Calls LLM with context injection |
| `command` | `commands`, `on_fail` | Runs shell commands, all must pass |

### Jump keywords

| Keyword | Allowed on | Behavior |
|---------|-----------|----------|
| `next` | any step | Jump to target step instead of advancing linearly |
| `on_fail` | command only | Jump to target step when any command fails |

Only backward jumps (target ≤ current position) count toward `max_retries`. Forward jumps are free.

### Context injection rules

| Position | Prompt content |
|----------|---------------|
| First LLM step | `[subtask description] --- [step prompt]` |
| Subsequent LLM steps | `[last LLM output] --- [step prompt]` |
| After on_fail jump | `[last LLM output] --- [step prompt] --- [validation errors]` |

### Policies reference

| Field | Default | Description |
|-------|---------|-------------|
| `batch_size` | 5 | Subtasks per batch |
| `max_iterations` | 10 | Batches before checkpoint/pause |
| `max_subtasks` | 20 | Safety cap on total subtasks |

`max_retries` (default 3, per task config) caps backward jumps per subtask. Default timeout per step: 1800s (30 min). Override per step via `"timeout"` in the step dict.

Full reference: `docs/getting-started.md` and `docs/config-reference.md`.

### Tools

By default, all tools are auto-approved for LLM-direct steps (via `--dangerously-skip-permissions`). To restrict to specific tools, set the `tools` field in the task JSON:

```json
{
    "tools": ["Read", "Write", "Edit"]
}
```

Omit the field (or don't set it) to allow all tools. Set it to a list to restrict to only those tools (via `--allowedTools`).

## What the LLM worker sees (prompt design)

Prompts are built in `flow.py`. Scope uses `_run_scope_llm_step`, cycle steps use `_run_cycle_llm_step`. The LLM has access to all tools by default (see Tools section). The provider ensures the LLM recognizes the workspace (see provider docstrings).

| Step | Prompt content | Notes |
|------|---------------|-------|
| **scope** | Task title + body + batch_size constraint. Must output JSON array of `{"title", "description"}`. On re-scope: completed summaries + original task. | Scope sees the full task body — this is the only step that does. |
| **cycle step (first)** | Subtask description + `---` + step prompt. | First LLM step in the cycle — uses subtask description as context. |
| **cycle step (2+)** | Last LLM output + `---` + step prompt. | Subsequent steps chain: each sees the previous LLM's output. |
| **cycle step (on_fail)** | Last LLM output + `---` + step prompt + `---` + validation errors. | Reached via on_fail — receives failed command output. |

**Key design rule**: cycle steps are intentionally isolated from the parent task. If a subtask description lacks context, the fix is in scope's output quality (or the task body that scope reads), not in leaking the parent body to cycle steps.

**Output instructions belong in the task body, not the engine.** TAO does not inject instructions about *what* to write or *where*. The task body must specify output expectations (e.g., "Write findings to a markdown file in the working directory"). Scope propagates these into subtask descriptions.

**Workspace and tool usage are provider-level, not engine-level.** Each provider handles two things via its own mechanism (e.g., `--append-system-prompt` for Claude):
1. **Workspace location** — where the cwd is, and that paths should be relative to it.
2. **Tool usage convention** — how to operate with files (e.g., "use the Write tool to save files to disk").

This is infrastructure, not prescription: it tells the LLM *how to use its tools*, not *what to produce*. The task body controls what to write and where; the provider ensures the LLM uses the right mechanism. Users and consumers (e.g., TOP) should not need to know provider-specific tool names.

## HTTP API (`tao serve --http`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/tasks` | Submit a new task |
| GET | `/tasks` | List all tasks (optional `?status=` filter) |
| GET | `/tasks/{id}` | Get task detail |
| DELETE | `/tasks/{id}` | Delete a terminal task |
| POST | `/tasks/{id}/stop` | Graceful stop |
| POST | `/tasks/{id}/cancel` | Cancel (terminal) |
| POST | `/tasks/{id}/unblock` | Resume blocked/stopped/failed task |
| POST | `/tasks/{id}/restart` | Restart from scratch (clears checkpoint + traces) |
| GET | `/tasks/{id}/traces` | Get execution traces |
| GET | `/tasks/{id}/summary` | Get aggregated metrics |
| GET | `/health` | Health check + queue status |
| GET | `/` or `/monitor` | Web monitor UI |

## Adding a new module

1. Create `src/<module>.py` with docstring describing its purpose
2. Create `tests/test_<module>.py`
3. Add factory functions to `tests/factories.py` if the module introduces new types
4. Update `src/__init__.py` if the module exports public API
5. Verify imports respect the dependency DAG above
6. Run `make lint && make test`

## Test conventions

- All fixtures use `tmp_path` for isolation — no shared state between tests
- Factory functions over mocks: never mock what you can create with a factory
- Test naming: `test_<module>_<scenario>` (e.g., `test_store_create_task`)
- Mark slow tests with `@pytest.mark.slow`

## Error handling

- Expected errors (`TaoError` subtypes) -> clean message to stderr
- Unexpected errors -> `logger.exception()` with full traceback
- JSON input: always catch `json.JSONDecodeError` with helpful message

## SQLite patterns

- WAL mode always enabled
- `threading.Lock` on all Store operations — single connection shared across HTTP server, queue, and task threads
- `_execute_with_retry` for lock contention
- JSON columns: try/except on parse, return safe default
- Schema version tracking
