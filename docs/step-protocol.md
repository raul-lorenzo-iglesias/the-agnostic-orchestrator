# Step Protocol (Subprocess Escape Hatch)

Subprocess steps are the escape hatch for custom logic that can't be expressed as an LLM prompt. In the default LLM-direct mode, TAO calls the LLM provider directly — you don't need any of this. Use subprocess steps only when you need deterministic checks, external API calls, or custom data transformations.

Steps are executables (any language) that communicate with TAO via JSON on stdin/stdout.

## When to use subprocess steps

- Deterministic validation (file checks, schema validation)
- External API calls (webhooks, database queries)
- Data transformations (parsing, formatting)
- Custom orchestration logic

For LLM-powered work, the default LLM-direct mode is simpler — just define a `cycle` array with `llm` steps.

## Stdin (engine → step)

TAO pipes a single JSON object to the step's stdin:

```json
{
  "ctx": {
    "task_title": "Build feature X",
    "task_body": "Implement the widget system",
    "workspace_path": "/tmp/ws-42",
    "batch_size": 5,
    "subtask": {"title": "Add tests", "description": "..."},
    "plan_output": "...",
    "...": "any key provided by previous steps"
  },
  "config": {
    "model_spec": "opus@claude",
    "...": "step-specific config from the cycle step definition"
  }
}
```

### Context keys guaranteed by the engine

| Key | Type | Present in | Description |
|-----|------|-----------|-------------|
| `task_title` | string | all steps | Task title from `submit()`. |
| `task_body` | string | all steps | Task body from `submit()`. |
| `workspace_path` | string | all steps | Output of workspace `create` command (empty string if no workspace). |
| `batch_size` | int | all steps | From policies config. |
| `batch_context` | string | after scope | Context from scope step's `data.batch_context`. |
| `subtask` | any | cycle steps | Current subtask object from scope's `data.subtasks[]`. |

**Re-scope only** (present when the engine re-scopes after a batch):

| Key | Type | Description |
|-----|------|-------------|
| `completed_titles` | list[str] | Titles of subtasks completed in previous batches. |
| `completed_summaries` | string | Formatted summary of completed subtasks. |
| `iteration` | int | Current re-scope iteration number (2+). |

Steps can also receive keys added by previous steps via `provides` (see Manifest below).

### Config object

The `config` object contains whatever was configured in the cycle step definition for this step. TAO does not interpret it — it's pass-through for the step to use.

## Calling the LLM service from a subprocess step

If your subprocess step needs an LLM, use `tao llm` — a JSON stdin/stdout bridge to the configured provider pool.

### Request format (step → `tao llm`)

```json
{
  "prompt": "Analyze this code...",
  "model": "opus",
  "tools": ["Read", "Write"],
  "timeout": 300,
  "cwd": "/path/to/workspace",
  "resume_session_id": ""
}
```

### Response format (`tao llm` → step)

```json
{
  "success": true,
  "output": "Here are my findings...",
  "cost_usd": 0.05,
  "tokens_in": 1500,
  "tokens_out": 800,
  "elapsed_s": 12.3,
  "session_id": "sess-abc-123"
}
```

On error: `{"success": false, "error": "all providers failed for model 'opus': ...", "output": ""}`.

## Stdout (step → engine)

The step must write a single JSON object to stdout:

```json
{
  "status": "succeeded",
  "output": "Human-readable summary of what happened",
  "data": {
    "result": "value passed to next steps via context"
  },
  "blocked_reason": "",
  "cost_usd": 0.05,
  "tokens_in": 1500,
  "tokens_out": 800,
  "elapsed_s": 12.3,
  "session_id": ""
}
```

### Required fields

| Field | Type | Values | Description |
|-------|------|--------|-------------|
| `status` | string | `"succeeded"`, `"failed"`, `"skipped"` | Step outcome. |
| `output` | string | — | Human-readable summary. Passed to `on_step_output` hook. |
| `data` | object | — | Key-value pairs injected into context for subsequent steps. Must include all keys declared in manifest `provides`. |

### Optional fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `blocked_reason` | string | `""` | If non-empty **and status is `failed`**, task enters `blocked` state instead of failing. |
| `cost_usd` | float | `0.0` | LLM cost for trace recording. |
| `tokens_in` | int | `0` | Input tokens for trace recording. |
| `tokens_out` | int | `0` | Output tokens for trace recording. |
| `elapsed_s` | float | `0.0` | Execution time for trace recording. |
| `session_id` | string | `""` | LLM session ID returned by the provider (for trace recording). |

