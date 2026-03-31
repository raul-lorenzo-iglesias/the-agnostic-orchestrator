"""Factory functions for TAO test data.

Each factory builds a valid instance with sensible defaults.
Pass keyword overrides to customize specific fields.
"""

from __future__ import annotations

from typing import Any

from src.models import (
    CycleConfig,
    CycleStep,
    FlowPolicies,
    HooksConfig,
    StepManifest,
    StepResult,
    StepStatus,
    WorkspaceConfig,
)


def create_step_result(**overrides: Any) -> StepResult:
    """Create a StepResult with sensible defaults."""
    defaults: dict[str, Any] = {
        "status": StepStatus.SUCCEEDED,
        "output": "ok",
        "data": {},
        "blocked_reason": "",
        "cost_usd": 0.0,
        "tokens_in": 0,
        "tokens_out": 0,
        "elapsed_s": 0.0,
        "session_id": "",
    }
    defaults.update(overrides)
    return StepResult(**defaults)


def create_manifest(**overrides: Any) -> StepManifest:
    """Create a StepManifest with sensible defaults."""
    defaults: dict[str, Any] = {
        "name": "test_step",
        "command": "echo ok",
        "needs": [],
        "provides": [],
        "timeout": 300,
    }
    defaults.update(overrides)
    return StepManifest(**defaults)


def create_workspace_config(**overrides: Any) -> WorkspaceConfig:
    """Create a WorkspaceConfig with sensible defaults (all empty strings)."""
    defaults: dict[str, Any] = {
        "create": "",
        "persist": "",
        "deliver": "",
        "cleanup": "",
    }
    defaults.update(overrides)
    return WorkspaceConfig(**defaults)


def create_flow_policies(**overrides: Any) -> FlowPolicies:
    """Create FlowPolicies with sensible defaults."""
    defaults: dict[str, Any] = {
        "max_subtasks": 20,
        "timeout_per_step": 300,
        "batch_size": 5,
        "max_iterations": 10,
    }
    defaults.update(overrides)
    return FlowPolicies(**defaults)


def create_cycle_step(**overrides: Any) -> CycleStep:
    """Create a CycleStep with sensible defaults (LLM type)."""
    defaults: dict[str, Any] = {
        "id": "implement",
        "type": "llm",
        "prompt": "Implement this subtask.",
        "model_spec": "sonnet",
        "commands": [],
        "on_fail": "",
        "next": "",
        "timeout": 1800,
        "failover": [],
    }
    defaults.update(overrides)
    return CycleStep(**defaults)


def create_cycle_config(**overrides: Any) -> CycleConfig:
    """Create a CycleConfig with sensible defaults (single implement step)."""
    defaults: dict[str, Any] = {
        "steps": [create_cycle_step()],
        "max_retries": 3,
    }
    defaults.update(overrides)
    # If steps are dicts, convert to CycleStep
    if defaults["steps"] and isinstance(defaults["steps"][0], dict):
        defaults["steps"] = [CycleStep.from_dict(s) for s in defaults["steps"]]
    return CycleConfig(**defaults)


def create_task(**overrides: Any) -> dict[str, Any]:
    """Create a task config dict with sensible defaults.

    Returns a plain dict suitable for flow/queue config (not a DB record).
    Use ``create_task_in_store`` for persisted tasks.
    """
    defaults: dict[str, Any] = {
        "task_id": 1,
        "title": "Test task",
        "body": "",
        "workspace_config": WorkspaceConfig(),
        "hooks_config": HooksConfig(),
        "policies": FlowPolicies(),
        "scope": {},
        "cycle": [],
    }
    defaults.update(overrides)
    return defaults


def create_task_in_store(store: Any, **overrides: Any) -> dict[str, Any]:
    """Create a task in the Store and return its dict representation."""
    defaults: dict[str, Any] = {
        "task_id": 1,
        "title": "Test task",
        "body": "",
        "config": {},
    }
    defaults.update(overrides)
    store.create_task(**defaults)
    return store.get_task(defaults["task_id"])


def create_provider_pool(
    providers: list[Any] | None = None,
    model_map: dict[str, list[str]] | None = None,
) -> Any:
    """Create a ProviderPool with sensible defaults (one FakeProvider).

    Args:
        providers: List of LLMProvider instances. Defaults to a single FakeProvider.
        model_map: Model alias → provider names. Defaults to opus/sonnet → fake.
    """
    from src.providers.pool import ProviderPool
    from tests.conftest import FakeProvider

    if providers is None:
        providers = [FakeProvider()]
    if model_map is None:
        model_map = {
            "opus": [p.name for p in providers],
            "sonnet": [p.name for p in providers],
        }
    return ProviderPool(providers=providers, model_map=model_map)


def create_checkpoint(**overrides: Any) -> dict[str, Any]:
    """Create a checkpoint dict with sensible defaults."""
    defaults: dict[str, Any] = {
        "task_id": 1,
        "completed_subtasks": [],
        "pending_subtasks": [{"title": "sub1", "description": "desc1"}],
        "workspace_path": "/tmp/workspace",
        "task_context": {"completed_summaries": "", "iteration": 1},
        "blocked_reason": None,
        "batch_number": 1,
    }
    defaults.update(overrides)
    return defaults


def create_engine_config(**overrides: Any) -> dict[str, Any]:
    """Create an engine config dict matching TOML structure.

    Top-level keys: ``engine`` (queue settings) and ``providers``.
    """
    defaults: dict[str, Any] = {
        "engine": {
            "max_concurrent": 5,
            "db_path": ".tao/engine.db",
        },
        "providers": {},
    }
    defaults.update(overrides)
    return defaults


def create_trace(**overrides: Any) -> dict[str, Any]:
    """Create a trace dict with sensible defaults."""
    defaults: dict[str, Any] = {
        "subtask_index": 0,
        "role": "implement",
        "model": "opus",
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": 0.01,
        "elapsed_s": 5.0,
        "success": True,
        "attempt": 1,
    }
    defaults.update(overrides)
    return defaults
