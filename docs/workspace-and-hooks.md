# Workspace Commands & Hooks

## `cwd` vs Workspace

For most tasks, set `cwd` in the task config — it tells the LLM where to work:

```python
config = {
    "cwd": "/path/to/project",
    "cycle": [
        {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude"},
    ],
}
```

Use **workspace commands** when you need lifecycle management — creating temporary environments, checkpointing, delivering results, and cleaning up:

```python
config = {
    "cycle": [
        {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude"},
    ],
    "workspace": {
        "create": "git worktree add /tmp/ws-{task_id} main && echo /tmp/ws-{task_id}",
        "persist": "git -C {workspace} add -A && git -C {workspace} commit -m 'checkpoint'",
        "deliver": "git -C {workspace} push origin HEAD",
        "cleanup": "git worktree remove {workspace}",
    },
}
```

When workspace commands are configured, the `create` command's stdout becomes the working directory. When only `cwd` is set, it's used directly.

## Workspace Lifecycle

```
create ─── scope ─── [subtask: cycle steps (e.g. plan→implement→validate→fix) → persist] ─── deliver ─── cleanup
  │                        │ (per subtask)                                           │          │
  │                        └── persist runs after each subtask succeeds              │          │
  │                                                                                 │          │
  └── stdout becomes workspace_path                                       fatal on failure    best-effort
```

### Commands

| Command | When | Fatal? | Placeholders |
|---------|------|--------|-------------|
| `create` | Before scope, once per task | Yes | `{task_id}` |
| `persist` | After each subtask succeeds | No (warning only) | `{workspace}`, `{task_id}` |
| `deliver` | After all subtasks complete | Yes | `{workspace}`, `{task_id}` |
| `cleanup` | After deliver (or on failure) | No (best-effort) | `{workspace}`, `{task_id}` |

### `create`

Runs before any step executes. Its **stdout** (trimmed) becomes the `workspace_path` available to all steps via `ctx.workspace_path`.

```python
"create": "git worktree add /tmp/ws-{task_id} main && echo /tmp/ws-{task_id}"
# stdout: "/tmp/ws-42" → becomes workspace_path
```

If `create` fails (non-zero exit), the task fails immediately.

If `create` is empty/unset, `workspace_path` is an empty string (and `cwd` is used as the working directory instead).

### `persist`

Runs after each subtask succeeds. Non-fatal — failure logs a warning but doesn't stop the flow.

```python
"persist": "git -C {workspace} add -A && git -C {workspace} commit -m 'checkpoint'"
```

### `deliver`

Runs after all subtasks complete. Fatal — failure fails the task.

```python
"deliver": "git -C {workspace} push origin HEAD"
```

### `cleanup`

Runs after deliver. Best-effort — failures are logged but ignored.

```python
"cleanup": "git worktree remove {workspace}"
```

## Template Placeholders

All workspace commands and hooks support template placeholders using `{key}` syntax.

**All values are automatically escaped with `shlex.quote()`** to prevent shell injection. You don't need to quote them manually.

Available placeholders vary by command — see the tables above and below.

Unknown placeholders raise `ValueError` (fail-fast, not silent substitution).

## Hooks

Hooks are shell commands fired on lifecycle events. They are non-fatal — exceptions are caught, logged, and the flow continues.

### Hook configuration

```python
config = {
    "hooks": {
        "on_step_output": "curl -X POST https://example.com/webhook -d @{output_file}",
        "on_scope_complete": "echo 'Scope done for task {task_id}'",
        "on_blocked": "notify-send 'Task {task_id} blocked: {reason}'",
        "on_flow_complete": "python process_summary.py {summary_file}",
        "on_error": "echo 'Error in task {task_id}: {error}' >> errors.log",
    }
}
```

### Hook reference

#### `on_step_output`

Fired after each cycle step completes.

| Placeholder | Description |
|-------------|-------------|
| `{task_id}` | Task ID |
| `{step_name}` | Step `id` from the cycle config (e.g. `"plan"`, `"implement"`, `"validate"`, `"fix"`) |
| `{output_file}` | Path to temp file containing the step's output text |

The temp file is automatically cleaned up after the hook runs.

#### `on_scope_complete`

Fired after the scope step completes (not `on_step_output` — scope is special).

| Placeholder | Description |
|-------------|-------------|
| `{task_id}` | Task ID |
| `{output_file}` | Path to temp file containing scope output |

#### `on_blocked`

Fired when a task enters `blocked` state.

| Placeholder | Description |
|-------------|-------------|
| `{task_id}` | Task ID |
| `{reason}` | The blocked reason string |

#### `on_flow_complete`

Fired when the flow finishes, regardless of final status (completed, failed, blocked, stopped). Fires exactly once per flow run.

| Placeholder | Description |
|-------------|-------------|
| `{task_id}` | Task ID |
| `{summary_file}` | Path to temp file containing JSON summary (same as `engine.summary()`) |

#### `on_error`

Fired when a step fails (non-blocked failure).

| Placeholder | Description |
|-------------|-------------|
| `{task_id}` | Task ID |
| `{error}` | Error message |

### Hook execution details

- Timeout: 30 seconds per hook.
- All exceptions are caught and logged (hooks never crash the flow).
- Data is passed via temp files (auto-cleaned after the hook runs).
- Hooks run synchronously in the flow thread.

## Example: Git Worktree Workflow

```python
engine.submit(
    title="Build feature X",
    config={
        "scope": {"model_spec": "sonnet@claude"},
        "cycle": [
            {"id": "plan", "type": "llm", "prompt": "Plan the implementation.", "model_spec": "sonnet@claude"},
            {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude", "next": "validate"},
            {"id": "fix", "type": "llm", "prompt": "Fix the errors.", "model_spec": "sonnet@claude", "next": "validate"},
            {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "pytest tests/"], "on_fail": "fix"},
        ],
        "max_retries": 3,
        "workspace": {
            "create": "git worktree add /tmp/tao-{task_id} main && echo /tmp/tao-{task_id}",
            "persist": "git -C {workspace} add -A && git -C {workspace} commit -m 'tao checkpoint' --allow-empty",
            "deliver": "git -C {workspace} push origin HEAD:refs/heads/tao-{task_id}",
            "cleanup": "git worktree remove {workspace} --force",
        },
        "hooks": {
            "on_blocked": "curl -X POST $SLACK_WEBHOOK -d '{\"text\": \"Task {task_id} needs input: {reason}\"}'",
            "on_flow_complete": "python notify.py {summary_file}",
        },
    },
)
```
