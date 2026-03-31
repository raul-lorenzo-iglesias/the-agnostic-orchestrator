"""LLM provider infrastructure — ProviderPool, failover, model routing.


Providers implement the LLMProvider protocol (defined in src/models.py).
ProviderPool handles model routing and failover across providers.
"""

from src.providers.claude import ClaudeCliProvider
from src.providers.copilot import CopilotCliProvider
from src.providers.llm_service import run_llm_service
from src.providers.pool import ProviderPool, parse_model_spec

__all__ = ["ClaudeCliProvider", "CopilotCliProvider", "ProviderPool", "parse_model_spec", "run_llm_service"]
