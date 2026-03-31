"""Shared pytest fixtures for TAO test suite."""

from __future__ import annotations

import json
import sqlite3

import pytest

from src.models import (
    FlowPolicies,
    HooksConfig,
    WorkspaceConfig,
)

# --- FakeProvider (structurally satisfies LLMProvider protocol) ---


class FakeProvider:
    """Test double for LLM providers. Records calls, returns canned responses."""

    name: str = "fake"

    def __init__(self, responses: list[dict] | None = None):
        self.responses: list[dict] = list(responses or [])
        self.calls: list[dict] = []

    def call(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        timeout: int,
        cwd: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict:
        self.calls.append(
            {
                "prompt": prompt,
                "model": model,
                "tools": tools,
                "timeout": timeout,
                "cwd": cwd,
                "resume_session_id": resume_session_id,
            }
        )
        if self.responses:
            return self.responses.pop(0)
        # Default: return scope-compatible JSON if prompt looks like scope,
        # otherwise return plain text.
        output = "ok"
        if "decompose" in prompt.lower() or "subtask" in prompt.lower():
            output = json.dumps(
                [{"title": "Subtask 1", "description": "Do the work"}]
            )
        return {
            "success": True,
            "output": output,
            "elapsed_s": 0.1,
            "cost_usd": 0.0,
            "tokens_in": 10,
            "tokens_out": 20,
            "session_id": "",
        }


# --- Fixtures ---


@pytest.fixture
def tmp_db(tmp_path):
    """SQLite connection with WAL mode in a temp directory."""
    db_path = tmp_path / "tao_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    yield conn
    conn.close()


@pytest.fixture
def store(tmp_path):
    """Store instance backed by a temp SQLite database."""
    from src.store import Store

    db_path = str(tmp_path / "tao_test.db")
    s = Store(db_path)
    yield s
    s.close()


class FailingProvider:
    """Provider that always raises — for failover tests."""

    name: str = "failing"

    def call(
        self,
        prompt: str,
        *,
        model: str,
        tools: list[str],
        timeout: int,
        cwd: str | None = None,
        resume_session_id: str | None = None,
    ) -> dict:
        raise RuntimeError("provider failed")


@pytest.fixture
def mock_pool():
    """ProviderPool with a FakeProvider for all models."""
    from src.providers.pool import ProviderPool

    provider = FakeProvider()
    pool = ProviderPool(
        providers=[provider],
        model_map={"opus": ["fake"], "sonnet": ["fake"]},
    )
    return pool


@pytest.fixture
def engine(tmp_path):
    """Engine instance with temp DB and no real providers."""
    from src.api import Engine

    db_path = str(tmp_path / "engine.db")
    config = {"engine": {"db_path": db_path}}
    e = Engine(config=config)
    yield e
    e.close()


@pytest.fixture
def sample_task_config():
    """Config dict with workspace, hooks, policies, scope, and cycle."""
    return {
        "workspace": WorkspaceConfig(),
        "hooks": HooksConfig(),
        "policies": FlowPolicies(),
        "scope": {"model_spec": "opus", "timeout": 300},
        "cycle": [
            {
                "id": "plan",
                "type": "llm",
                "prompt": "Plan the implementation.",
                "model_spec": "opus",
                "timeout": 300,
            },
            {
                "id": "implement",
                "type": "llm",
                "prompt": "Implement the plan.",
                "model_spec": "sonnet",
                "timeout": 600,
            },
        ],
    }
