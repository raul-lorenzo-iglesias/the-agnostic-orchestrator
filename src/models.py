"""Shared type vocabulary for TAO — enums, dataclasses, exceptions, protocols."""

from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


def _enum_dict_factory(items: list[tuple[str, Any]]) -> dict[str, Any]:
    return {k: v.value if isinstance(v, enum.Enum) else v for k, v in items}


# --- Enums (all str-backed for natural JSON serialization) ---


class TaskStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Statuses that can be deleted (terminal + stopped, since stopped is resumable but dead-end-able)
DELETABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.STOPPED}
)


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepRole(enum.StrEnum):
    SCOPE = "scope"


# --- Dataclasses ---


@dataclass
class StepResult:
    status: StepStatus
    output: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str = ""
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    elapsed_s: float = 0.0
    session_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepResult:
        known = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        filtered["status"] = StepStatus(filtered["status"])
        return cls(**filtered)


@dataclass
class StepManifest:
    name: str
    command: str = ""
    needs: list[str] = field(default_factory=list)
    provides: list[str] = field(default_factory=list)
    timeout: int = 300

    @property
    def is_llm_direct(self) -> bool:
        """True if this step should be executed via LLM-direct (no subprocess)."""
        return not self.command

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepManifest:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class CycleStep:
    """A single step in a configurable cycle.

    type="llm": calls the LLM with prompt + model_spec.
    type="command": runs shell commands in order.
    """

    id: str
    type: str
    prompt: str = ""
    model_spec: str = ""
    commands: list[str] = field(default_factory=list)
    on_fail: str = ""
    next: str = ""
    timeout: int = 1800
    failover: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleStep:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class CycleConfig:
    """Configurable cycle definition — the sequence of steps within each subtask."""

    steps: list[CycleStep] = field(default_factory=list)
    max_retries: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "max_retries": self.max_retries,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CycleConfig:
        steps = [CycleStep.from_dict(s) for s in data.get("steps", [])]
        return cls(steps=steps, max_retries=data.get("max_retries", 3))


@dataclass
class WorkspaceConfig:
    create: str = ""
    persist: str = ""
    deliver: str = ""
    cleanup: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceConfig:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class HooksConfig:
    on_step_output: str = ""
    on_scope_complete: str = ""
    on_blocked: str = ""
    on_flow_complete: str = ""
    on_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HooksConfig:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class FlowPolicies:
    max_subtasks: int = 20
    timeout_per_step: int = 900
    batch_size: int = 5
    max_iterations: int = 10

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self, dict_factory=_enum_dict_factory)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FlowPolicies:
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


# --- Exceptions ---


class TaoError(Exception):
    """Base for all expected TAO errors. CLI catches these for clean output."""


class TaskNotFoundError(TaoError):
    """Raised when a task_id doesn't exist in the store."""


class StepTimeoutError(TaoError):
    """Raised when a step exceeds its configured timeout."""


class ManifestValidationError(TaoError):
    """Raised when a step manifest fails validation."""


class StoreError(TaoError):
    """Raised on persistence failures (corrupt DB, schema mismatch)."""


class ProviderError(TaoError):
    """Raised when all providers fail for an LLM call."""


# --- Protocols ---


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    def call(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        timeout: int,
        cwd: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict[str, Any]: ...