### Status behavior

| Status | Flow continues? | Task status |
|--------|----------------|-------------|
| `succeeded` | Yes | Running |
| `failed` | No | Failed |
| `failed` + `blocked_reason` | No | Blocked (task can be resumed via `unblock`) |
| `skipped` | Yes (step treated as no-op) | Running |

### Invalid output handling

If the step's stdout is not valid JSON or doesn't match the expected structure, TAO synthesizes a `failed` result:

- Non-zero exit code + no valid JSON → `"step exited with code N: <stderr>"`
- Zero exit code + invalid JSON → `"invalid JSON output: <stdout[:200]>"`

## Stderr

Stderr is captured and logged at DEBUG level. It does not affect the step result. Use stderr for diagnostic output.

## Environment variables

TAO sets these environment variables for every step subprocess:

| Variable | Type | Description |
|----------|------|-------------|
| `TAO_TASK_ID` | string | Task ID (integer as string). |
| `TAO_SUBTASK_INDEX` | string | Current subtask index (0-based, integer as string). |
| `TAO_ROLE` | string | Step `id` from the cycle config (e.g. `"plan"`, `"implement"`, `"validate"`, `"fix"`, `"gather"`, `"write"`). |
| `TAO_CONFIG` | string | Absolute path to the engine's TOML config file. Used by `tao llm` to find provider configuration. |

## Step Manifest

A subprocess step is declared by a JSON manifest. In the manifest, the `command` field is what makes it a subprocess step (as opposed to an LLM-direct step).

### Manifest schema

```json
{
  "name": "validate",
  "command": "python steps/validate.py",
  "needs": ["subtask", "workspace_path", "implement_output"],
  "provides": ["validation_report"],
  "timeout": 600
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | yes | — | Step name (must be non-empty). |
| `command` | string | yes | — | Shell command to execute. This is what makes it a subprocess step. |
| `needs` | string[] | no | `[]` | Context keys required before execution. Missing keys → `TaoError`. |
| `provides` | string[] | no | `[]` | Context keys this step adds. Values read from `data` in the step's output. |
| `timeout` | int | no | `300` | Max seconds before the step is killed. Must be positive. |

### Manifest validation

TAO validates subprocess step manifests:
- `name` is a non-empty string
- `command` is a non-empty string
- `timeout` is positive

Invalid manifests raise `ManifestValidationError`.

## Scope step output keys

The scope step's `data` dict has special keys consumed by the engine:

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `subtasks` | list | expected | List of subtask objects. Each becomes `ctx.subtask` for cycle steps. Empty list = task is complete. |
| `batch_context` | string | no | Injected into `ctx.batch_context` for all subsequent steps. |

Re-scope always happens after each batch. The scope step signals completion by returning an empty `subtasks` array — there is no `remaining_estimate` flag. The engine always re-scopes; zero subtasks = done.

## Examples

### Deterministic check (Python) — no LLM

```python
#!/usr/bin/env python3
"""Review step that validates output without calling an LLM."""
import json, os, sys

data = json.load(sys.stdin)
ctx = data["ctx"]
workspace = ctx.get("workspace_path", "")

issues = []
if not os.path.isfile(os.path.join(workspace, "output.md")):
    issues.append("output.md not found")

if issues:
    json.dump({"status": "failed", "output": "; ".join(issues), "data": {}}, sys.stdout)
else:
    json.dump({"status": "succeeded", "output": "Review passed", "data": {}}, sys.stdout)
```

### Bash

```bash
#!/bin/bash
INPUT=$(cat)
TITLE=$(echo "$INPUT" | python3 -c "import json,sys; print(json.load(sys.stdin)['ctx']['task_title'])")
echo "{\"status\": \"succeeded\", \"output\": \"Processed: $TITLE\", \"data\": {}}"
```

### Node.js

```javascript
const data = JSON.parse(require('fs').readFileSync('/dev/stdin', 'utf8'));
const result = { status: "succeeded", output: "analyzed", data: {} };
process.stdout.write(JSON.stringify(result));
```

**Go, Rust, Ruby...** — anything that reads stdin and writes stdout works.
