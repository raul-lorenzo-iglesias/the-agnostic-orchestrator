# Error Reference

All expected errors inherit from `TaoError`. The CLI catches these for clean output (exit code 1). Unexpected errors get full tracebacks (exit code 2).

## Hierarchy

```
TaoError (base)
├── TaskNotFoundError
├── StepTimeoutError
├── ManifestValidationError
├── StoreError
└── ProviderError
```

## `TaoError`

Base class for all expected TAO errors.

```python
from src.models import TaoError
```

**When raised**: Various — acts as base class. Also raised directly for:
- Missing context keys: `"step 'execute' missing context keys: ['plan_output']"`
- Task not blocked: `"task 42 is not blocked (status: running)"`
- Task already terminal: `"task 42 is already terminal (status: completed)"`
- Iteration limit (checkpoint): `"iteration limit exceeded: 11 > 10"` — task enters `blocked` state, not `failed`
- Subtask limit: `"subtask limit exceeded: 25 > 20"`
- Policy validation: `"max_retries must be positive, got 0"`
- Config file: `"config file not found: missing.toml"`
- Invalid TOML: `"invalid TOML in config: ..."`
- Unknown provider: `"unknown provider type: openai_cli"`
- Required cwd missing: `"workspace path (cwd) is required in task config"`

## `TaskNotFoundError`

```python
from src.models import TaskNotFoundError
```

**When raised**: Any operation on a non-existent task ID.

**Typical message**: `"task 42 not found"`

**Raised by**: `Store.get_task()`, `Store.update_task_status()`, and any Engine method that accesses a task.

**How to handle**: Check task exists before operating, or catch and report to user.

## `StepTimeoutError`

```python
from src.models import StepTimeoutError
```

**When raised**: A step exceeds its configured `timeout` seconds.

**Typical message**: `"step 'execute' timed out after 600s"`

**Raised by**: `run_step()` in `step_runner.py`.

**What happens**: The process is killed (`SIGKILL`), waited on for 5s, then `SIGTERM` if still alive. The flow catches the error and the task fails immediately.

**How to handle**: Increase the step's `timeout` in the manifest, or optimize the step.

## `ManifestValidationError`

```python
from src.models import ManifestValidationError
```

**When raised**: A subprocess step manifest has invalid content.

**Typical messages**:
- `"manifest name: must be non-empty"`
- `"manifest command: must be non-empty"`
- `"manifest timeout: must be positive, got -1"`

**Raised by**: Manifest validation when loading subprocess step manifests.

**How to handle**: Fix the manifest JSON file. Only relevant when using subprocess steps (the escape hatch).

## `StoreError`

```python
from src.models import StoreError
```

**When raised**: Database-level failures.

**Typical messages**:
- `"task 42 already exists"` — duplicate task_id on insert.
- `"schema_version not found in meta table"` — corrupt database.
- `"schema version mismatch: expected 1, got 2"` — database from newer TAO version.

**Raised by**: `Store` methods.

**How to handle**:
- Duplicate task: use a different task_id or check existence first.
- Schema issues: delete `.tao/` directory to reset, or upgrade TAO.

## `ProviderError`

```python
from src.models import ProviderError
```

**When raised**: All LLM providers failed for a call.

**Typical message**: `"all providers failed for model 'opus': <last error details>"`

**Raised by**: `ProviderPool.call()`.

**How to handle**: Check that the requested model alias exists in your config, that the CLI tool is installed (`claude`, `copilot`), and that the provider is reachable.
