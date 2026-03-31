"""Tests for tao.policy — validation and limit enforcement."""

import pytest

from src.models import CycleConfig, CycleStep, FlowPolicies, TaoError
from src.policy import (
    check_iteration_limit,
    check_subtask_limit,
    validate_cycle_config,
    validate_policies,
)

# --- validate_policies ---


def test_policy_validate_defaults():
    """Empty dict returns FlowPolicies with all defaults."""
    p = validate_policies({})
    assert p == FlowPolicies()


def test_policy_validate_custom_values():
    """Custom values for all fields round-trip correctly."""
    data = {
        "max_subtasks": 50,
        "timeout_per_step": 600,
        "batch_size": 10,
        "max_iterations": 25,
    }
    p = validate_policies(data)
    assert p.max_subtasks == 50
    assert p.timeout_per_step == 600
    assert p.batch_size == 10
    assert p.max_iterations == 25


@pytest.mark.parametrize("value", [0, 101])
def test_policy_validate_max_subtasks_out_of_range(value):
    """max_subtasks outside [1, 100] raises TaoError."""
    with pytest.raises(TaoError, match="max_subtasks"):
        validate_policies({"max_subtasks": value})


@pytest.mark.parametrize("value", [0, 51])
def test_policy_validate_batch_size_out_of_range(value):
    """batch_size outside [1, 50] raises TaoError."""
    with pytest.raises(TaoError, match="batch_size"):
        validate_policies({"batch_size": value})


@pytest.mark.parametrize("value", [0, 101])
def test_policy_validate_max_iterations_out_of_range(value):
    """max_iterations outside [1, 100] raises TaoError."""
    with pytest.raises(TaoError, match="max_iterations"):
        validate_policies({"max_iterations": value})


def test_policy_validate_ignores_unknown_keys():
    """Extra keys in input dict are silently ignored."""
    p = validate_policies({"session_chaining": True, "unknown_key": 42})
    assert p == FlowPolicies()


# --- validate_cycle_config ---


def test_policy_validate_cycle_config_valid():
    """Valid cycle config passes without error."""
    cc = CycleConfig(
        steps=[
            CycleStep(id="plan", type="llm", prompt="Plan."),
            CycleStep(id="implement", type="llm", prompt="Implement."),
            CycleStep(id="validate", type="command", commands=["pytest"], on_fail="fix"),
            CycleStep(id="fix", type="llm", prompt="Fix errors.", next="validate"),
        ],
        max_retries=3,
    )
    validate_cycle_config(cc)  # should not raise


def test_policy_validate_cycle_config_duplicate_ids():
    """Duplicate step IDs raise TaoError."""
    cc = CycleConfig(
        steps=[
            CycleStep(id="plan", type="llm", prompt="A."),
            CycleStep(id="plan", type="llm", prompt="B."),
        ],
    )
    with pytest.raises(TaoError, match="duplicate step IDs"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_invalid_type():
    """Invalid step type raises TaoError."""
    cc = CycleConfig(
        steps=[CycleStep(id="x", type="invalid", prompt="A.")],
    )
    with pytest.raises(TaoError, match="type must be"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_llm_no_prompt():
    """LLM step without prompt raises TaoError."""
    cc = CycleConfig(
        steps=[CycleStep(id="x", type="llm", prompt="")],
    )
    with pytest.raises(TaoError, match="non-empty prompt"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_command_no_commands():
    """Command step without commands raises TaoError."""
    cc = CycleConfig(
        steps=[CycleStep(id="x", type="command", commands=[])],
    )
    with pytest.raises(TaoError, match="non-empty commands"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_on_fail_only_command():
    """on_fail on LLM step raises TaoError."""
    cc = CycleConfig(
        steps=[
            CycleStep(id="x", type="llm", prompt="A.", on_fail="x"),
        ],
    )
    with pytest.raises(TaoError, match="on_fail is only valid for command"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_on_fail_unknown_target():
    """on_fail referencing unknown step raises TaoError."""
    cc = CycleConfig(
        steps=[
            CycleStep(id="validate", type="command", commands=["pytest"], on_fail="nonexistent"),
        ],
    )
    with pytest.raises(TaoError, match="on_fail references unknown"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_next_unknown_target():
    """next referencing unknown step raises TaoError."""
    cc = CycleConfig(
        steps=[
            CycleStep(id="x", type="llm", prompt="A.", next="nonexistent"),
        ],
    )
    with pytest.raises(TaoError, match="next references unknown"):
        validate_cycle_config(cc)


def test_policy_validate_cycle_config_max_retries_out_of_range():
    """max_retries outside [1, 20] raises TaoError."""
    cc = CycleConfig(
        steps=[CycleStep(id="x", type="llm", prompt="A.")],
        max_retries=0,
    )
    with pytest.raises(TaoError, match="max_retries"):
        validate_cycle_config(cc)

    cc2 = CycleConfig(
        steps=[CycleStep(id="x", type="llm", prompt="A.")],
        max_retries=21,
    )
    with pytest.raises(TaoError, match="max_retries"):
        validate_cycle_config(cc2)


# --- check_iteration_limit ---


def test_policy_check_iteration_within():
    """Iteration within limit returns False."""
    assert check_iteration_limit(5, 10) is False


def test_policy_check_iteration_exceeded():
    """Iteration exceeding limit returns True."""
    assert check_iteration_limit(11, 10) is True


# --- check_subtask_limit ---


def test_policy_check_subtask_within():
    """Subtask count within limit does not raise."""
    check_subtask_limit(5, 20)  # should not raise


def test_policy_check_subtask_exceeded():
    """Subtask count exceeding limit raises TaoError."""
    with pytest.raises(TaoError, match="subtask limit exceeded"):
        check_subtask_limit(21, 20)
