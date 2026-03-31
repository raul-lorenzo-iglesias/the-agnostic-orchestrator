# TAO — The Agnostic Orchestrator

A generic task orchestrator that drives LLM work through configurable cycles.

## What TAO Is

TAO breaks tasks into subtasks and executes each through a user-defined cycle of LLM and command steps. It manages concurrency, persists state across interruptions, and supports checkpoint/resume. By default, TAO calls the LLM provider directly — no subprocess scripts needed. Subprocess steps are available as an escape hatch for custom logic.

- **Python >= 3.11**, stdlib-only core (no Django, SQLAlchemy, Pydantic)
- **LLM-direct**: calls the provider CLI directly for each step
- **SQLite + WAL**: persistent state, concurrent-safe, checkpoint/resume
- **Provider pool**: Claude CLI, Copilot CLI, extensible

## The One Flow

```
scope(task) → subtasks[]
for each subtask:
    cycle: step₁ → step₂ → ... → stepₙ  (with jumps on success/failure)
re-scope → more subtasks or done
```

## Install

```bash
pip install -e ".[dev]"
```

## Quick Start

> Full walkthrough: **[docs/getting-started.md](docs/getting-started.md)**

### 1. Configure

Create a `tao.toml`:

```toml
[engine]
db_path = ".tao/engine.db"
max_concurrent = 2

[providers.claude]
type = "claude_cli"
models = { opus = "opus", sonnet = "sonnet", haiku = "haiku" }
# The CLI accepts short aliases ("opus", "sonnet", "haiku") and full model IDs
# ("claude-opus-4-6"). Aliases auto-resolve to the latest version of each tier.
```

### 2. Define a task

Create a `task.json`. The `cwd` field is the working directory where the LLM operates — **it must exist before running the task**. For code tasks, point to your project repo. For research/output tasks, create a dedicated directory first (`mkdir -p /tmp/my-research`).

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
    "policies": {"batch_size": 5, "max_iterations": 10}
}
```

For long prompts, use `body_file` instead of `body` (path relative to the JSON file, plain text or markdown, injected as-is into the task body):

```json
{
    "title": "Research: SEO analysis",
    "body_file": "prompt.md",
    "cwd": "/path/to/workspace",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "gather", "type": "llm", "prompt": "Research this topic.", "model_spec": "sonnet@claude"},
        {"id": "write", "type": "llm", "prompt": "Write the findings.", "model_spec": "sonnet@claude"}
    ]
}
```

### 3. Run

```bash
tao run task.json                        # single task
tao run task1.json task2.json task3.json  # multiple tasks in parallel
tao --config /path/to/tao.toml run task.json  # custom config path
```

Submits all tasks and starts the queue in a single process. The process exits automatically when all tasks finish. Ctrl+C to stop early. Add `-v` for verbose logging.

### 4. Monitor

`tao run` writes state to a SQLite database (`.tao/engine.db` by default). You can query it from another terminal while `tao run` is active:

```bash
tao status              # list all tasks
tao status 42           # detail for one task
tao traces 42           # per-step cost/time
tao summary 42          # aggregated metrics
```

With HTTP mode (`tao serve --http`), a web monitor is available at `http://127.0.0.1:8321/` with real-time task status, subtask progress, cost tracking, and trace details.

## Task JSON Reference

### Model spec format

Models use the `alias@provider` format, where `alias` matches a key in `tao.toml`'s `models` map and `provider` matches the provider name:

```
"model_spec": "opus@claude"     # use opus alias from claude provider
"model_spec": "sonnet@claude"   # use sonnet alias from claude provider
"model_spec": "haiku@claude"    # use haiku alias from claude provider
```

### Modes

- **Scoped** (scope + cycle): scope decomposes into subtasks, the cycle runs for each. Re-scope after each batch — empty array = done.
- **One-shot** (cycle only): no decomposition, body becomes the single subtask. Use when the task is already atomic.

### By task type

