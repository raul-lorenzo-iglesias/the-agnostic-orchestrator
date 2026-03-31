# Configuration Reference

TAO is configured via a TOML file (default: `tao.toml`). All sections are optional except where noted.

## `[engine]`

Top-level engine settings.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `db_path` | string | `".tao/engine.db"` | Path to the SQLite database. **Resolved relative to the process cwd**, not the config file location. Parent directories are created automatically. |
| `max_concurrent` | int | `5` | Maximum tasks running in parallel. |

```toml
[engine]
db_path = ".tao/engine.db"
max_concurrent = 3
```

## `[providers.<name>]`

One section per LLM provider. The `<name>` is a user-chosen identifier (e.g., `claude`, `copilot`).

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `type` | string | yes | Provider implementation. One of: `"claude_cli"`, `"copilot_cli"`. |
| `models` | dict | yes | Map of alias → model identifier. The CLI accepts short aliases (`"opus"`, `"sonnet"`, `"haiku"`) and full model IDs (`"claude-opus-4-6"`). Aliases auto-resolve to the latest version. |

```toml
[providers.claude]
type = "claude_cli"
models = { opus = "opus", sonnet = "sonnet", haiku = "haiku" }

[providers.copilot]
type = "copilot_cli"
models = { default = "gpt-4" }
```

**Model routing**: When a step requests `"sonnet@claude"`, TAO routes to the `claude` provider and passes the `sonnet` alias. The provider resolves it to the actual model ID via its `models` map.

## Task config (passed via `engine.submit()`, JSON file, or HTTP API)

Task-level configuration is a JSON/dict passed per task. It controls the flow behavior for that specific task.

### `cwd`

Working directory for LLM calls. The LLM reads project files, runs commands, and makes changes in this directory. **Mandatory — the directory must exist before running the task.** TAO does not create it.

```json
{
    "cwd": "/path/to/project"
}
```

> **Note**: The Claude provider auto-creates a minimal `CLAUDE.md` in the cwd if one doesn't exist, so the CLI recognizes it as a workspace. This is transparent to the user.

### `cycle`

Array of step configs defining the execution sequence for each subtask. This is the core of TAO's configurable workflow.

Two step types:
- **`llm`**: sends a prompt to an LLM, receives output
- **`command`**: executes shell commands, captures results

Two flow-control keywords:
- **`next`**: on any step — jump to target step by id instead of advancing linearly
- **`on_fail`**: on command steps only — jump to target step if any command fails

```json
{
    "cycle": [
        {"id": "plan", "type": "llm", "prompt": "Plan the changes.", "model_spec": "sonnet@claude"},
        {"id": "implement", "type": "llm", "prompt": "Implement the plan.", "model_spec": "opus@claude", "next": "validate"},
        {"id": "fix", "type": "llm", "prompt": "Fix errors.", "model_spec": "sonnet@claude", "next": "validate"},
        {"id": "validate", "type": "command", "commands": ["pytest tests/", "ruff check src/"], "on_fail": "fix"}
    ]
}
```

#### LLM step fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier within the cycle |
| `type` | string | yes | Must be `"llm"` |
| `prompt` | string | yes | Instruction for the LLM |
| `model_spec` | string | yes | Model to use, format `alias@provider` |
| `next` | string | no | Jump to this step id after success |
| `timeout` | int | no | Max seconds (default 1800) |
| `failover` | list[string] | no | Fallback `model@provider` alternatives |

#### Command step fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | yes | Unique identifier within the cycle |
| `type` | string | yes | Must be `"command"` |
| `commands` | list[string] | yes | Shell commands to run sequentially. Stops on first failure — all outputs (including from failed command) are captured. |
| `on_fail` | string | no | Jump to this step id if any command fails |
| `next` | string | no | Jump to this step id after success |

#### Failover

When an LLM step fails (provider error, timeout, etc.), TAO tries the fallback models in order:

```json
{
    "id": "implement",
    "type": "llm",
    "prompt": "Implement the plan.",
    "model_spec": "opus@claude",
    "failover": ["sonnet@claude", "default@copilot"]
}
```

If `opus@claude` fails, TAO tries `sonnet@claude`, then `default@copilot`. If all fail, the step fails. Failover is per-step — each step can have its own fallback chain.

#### Context injection

TAO automatically injects context into LLM steps based on position:

| Position | Prompt content |
|----------|---------------|
| First LLM step | `[subtask description] --- [step prompt]` |
| Subsequent LLM steps | `[last LLM output] --- [step prompt]` |
| After on_fail jump | `[last LLM output] --- [step prompt] --- [validation errors]` |

> **Design note**: Cycle steps are intentionally isolated from the parent task. The first LLM step receives the *subtask description* (from scope), not the original task body. If your cycle steps need specific context (output filenames, constraints, etc.), include it in the task body so scope propagates it into subtask descriptions.

