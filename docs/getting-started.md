# Getting Started with TAO

This guide walks you through running your first task with TAO, from zero to execution.

## Prerequisites

- Python >= 3.11
- `pip install -e ".[dev]"`
- An LLM CLI installed (e.g. `claude` or `copilot`)
  - For Claude: install the [Claude CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` command must be available in PATH)
  - For Copilot: install the [GitHub Copilot CLI](https://docs.github.com/en/copilot) (`copilot` command)

## Concepts

- **Task**: a unit of work (title + body + config)
- **Two modes**: scoped (decompose → batch → re-scope loop) and one-shot (single subtask, execute directly)
- **Cycle**: configurable sequence of steps (LLM + command) that runs for each subtask
- **LLM-direct mode** (default): TAO calls the LLM provider directly. No scripts needed.
- **model@provider**: explicit format for model selection (e.g. `opus@claude`)

## Step 1: Create config

**tao.toml:**
```toml
[engine]
db_path = ".tao/engine.db"    # SQLite state, auto-created
max_concurrent = 3              # max parallel tasks

[providers.claude]
type = "claude_cli"
models = { opus = "opus", sonnet = "sonnet", haiku = "haiku" }
# Aliases auto-resolve to the latest version of each tier.
# Full model IDs also work: opus = "claude-opus-4-6"

# [providers.copilot]
# type = "copilot_cli"
# models = { default = "gpt-4" }
```

## Step 2: Run a task

> **Important**: The `cwd` directory must exist before running a task — TAO does not create it. For research/output tasks, create a dedicated directory first: `mkdir -p /tmp/my-research`

### Example: Code task (scoped, with validation loop)

```python
from src.api import Engine

with Engine(config_path="tao.toml") as engine:
    task_id = engine.submit(
        title="Add user authentication",
        body="Implement email/password login with session tokens. "
             "Add tests for login, logout, and invalid credentials.",
        config={
            "cwd": "/path/to/project",
            "scope": {"model_spec": "sonnet@claude"},
            "cycle": [
                {"id": "plan", "type": "llm", "prompt": "Plan the implementation. List files, functions, and test cases.", "model_spec": "sonnet@claude"},
                {"id": "implement", "type": "llm", "prompt": "Implement the plan. Write all files to disk.", "model_spec": "opus@claude", "next": "validate"},
                {"id": "fix", "type": "llm", "prompt": "Fix the validation errors. Minimum change needed.", "model_spec": "sonnet@claude", "next": "validate"},
                {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "python -m pytest tests/"], "on_fail": "fix"},
            ],
            "max_retries": 3,
            "policies": {"batch_size": 3, "max_iterations": 5},
        },
    )

    result = engine.run_flow(task_id)
    print(f"Result: {result}")  # completed, failed, blocked, stopped, cancelled

    for trace in engine.get_traces(task_id):
        print(f"  [{trace['role']}] {trace['model']} {trace['elapsed_s']:.1f}s ${trace['cost_usd']:.4f}")
```

What happens:
1. **Scope** decomposes the task into up to 3 subtasks (batch_size)
2. For each subtask: **plan** → **implement** → **validate** (→ **fix** → **validate** if errors)
3. `max_retries: 3` caps backward jumps (fix→validate loop) per subtask
4. After the batch: **re-scope** checks what remains. Zero subtasks = done.
5. After 5 batches (max_iterations): checkpoint, waits for human approval

### How scope works

When a task has `scope` configured, TAO sends the task title + body to the scope LLM and expects a JSON array of subtasks back:

```json
[
    {"title": "Subtask 1", "description": "Detailed description of what to do"},
    {"title": "Subtask 2", "description": "..."}
]
```

The scope prompt includes the `batch_size` constraint. After each batch, TAO re-scopes — sending the original task + summaries of completed subtasks. If re-scope returns an empty array, the task is done.

**Important**: Cycle steps only see the subtask description, not the original task body. If your cycle steps need specific context (output filenames, constraints, etc.), put it in the task body so scope can propagate it into subtask descriptions.

### Example: Research task (one-shot)

A simple task with no decomposition — just execute:

```python
with Engine(config_path="tao.toml") as engine:
    task_id = engine.submit(
        title="Research: State of WebAssembly in 2026",
        body="Write a comprehensive analysis of WebAssembly adoption. "
             "Save the report to research/wasm-2026.md.",
        config={
            "cwd": "/path/to/workspace",
            "cycle": [
                {"id": "implement", "type": "llm", "prompt": "Research and write the report.", "model_spec": "sonnet@claude"},
            ],
        },
    )

    result = engine.run_flow(task_id)
```

No scope → single subtask from title/body → cycle runs once → done. No re-scope, no batches.

## CLI usage

There are two ways to run tasks:

- **`tao run task.json`** — submits task(s) and starts the queue in one process. Simplest for quick use.
- **`tao serve --http`** — starts the queue + HTTP API as a long-running service. Submit tasks via `curl` or the Python API.

Both write to the same SQLite database (`.tao/engine.db`). You can use `tao status`, `tao traces`, etc. from another terminal while either is running.

```bash
# Option A: one-shot run
tao run task.json

# Option B: long-running service
tao serve --http

