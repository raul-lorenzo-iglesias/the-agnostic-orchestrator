# Pytest & Testing Patterns

## Filesystem isolation

All fixtures that touch the filesystem use `tmp_path`. No test writes to the
real filesystem. No shared state between tests — each test gets its own
directory and database.

## Factory functions over mocks

Use `tests/factories.py` for creating test data. Factories have sensible
defaults and accept `**overrides`:

```python
def create_task(store, **overrides):
    defaults = {"issue_number": 1, "title": "test task", ...}
    defaults.update(overrides)
    return store.create_task(**defaults)
```

Never mock what you can create with a factory. A real `sqlite3` database in
`tmp_path` is always preferred over mocking `Store`.

Mock only external boundaries — use `FakeProvider` from `conftest.py` for LLM
provider interactions.

## Test naming

`test_<module>_<scenario>` — e.g., `test_store_create_task`,
`test_flow_retry_on_failure`. The module prefix groups tests logically.

## Slow tests

Tests expected to take >1s get `@pytest.mark.slow`. CI can skip with
`pytest -m "not slow"`.

## Fixture cataog

Before creating ad-hoc fixtures, check `tests/conftest.py`:
- `tmp_db` — tmp_path SQLite store with WAL mode
- `mock_pool` — ProviderPool with FakeProvider (configurable responses)
- `sample_task_config` — dict with cwd, cycle, scope, and policy configs
