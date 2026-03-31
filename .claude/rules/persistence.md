# SQLite & Persistence Patterns

## WAL mode

Every new connection MUST enable WAL mode immediately:

```python
conn.execute("PRAGMA journal_mode=WAL")
```

No exceptions. This prevents reader/writer contention on concurrent access.

## Retry on lock contention

Use the `_execute_with_retry` pattern for all write operations:

```python
def _execute_with_retry(conn, sql, params=(), max_retries=3, delay=0.1):
    for attempt in range(max_retries):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                time.sleep(delay * (attempt + 1))
            else:
                raise
```

## JSON column safety

Always wrap `json.loads()` in try/except. Never let a corrupt JSON cell crash
the application:

```python
try:
    result = json.loads(raw)
except json.JSONDecodeError:
    logger.warning("corrupt JSON in column %s: %r", col_name, raw)
    result = {}  # or [] depending on expected type
```

## Checkpoint validation on resume

When restoring from checkpoint, validate saved step index against the current
cycle configuration. If the checkpoint references a step that no longer exists:
- Log a warning with the stale step info
- Clear the stale checkpoint
- Never silently skip or double-run steps

## Schema version tracking

Include a `schema_version` in a metadata table or as a constant. Check version
on connect — fail clearly if schema is newer than code expects. Even a comment
documenting the version is acceptable for v0.1.
