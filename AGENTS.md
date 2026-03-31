# TAO — Instructions for AI Agents

This file provides instructions for AI coding agents (Codex, Copilot, Cursor, Claude, etc.) working on this codebase.

## Setup

```bash
pip install -e ".[dev]"
```

## Verify your changes

```bash
make lint       # ruff check src/ tests/
make test       # python -m pytest tests/ -v
```

Both must pass before any commit.

## Architecture

TAO is a task orchestrator that drives LLM work through configurable cycles. Each task defines a `cycle` array of steps (LLM + command types) with jump keywords for flow control. Subprocess steps are available as an escape hatch for custom non-LLM logic.

### Key constraints

- **stdlib only**: no Django, SQLAlchemy, Pydantic, attrs, or async frameworks
- **LLM-direct default**: TAO calls the LLM provider directly. Subprocess steps are the escape hatch.
- **SQLite persistence**: raw `sqlite3` with WAL mode, never an ORM
- **Dataclasses**: use `to_dict()` method (never `dataclasses.asdict()` directly)
- **Enums**: inherit from `enum.StrEnum`, lowercase string values

### Module dependency DAG

Imports flow downward only. Never import upward. No circular imports.

```
models         ← stdlib only (no internal imports)
fmt            ← (stdlib only — CLI formatting)
store          ← models
policy         ← models
step_runner    ← models
gates          ← (no internal imports — command runner for cycle command steps)
flow           ← models, store, step_runner, policy, gates, providers/pool
queue          ← models, store, flow
api            ← models, store, queue, flow
server         ← models, api, flow (HTTP API)
cli            ← models, api, server, fmt
providers/*    ← models (LLMProvider protocol)
```

## Existing building blocks — use them

Before creating new types, fixtures, or helpers, check these files:

| File | Contains |
|------|----------|
| `src/models.py` | All enums, dataclasses, exceptions, `LLMProvider` protocol |
| `tests/conftest.py` | Shared fixtures: `tmp_db`, `mock_pool`, `sample_task_config` |
| `tests/factories.py` | Factory functions: `create_task`, `create_step_result`, `create_manifest`, etc. |

## Patterns to follow

### Subprocess execution
- `shell=True` requires documented trust model + `shlex.quote()` on all placeholders
- After `proc.kill()`: always `proc.wait(timeout=5)`, fallback to `proc.terminate()`, log failures

### SQLite
- Enable WAL mode on every new connection
- Use `_execute_with_retry` for all write operations
- Wrap `json.loads()` in try/except — never let corrupt JSON crash the app

### Error handling
- Expected errors (`TaoError` subtypes) → clean message to stderr, exit 1
- Unexpected errors → `logger.exception()` with full traceback, exit 2
- JSON input from CLI → always catch `json.JSONDecodeError` explicitly

### Tests
- All fixtures use `tmp_path` for filesystem isolation
- Use factory functions from `tests/factories.py` — never mock what you can create
- Test naming: `test_<module>_<scenario>`
- Mark slow tests (>1s) with `@pytest.mark.slow`

## Setting up and running a task

Submit a task with `title`, `body`, `cwd`, `cycle` (step sequence), and optionally `scope` (for decomposition). TAO calls the LLM directly by default — no pack directories needed.

Full guide: `docs/getting-started.md`. For subprocess escape hatch: `docs/step-protocol.md`.

Complete reference docs in `docs/`: config-reference, step-protocol, api-reference, task-lifecycle, workspace-and-hooks, errors.

## Adding a new module

1. Create `src/<module>.py` with a docstring describing its purpose
2. Create `tests/test_<module>.py`
3. Add factory functions to `tests/factories.py` if the module introduces new types
4. Update `src/__init__.py` if the module exports public API
5. Verify imports respect the dependency DAG
6. Run `make lint && make test`
