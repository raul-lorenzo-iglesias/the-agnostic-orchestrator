"""Tests for tao.models — enums, dataclasses, exceptions, and protocols."""

from __future__ import annotations

import pytest

from src.models import (
    CycleConfig,
    CycleStep,
    FlowPolicies,
    LLMProvider,
    StepManifest,
    StepResult,
    StepRole,
    StepStatus,
    StepTimeoutError,
    TaoError,
    TaskNotFoundError,
    TaskStatus,
    WorkspaceConfig,
)
from tests.conftest import FakeProvider
from tests.factories import (
    create_cycle_step,
    create_manifest,
    create_step_result,
    create_workspace_config,
)

# --- Group 1: Enum string values ---


def test_models_task_status_string_values():
    expected = {"queued", "running", "completed", "failed", "blocked", "stopped", "cancelled"}
    assert {m.value for m in TaskStatus} == expected
    for member in TaskStatus:
        assert isinstance(member, str)


def test_models_step_status_string_values():
    expected = {"pending", "running", "succeeded", "failed", "skipped"}
    assert {m.value for m in StepStatus} == expected
    for member in StepStatus:
        assert isinstance(member, str)


def test_models_step_role_string_values():
    expected = {"scope"}
    assert {m.value for m in StepRole} == expected
    for member in StepRole:
        assert isinstance(member, str)


# --- Group 2: Dataclass to_dict / from_dict round-trips ---


def test_models_step_result_round_trip():
    original = create_step_result()
    d = original.to_dict()
    assert d["status"] == "succeeded"
    assert not isinstance(d["status"], StepStatus)
    restored = StepResult.from_dict(d)
    assert restored == original


def test_models_step_result_round_trip_with_data():
    original = create_step_result(data={"key": "val"}, cost_usd=1.5, tokens_in=100, tokens_out=200)
    d = original.to_dict()
    assert d["data"] == {"key": "val"}
    assert d["cost_usd"] == 1.5
    restored = StepResult.from_dict(d)
    assert restored == original


def test_models_step_manifest_round_trip():
    original = create_manifest(needs=["a"], provides=["b"])
    d = original.to_dict()
    assert d["needs"] == ["a"]
    assert d["provides"] == ["b"]
    restored = StepManifest.from_dict(d)
    assert restored == original


def test_models_workspace_config_round_trip():
    original = create_workspace_config(create="mkdir ws", cleanup="rm -rf ws")
    d = original.to_dict()
    assert d["create"] == "mkdir ws"
    restored = WorkspaceConfig.from_dict(d)
    assert restored == original


def test_models_flow_policies_round_trip():
    original = FlowPolicies(batch_size=10)
    d = original.to_dict()
    restored = FlowPolicies.from_dict(d)
    assert restored == original


def test_models_cycle_step_round_trip():
    original = create_cycle_step(
        id="plan", prompt="Plan this.", model_spec="opus",
        failover=["sonnet"], timeout=600,
    )
    d = original.to_dict()
    assert d["id"] == "plan"
    assert d["type"] == "llm"
    assert d["failover"] == ["sonnet"]
    restored = CycleStep.from_dict(d)
    assert restored == original


def test_models_cycle_step_command_round_trip():
    original = CycleStep(
        id="validate", type="command",
        commands=["pytest", "ruff check src/"],
        on_fail="fix",
    )
    d = original.to_dict()
    assert d["commands"] == ["pytest", "ruff check src/"]
    assert d["on_fail"] == "fix"
    restored = CycleStep.from_dict(d)
    assert restored == original


def test_models_cycle_config_round_trip():
    steps = [
        create_cycle_step(id="plan", prompt="Plan."),
        create_cycle_step(id="implement", prompt="Implement."),
    ]
    original = CycleConfig(steps=steps, max_retries=5)
    d = original.to_dict()
    assert len(d["steps"]) == 2
    assert d["max_retries"] == 5
    restored = CycleConfig.from_dict(d)
    assert restored == original


def test_models_cycle_config_empty():
    original = CycleConfig()
    d = original.to_dict()
    assert d["steps"] == []
    assert d["max_retries"] == 3
    restored = CycleConfig.from_dict(d)
    assert restored == original


# --- Group 3: Enum validation errors in from_dict ---


def test_models_step_result_from_dict_invalid_status():
    with pytest.raises(ValueError):
        StepResult.from_dict({"status": "invalid_status", "output": ""})


def test_models_step_result_from_dict_missing_status():
    with pytest.raises(KeyError):
        StepResult.from_dict({"output": "ok"})


# --- Group 4: LLMProvider protocol compliance ---


def test_models_fake_provider_satisfies_protocol():
    provider = FakeProvider()
    assert isinstance(provider, LLMProvider)


def test_models_fake_provider_records_calls():
    custom_response = {"custom": True}
    provider = FakeProvider(responses=[custom_response])

    result = provider.call("hello", model="opus", tools=["Read"], timeout=60)
    assert result == custom_response
    assert len(provider.calls) == 1
    assert provider.calls[0]["prompt"] == "hello"
    assert provider.calls[0]["model"] == "opus"

    # Queue exhausted — should return default response
    result2 = provider.call("again", model="sonnet", tools=[], timeout=30)
    assert result2["success"] is True
    assert result2["output"] == "ok"
    assert len(provider.calls) == 2


# --- Group 5: Exceptions hierarchy ---


def test_models_tao_error_hierarchy():
    assert issubclass(TaskNotFoundError, TaoError)
    assert issubclass(StepTimeoutError, TaoError)
    assert issubclass(TaoError, Exception)


def test_models_tao_error_catchable_as_base():
    with pytest.raises(TaoError) as exc_info:
        raise TaskNotFoundError("task 42 not found")
    assert "task 42 not found" in str(exc_info.value)


# --- Group 6: Fixture smoke tests ---


def test_models_sample_task_config_structure(sample_task_config):
    assert "workspace" in sample_task_config
    assert "hooks" in sample_task_config
    assert "policies" in sample_task_config
    assert "scope" in sample_task_config
    assert "cycle" in sample_task_config
    assert len(sample_task_config["cycle"]) == 2
    assert isinstance(sample_task_config["workspace"], WorkspaceConfig)
