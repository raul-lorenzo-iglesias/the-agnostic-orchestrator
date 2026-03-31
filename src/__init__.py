# TAO — The Agnostic Orchestrator
# A generic, language-agnostic engine that orchestrates LLM work.

__version__ = "1.0.0"

from src.api import Engine, load_config
from src.flow import request_stop, run_flow
from src.gates import run_gate_command
from src.policy import (
    check_iteration_limit,
    check_subtask_limit,
    validate_policies,
)
from src.providers import ClaudeCliProvider, CopilotCliProvider, ProviderPool, parse_model_spec, run_llm_service
from src.queue import QueueManager
from src.step_runner import (
    format_template_cmd,
    run_step,
    validate_context,
)
from src.store import Store

__all__ = [
    "Engine",
    "ClaudeCliProvider",
    "CopilotCliProvider",
    "ProviderPool",
    "QueueManager",
    "Store",
    "request_stop",
    "run_flow",
    "check_iteration_limit",
    "check_subtask_limit",
    "format_template_cmd",
    "parse_model_spec",
    "run_gate_command",
    "run_step",
    "validate_context",
    "run_llm_service",
    "load_config",
    "validate_policies",
]
