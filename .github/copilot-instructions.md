# TAO — Instructions for AI Assistants

Python >= 3.11 task orchestrator. Stdlib-only core. LLM-direct execution by default.

## Commands

```
pip install -e ".[dev]"
make test       # python -m pytest tests/ -v
make lint       # ruff check src/ tests/
make format     # ruff format + ruff check --fix
```

## Rules

- No ORMs, no async/await, no Pydantic — stdlib `dataclasses` + raw `sqlite3`
- No `dataclasses.asdict()` directly — use `to_dict()` with `_enum_dict_factory` from `src/models.py`
- No `shell=True` without `shlex.quote()` on all template placeholders
- After `proc.kill()`, always `proc.wait(timeout=5)` with `terminate()` fallback
- SQLite: WAL mode always, `_execute_with_retry` for writes, try/except on `json.loads()`
- Expected errors (`TaoError`) → exit 1. Unexpected → `logger.exception()` + exit 2
- All enums inherit `enum.StrEnum`. Values are lowercase strings.

## Module dependency DAG — never import upward

```
models ← stdlib only
fmt ← (stdlib only — CLI formatting)
store, policy, step_runner ← models
gates ← (no internal imports — command runner for cycle command steps)
flow ← models, store, step_runner, policy, gates, providers/pool
queue ← models, store, flow
api ← models, store, queue, flow
server ← models, api, flow
cli ← models, api, server, fmt
providers/* ← models
```

## Building blocks — use these, don't recreate

- `src/models.py` — enums, dataclasses, exceptions, LLMProvider protocol
- `tests/conftest.py` — fixtures: tmp_db, mock_pool, sample_task_config
- `tests/factories.py` — create_task, create_step_result, create_manifest, etc.

## Tests

- All fixtures use `tmp_path` — no shared state between tests
- Factories over mocks: never mock what you can create with a factory
- Naming: `test_<module>_<scenario>` (e.g. `test_store_create_task`)
- Slow tests (>1s): `@pytest.mark.slow`

## Setting up and running a task

Submit with `title`, `body`, `cwd`, `cycle` (step sequence), and optionally `scope` (for decomposition). TAO calls the LLM directly by default — no pack directories needed.

See `docs/getting-started.md` for examples. For subprocess escape hatch: `docs/step-protocol.md`.

Complete reference docs in `docs/`: config-reference, step-protocol, api-reference, task-lifecycle, workspace-and-hooks, errors.

## Adding a module

1. Create `src/<module>.py`
2. Create `tests/test_<module>.py`
3. Add factories to `tests/factories.py` if new types introduced
4. Respect the dependency DAG
5. Run `make lint && make test`