### `scope`

Optional. When present, TAO decomposes the task into subtasks before running the cycle. Omit for one-shot tasks.

```json
{
    "scope": {"model_spec": "sonnet@claude"}
}
```

### `max_retries`

Safety cap on backward jumps per subtask (default 3). Only backward jumps (target ≤ current step index) count. Exhausted → subtask fails → task fails.

**This is a top-level task field, not inside `policies`:**

```json
{
    "cwd": "...",
    "cycle": [...],
    "max_retries": 3,
    "policies": {"batch_size": 5}
}
```

### `tools`

List of tools to restrict LLM access. Omit for all tools (auto-approved via `--dangerously-skip-permissions`).

**Tool names are provider-specific.** For Claude CLI: `Read`, `Write`, `Edit`, `Glob`, `Grep`, `Bash`, `WebSearch`, `WebFetch`, etc. For other providers, consult the provider's documentation.

```json
{
    "tools": ["Read", "Write", "Edit"]
}
```

### `workspace`

Shell commands for workspace lifecycle. All commands support `{task_id}` and `{workspace}` placeholders (auto-escaped with `shlex.quote()`).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `create` | string | `""` | Run before the flow starts. Stdout becomes `workspace_path`. |
| `persist` | string | `""` | Run after each subtask succeeds. Non-fatal on failure. |
| `deliver` | string | `""` | Run after all subtasks complete. Fatal on failure. |
| `cleanup` | string | `""` | Run after deliver (or on failure). Best-effort. |

```json
{
    "workspace": {
        "create": "git worktree add /tmp/ws-{task_id} main && echo /tmp/ws-{task_id}",
        "persist": "git -C {workspace} add -A && git -C {workspace} commit -m 'checkpoint'",
        "deliver": "git -C {workspace} push origin HEAD",
        "cleanup": "git worktree remove {workspace}"
    }
}
```

For simple cases, `cwd` is sufficient. Workspace commands are for when you need create/persist/deliver/cleanup lifecycle management.

### `hooks`

Shell commands fired on lifecycle events. All support `{task_id}` placeholder. Data is passed via temp file whose path is injected as a placeholder.

| Key | Type | Default | Trigger | Data placeholder |
|-----|------|---------|---------|-----------------|
| `on_step_output` | string | `""` | After each step completes | `{step_name}`, `{output_file}` |
| `on_scope_complete` | string | `""` | After scope step | `{output_file}` |
| `on_blocked` | string | `""` | When task enters blocked state | `{reason}` |
| `on_flow_complete` | string | `""` | When flow finishes (any status) | `{summary_file}` |
| `on_error` | string | `""` | On step failure | `{error}` |

### `policies`

Flow behavior limits.

| Key | Type | Default | Range | Description |
|-----|------|---------|-------|-------------|
| `max_subtasks` | int | `20` | 1–100 | Max subtasks from scope. Exceeding raises `TaoError`. |
| `batch_size` | int | `5` | 1–50 | Subtasks per batch (passed to scope step in context). |
| `max_iterations` | int | `10` | 1–100 | Max batches before checkpoint (pause for human). |

### Unblock context

When unblocking a task (via `tao unblock --context` or HTTP API), the `context` dict supports one recognized key:

- `human_message` — a text message injected into the next scope or cycle step's prompt. The LLM sees it as additional guidance. It is consumed once and not carried forward.

```bash
tao unblock 1 --context '{"human_message": "use sqlite instead of postgres"}'
```

## Full example

```toml
[engine]
db_path = ".tao/engine.db"
max_concurrent = 5

[providers.claude]
type = "claude_cli"
models = { opus = "opus", sonnet = "sonnet", haiku = "haiku" }
```

```python
engine.submit(
    title="Build feature X",
    body="Implement the widget system with tests.",
    config={
        "cwd": "/path/to/project",
        "workspace": {
            "create": "git worktree add /tmp/ws-{task_id} main && echo /tmp/ws-{task_id}",
            "cleanup": "git worktree remove {workspace}",
        },
        "hooks": {
            "on_blocked": "notify-send 'TAO blocked: {reason}'",
        },
        "scope": {"model_spec": "sonnet@claude"},
        "cycle": [
            {"id": "plan", "type": "llm", "prompt": "Plan.", "model_spec": "sonnet@claude"},
            {"id": "implement", "type": "llm", "prompt": "Implement.", "model_spec": "opus@claude", "next": "validate"},
            {"id": "fix", "type": "llm", "prompt": "Fix.", "model_spec": "sonnet@claude", "next": "validate"},
            {"id": "validate", "type": "command", "commands": ["ruff check --fix src/", "pytest tests/ -x"], "on_fail": "fix"},
        ],
        "max_retries": 3,
        "policies": {"batch_size": 3},
    },
)
```
