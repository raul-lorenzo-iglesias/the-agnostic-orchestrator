# Step Execution & Subprocess Patterns

## shell=True trust model

`run_step()` executes subprocess step commands with `shell=True`. Subprocess
steps are the escape hatch (user-provided manifests with `command` field).
Document the trust assumption in `step_runner.py` docstring.

Validate manifest command structure on load:
- `name` and `command` must be non-empty strings
- `timeout` must be positive (if set)
- No bare semicolons or pipes without justification

## Template placeholder escaping

`_run_workspace_cmd()` and `_fire_hook()` format template strings before shell
execution. All placeholders MUST be escaped with `shlex.quote()`:

```python
cmd = template.format_map({k: shlex.quote(v) for k, v in values.items()})
```

Only known keys are accepted in `format_map()`. Unknown keys → raise `ValueError`.

## Subprocess cleanup after kill

After `proc.kill()`, always reap the process. Never leave zombies:

```python
proc.kill()
try:
    proc.wait(timeout=5)
except subprocess.TimeoutExpired:
    proc.terminate()
    logger.warning("process %d did not exit after kill+terminate", proc.pid)
```

Always log cleanup failures — silent zombie leaks are hard to debug.
