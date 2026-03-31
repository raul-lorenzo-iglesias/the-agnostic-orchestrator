# Contributing to TAO

## Setup

```bash
git clone https://github.com/raul-lorenzo-iglesias/the-agnostic-orchestrator.git
cd the-agnostic-orchestrator
pip install -e ".[dev]"
make test
```

## Development workflow

1. Create a branch: `git checkout -b feature/description`
2. Make changes
3. Run `make lint && make test` — both must pass
4. Commit with a descriptive message
5. Open a PR

## Code style

- **Formatter**: `ruff format` (line length 99, Python 3.11+)
- **Linter**: `ruff check` with rules E, F, W, I, UP
- Run `make format` to auto-fix

## Architecture rules

- **stdlib only** — no Django, SQLAlchemy, Pydantic, or attrs
- **LLM-direct by default** — TAO calls the LLM provider directly. Subprocess steps are the escape hatch.
- **Module imports go downward** — see the dependency DAG in `CLAUDE.md` or `AGENTS.md`
- **Use existing building blocks** — `src/models.py` for types, `tests/factories.py` for test data

## Testing

```bash
make test       # run all tests
make lint       # check code style
```

- Tests use `tmp_path` for filesystem isolation
- Use factory functions from `tests/factories.py` instead of mocks
- Name tests as `test_<module>_<scenario>`
- Every new module needs a corresponding test file

## Reference docs

- **[API Reference](docs/api-reference.md)** — all Engine methods, signatures, return types, errors
- **[Error Reference](docs/errors.md)** — error hierarchy and handling guidance
- **[Step Protocol](docs/step-protocol.md)** — subprocess escape hatch: stdin/stdout spec for step executables

## Adding a new LLM provider

1. Create `src/providers/<name>.py`
2. Implement the `LLMProvider` protocol from `src/models.py`:

```python
from src.models import LLMProvider

class MyProvider:
    name: str = "my_provider"

    def __init__(self, models: dict[str, str] | None = None):
        self.models = models or {}

    def call(self, prompt, *, model, tools, timeout, cwd=None, resume_session_id=None):
        # ... call your LLM ...
        return {
            "success": True,
            "output": "response text",
            "elapsed_s": 1.2,
            "cost_usd": 0.01,
            "tokens_in": 100,
            "tokens_out": 200,
            "session_id": "",
        }
```

3. Register the provider type in `src/api.py` → `_PROVIDER_TYPES`
4. Add tests in `tests/test_providers.py`
5. Run `make lint && make test`
