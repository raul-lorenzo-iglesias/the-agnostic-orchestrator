# CLI & API Error Handling Patterns

## Two-tier error handling

Distinguish expected errors from unexpected ones in `main()`:

```python
def main():
    try:
        args = parse_args()
        args.func(args)
    except TaoError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        logger.exception("unexpected error")
        sys.exit(2)
```

- Exit 0: success
- Exit 1: expected error (user can fix — bad input, task not found)
- Exit 2: unexpected error (bug — needs investigation)

## JSON input validation

Always catch `json.JSONDecodeError` explicitly. Show what was received and
what format is expected:

```python
try:
    ctx = json.loads(raw)
except json.JSONDecodeError as e:
    print(f"error: invalid JSON in --context: {e}", file=sys.stderr)
    sys.exit(1)
```

## Error documentation in docstrings

API functions that can raise `TaoError` subtypes must document which ones:

```python
def get_task(self, task_id: str) -> dict:
    """Fetch task by ID.

    Raises:
        TaskNotFoundError: if task_id does not exist in the store.
    """
```

## Test coverage

Every CLI subcommand has a corresponding test in `test_cli.py`. Tests verify
both success paths and error paths (expected errors produce clean messages,
unexpected errors produce tracebacks).
