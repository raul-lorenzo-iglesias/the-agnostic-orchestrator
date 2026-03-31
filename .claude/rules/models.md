# Dataclass & Enum Patterns

## Canonical dataclass structure

All dataclasses in `src/models.py` follow the same pattern:
- Fields with type annotations and sensible defaults
- `to_dict()` instance method using `_enum_dict_factory`
- `from_dict(cls, data)` classmethod for deserialization

## Never use dataclasses.asdict() directly

Always use the `to_dict()` method which wraps `asdict()` with
`_enum_dict_factory`. Direct `asdict()` leaves enum members as objects
instead of their string values:

```python
def to_dict(self):
    return dataclasses.asdict(self, dict_factory=_enum_dict_factory)
```

## from_dict() validation

Enum fields must be explicitly constructed in `from_dict()`:

```python
@classmethod
def from_dict(cls, data):
    data = dict(data)  # don't mutate input
    data["status"] = StepStatus(data["status"])  # raises ValueError if invalid
    return cls(**data)
```

Never rely on implicit enum conversion. The explicit constructor gives clear
error messages on invalid values.

## Protocols for interfaces

Use `typing.Protocol` (with `@runtime_checkable` where needed) for defining
interfaces like `LLMProvider`. No `abc.ABC` or `abc.abstractmethod`:

```python
@runtime_checkable
class LLMProvider(Protocol):
    name: str
    def call(self, prompt: str, *, model: str, tools: list[str],
             timeout: int, cwd: str | None = None,
             resume_session_id: str | None = None) -> dict[str, Any]: ...
```

## String-valued enums

All enums inherit from `enum.StrEnum` (Python 3.11+). Values are lowercase strings.
This enables natural JSON serialization and direct string comparison.