# Submit a code task via HTTP
curl -X POST http://localhost:8321/tasks -H 'Content-Type: application/json' -d '{
  "title": "Add user authentication",
  "body": "Implement email/password login with tests",
  "cwd": "/path/to/project",
  "scope": {"model_spec": "sonnet@claude"},
  "cycle": [
    {"id": "plan", "type": "llm", "prompt": "Plan.", "model_spec": "sonnet@claude"},
    {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude", "next": "validate"},
    {"id": "fix", "type": "llm", "prompt": "Fix errors.", "model_spec": "sonnet@claude", "next": "validate"},
    {"id": "validate", "type": "command", "commands": ["pytest tests/"], "on_fail": "fix"}
  ],
  "max_retries": 3
}'

# Submit a research task (one-shot, no scope)
curl -X POST http://localhost:8321/tasks -H 'Content-Type: application/json' -d '{
  "title": "Research: WebAssembly in 2026",
  "body": "Write analysis of WASM adoption. Save to research/wasm-2026.md.",
  "cwd": "/path/to/workspace",
  "cycle": [
    {"id": "implement", "type": "llm", "prompt": "Research and write.", "model_spec": "sonnet@claude"}
  ]
}'

# Or submit from a JSON file
tao run task.json

# Check progress
curl http://localhost:8321/tasks/1

# Monitor in browser
open http://localhost:8321/
```

### CLI direct commands

```bash
# Check status
tao status          # list all tasks
tao status 1        # detail for task 1

# Execution traces
tao traces 1

# Aggregated metrics
tao summary 1

# Control
tao stop 1          # pause (resumable)
tao cancel 1        # terminate (permanent)
tao unblock 1       # resume a blocked/stopped/failed task
tao unblock 1 --context '{"human_message": "use pandas instead"}'
```

### Example output

After running `tao run task.json`, you should see on stdout:
```
submitted task 1
```
(Logging output appears on stderr — the above is the only stdout line.)

In another terminal:
```
$ tao status
  ID  Title                          Status     Current Step
   1  Add user authentication        running    implement:1 — Add login endpoint

$ tao traces 1
  #  Subtask  Step        Model           Tokens     Cost     Time  OK
  1  1        scope       sonnet@claude   16k/300    $0.035   6.8s  ✓
  2  1        plan        sonnet@claude   32k/400    $0.027  12.4s  ✓
  3  1        implement   opus@claude    105k/1.4k   $0.067  30.3s  ✓
  4  1        validate    —               —          —        0.3s  ✓

$ tao summary 1
  Total cost:    $0.129
  Total time:    49.8s
  Steps:         4 succeeded, 0 failed
  Tokens:        153k in, 2.1k out
```

## Queue multiple tasks

```python
with Engine(config_path="tao.toml") as engine:
    for task in tasks:
        engine.submit(title=task["title"], body=task["body"], config=task["config"])

    engine.serve()  # dispatches tasks respecting max_concurrent, blocks until Ctrl+C
```

## Communication with TAO

### Checkpoint (automatic pause)

After `max_iterations` batches, TAO pauses and waits:

```bash
tao unblock 1                                    # just continue
tao unblock 1 --context '{"human_message": "focus on the API tests"}'  # continue with guidance
```

### Failure retry

When a task fails, you can retry with guidance:

```bash
# Retry with a message
tao unblock 1 --context '{"human_message": "use sqlite instead of postgres"}'
```

### Stop and resume

```bash
tao stop 1      # pauses after current step finishes
tao unblock 1   # resumes from where it left off
```

### Cancel (permanent)

```bash
tao cancel 1    # terminates, cannot be resumed
```

## Further reading

- **[Config reference](config-reference.md)** — full TOML schema, task config, policies
- **[Step protocol](step-protocol.md)** — subprocess escape hatch: stdin/stdout spec, manifest schema
- **[API reference](api-reference.md)** — all Engine methods with signatures and examples
- **[Task lifecycle](task-lifecycle.md)** — state machine, policies, checkpoint/resume
- **[Workspace & hooks](workspace-and-hooks.md)** — workspace commands, hook events
- **[Errors](errors.md)** — error hierarchy, when each is raised, how to handle
- **[HTTP API](http-api.md)** — REST endpoints for `tao serve --http`

## Troubleshooting

### Task stuck in "running"
```bash
tao stop 1
```

### See detailed execution log
```bash
tao traces 1
```

### Task completes instantly with no output (most common issue)
The database has stale checkpoints from a previous run. Tasks with reused IDs inherit the old checkpoint and skip execution — they show `completed` with 0 traces and produce no files. This also happens when switching between `tao run` and `tao serve --http` against the same DB.

**Fix:** delete the database and restart:
```bash
rm -rf .tao/    # deletes SQLite DB — all task history is lost
```

### `cwd does not exist or is not a directory`
The `cwd` path must exist before submitting the task. Create it first:
```bash
mkdir -p /path/to/workspace
```

### Reset all state
```bash
rm -rf .tao/    # deletes SQLite DB
```

### Platform notes
- **Windows (Git Bash / MINGW64)**: Use Windows-style paths with forward slashes for `cwd` (e.g. `C:/Users/me/project`). MINGW64 paths like `/tmp/foo` are not resolved by Python. If `tao status` shows garbled characters, set `PYTHONIOENCODING=utf-8` in your shell or use `tao --json status` for ASCII-safe output.
- **Claude provider**: Auto-creates a `CLAUDE.md` in the `cwd` if missing, so the Claude CLI recognizes it as a workspace. This file is created automatically — you don't need to manage it.