**Code task** — full dev cycle with validation loop:
```json
{
    "title": "...", "body": "...", "cwd": "/path/to/repo",
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

**Research** — scoped, linear cycle:
```json
{
    "title": "...", "body_file": "prompt.md", "cwd": "...",
    "scope": {"model_spec": "sonnet@claude"},
    "cycle": [
        {"id": "gather", "type": "llm", "prompt": "Research.", "model_spec": "sonnet@claude"},
        {"id": "write", "type": "llm", "prompt": "Write document.", "model_spec": "sonnet@claude"}
    ],
    "policies": {"batch_size": 5, "max_iterations": 2}
}
```

**Quick one-shot**:
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

### Context injection

| Position | Prompt content |
|----------|---------------|
| First LLM step | `[subtask description] --- [step prompt]` |
| Subsequent LLM steps | `[last LLM output] --- [step prompt]` |
| After on_fail jump | `[last LLM output] --- [step prompt] --- [validation errors]` |

### Policies

| Field | Default | Description |
|-------|---------|-------------|
| `batch_size` | 5 | Subtasks per batch |
| `max_iterations` | 10 | Batches before checkpoint/pause |
| `max_subtasks` | 20 | Safety cap on total subtasks |

`max_retries` (default 3, per task config) caps backward jumps per subtask. Default timeout per step: 1800s (30 min). Override per step via `"timeout"` in the step dict.

### Tools

By default, all tools are auto-approved for LLM-direct steps (via `--dangerously-skip-permissions`). To restrict to specific tools, pass a list of provider-specific tool names (e.g. `Read`, `Write`, `Edit` for Claude CLI):

```json
{
    "tools": ["Read", "Write", "Edit"]
}
```

### Timeout

Default timeout per step: 1800s (30 min). Override per step:

```json
{"id": "implement", "type": "llm", "prompt": "...", "model_spec": "opus@claude", "timeout": 1200}
```

## Blocked Tasks

When a step determines human input is needed, it signals a block:

```bash
tao unblock 42 --context '{"answer": "Use Python 3.11+ only"}'
```

## Subprocess Steps (Escape Hatch)

For custom logic that can't be expressed as an LLM prompt, you can use subprocess steps. Create a manifest with a `command` field — see [docs/step-protocol.md](docs/step-protocol.md) for the full spec.

## CLI Reference

```bash
tao run F [F...]               # submit task(s) from JSON and serve
tao serve [--http]             # start queue loop (--http for REST API + web monitor)
tao submit --title "..." [--body "..."] [--task-config '{}']  # add task to queue
tao status [N]                 # list all tasks or detail for one
tao traces N                   # execution traces
tao summary N                  # aggregated metrics
tao unblock N [--context '{}'] # resume blocked/stopped/failed task
tao stop N                     # graceful stop
tao cancel N                   # cancel (terminal, cannot be resumed)
tao restart N                  # restart from scratch (clears checkpoint + traces)
tao llm                        # LLM bridge (stdin/stdout JSON)
```

Use `tao --json <command>` for machine-readable JSON output (e.g. `tao --json status 1`).

## HTTP API

Start with `tao serve --http` (default port 8321).

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/tasks` | Submit a new task |
| GET | `/tasks` | List all tasks (`?status=` filter) |
| GET | `/tasks/{id}` | Get task detail |
| DELETE | `/tasks/{id}` | Delete a terminal task |
| POST | `/tasks/{id}/stop` | Graceful stop |
| POST | `/tasks/{id}/cancel` | Cancel (terminal) |
| POST | `/tasks/{id}/unblock` | Resume blocked/stopped/failed task |
| POST | `/tasks/{id}/restart` | Restart from scratch |
| GET | `/tasks/{id}/traces` | Execution traces |
| GET | `/tasks/{id}/summary` | Aggregated metrics |
| GET | `/health` | Health check + queue status |
| GET | `/` or `/monitor` | Web monitor UI |

## Python API

```python
from src.api import Engine

with Engine(config_path="tao.toml") as engine:
    task_id = engine.submit(
        title="Build feature X",
        body="Implement the widget system",
        config={
            "cwd": "/path/to/project",
            "scope": {"model_spec": "sonnet@claude"},
            "cycle": [
                {"id": "plan", "type": "llm", "prompt": "Plan.", "model_spec": "sonnet@claude"},
                {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix.", "model_spec": "sonnet@claude", "next": "validate"},
                {"id": "validate", "type": "command", "commands": ["pytest tests/"], "on_fail": "fix"},
            ],
            "max_retries": 3,
        },
    )

    result = engine.run_flow(task_id)
    print(f"Task finished: {result}")

    for t in engine.get_traces(task_id):
        print(f"  {t['role']} → {t['model']} ({t['elapsed_s']:.1f}s, ${t['cost_usd']:.4f})")
```

## Documentation

| Doc | What it covers |
|-----|---------------|
| **[Getting Started](docs/getting-started.md)** | First task from zero to execution |
| **[Config Reference](docs/config-reference.md)** | Full TOML schema, task config, policies, defaults |
| **[Step Protocol](docs/step-protocol.md)** | Subprocess escape hatch: stdin/stdout spec |
| **[API Reference](docs/api-reference.md)** | All Engine methods with signatures and errors |
| **[Task Lifecycle](docs/task-lifecycle.md)** | State machine, transitions, checkpoint/resume |
| **[Workspace & Hooks](docs/workspace-and-hooks.md)** | Workspace commands, hook events, cwd vs workspace |
| **[Errors](docs/errors.md)** | Error hierarchy, when each is raised |
| **[HTTP API](docs/http-api.md)** | REST API for local service mode |

## Architecture

```
src/
├── models.py         # enums, dataclasses, exceptions, LLMProvider protocol
├── store.py          # SQLite + WAL persistence
├── flow.py           # orchestration loop (scope → cycle interpreter → re-scope)
├── queue.py          # task state machine + concurrency control
├── api.py            # Engine — public Python API
├── server.py         # HTTP API (ThreadingHTTPServer)
├── cli.py            # CLI entry point
├── policy.py         # batch/iteration/retry policies
├── gates.py          # command runner for cycle command steps
├── step_runner.py    # subprocess escape hatch
├── fmt.py            # CLI output formatting
├── providers/
│   ├── pool.py       # provider pool with model routing
│   ├── claude.py     # Claude CLI provider
│   ├── copilot.py    # Copilot CLI provider
│   └── llm_service.py # LLM bridge for subprocess steps
└── static/
    └── monitor.html  # web monitor UI
```

## License

AGPL-3.0 — See [LICENSE](LICENSE) for details.
