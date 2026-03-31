"""Flow policies — validation and limit enforcement.


Engine policies: iteration limits, subtask limits.
Cycle config validation.
"""

from __future__ import annotations

import dataclasses

from src.models import CycleConfig, FlowPolicies, TaoError

# Validation ranges for FlowPolicies fields.
_RANGES: dict[str, tuple[int, int]] = {
    "max_subtasks": (1, 100),
    "timeout_per_step": (1, 3600),
    "batch_size": (1, 50),
    "max_iterations": (1, 100),
}


def validate_policies(data: dict) -> FlowPolicies:
    """Validate a raw config dict and return a FlowPolicies instance.

    Unknown keys are silently ignored (forward compatibility with TOML configs
    that may contain keys for other modules).

    Raises:
        TaoError: if any value is out of its allowed range or wrong type.
    """
    known_fields = {f.name for f in dataclasses.fields(FlowPolicies)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    policies = FlowPolicies.from_dict(filtered)

    for field_name, (lo, hi) in _RANGES.items():
        value = getattr(policies, field_name)
        if not (lo <= value <= hi):
            raise TaoError(f"{field_name} must be between {lo} and {hi}, got {value}")

    return policies


def validate_cycle_config(cc: CycleConfig) -> None:
    """Validate a CycleConfig. Raises TaoError on invalid config.

    Checks:
        - Step IDs are unique
        - type is "llm" or "command"
        - LLM steps have non-empty prompt
        - Command steps have non-empty commands
        - on_fail only on command steps, references existing ID
        - next references existing ID
        - max_retries between 1 and 20
    """
    if not (1 <= cc.max_retries <= 20):
        raise TaoError(f"max_retries must be between 1 and 20, got {cc.max_retries}")

    ids = [s.id for s in cc.steps]
    if len(ids) != len(set(ids)):
        dupes = [i for i in ids if ids.count(i) > 1]
        raise TaoError(f"duplicate step IDs: {sorted(set(dupes))}")

    id_set = set(ids)

    for step in cc.steps:
        if step.type not in ("llm", "command"):
            raise TaoError(
                f"step '{step.id}': type must be 'llm' or 'command', got '{step.type}'"
            )

        if step.type == "llm" and not step.prompt:
            raise TaoError(f"step '{step.id}': LLM steps must have a non-empty prompt")

        if step.type == "command" and not step.commands:
            raise TaoError(f"step '{step.id}': command steps must have non-empty commands")

        if step.on_fail:
            if step.type != "command":
                raise TaoError(f"step '{step.id}': on_fail is only valid for command steps")
            if step.on_fail not in id_set:
                raise TaoError(
                    f"step '{step.id}': on_fail references unknown step '{step.on_fail}'"
                )

        if step.next and step.next not in id_set:
            raise TaoError(
                f"step '{step.id}': next references unknown step '{step.next}'"
            )


def check_iteration_limit(iteration: int, max_iterations: int) -> bool:
    """Check whether *iteration* exceeds *max_iterations*.

    Returns:
        True if the limit is exceeded (caller should checkpoint), False otherwise.
    """
    return iteration > max_iterations


def check_subtask_limit(count: int, max_subtasks: int) -> None:
    """Raise ``TaoError`` if *count* exceeds *max_subtasks*."""
    if count > max_subtasks:
        raise TaoError(f"subtask limit exceeded: {count} > {max_subtasks}")
