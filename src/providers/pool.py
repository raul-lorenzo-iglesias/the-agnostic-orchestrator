"""ProviderPool — multi-provider LLM scheduling with failover.


Multiple providers (Claude CLI, Copilot CLI, etc.).
Model routing: resolves which provider serves the requested model.
Failover: if one provider fails, tries the next.
"""

from __future__ import annotations

import logging
from typing import Any

from src.models import LLMProvider, ProviderError

logger = logging.getLogger(__name__)


def parse_model_spec(spec: str) -> tuple[str, str | None]:
    """Parse a model spec string into (model, provider_name).

    Accepts:
        "opus@claude_cli" → ("opus", "claude_cli")
        "opus" → ("opus", None)

    Returns:
        Tuple of (model_alias, provider_name_or_None).
    """
    if "@" in spec:
        model, provider = spec.split("@", 1)
        return model.strip(), provider.strip()
    return spec.strip(), None


class ProviderPool:
    """Routes LLM calls to providers with model-based routing and failover.

    Args:
        providers: List of LLMProvider instances to register.
        model_map: Maps model alias → ordered list of provider names.
                   First provider in the list is tried first; others are fallbacks.

    Raises:
        ProviderError: If a provider name in model_map is not in the provider list.
    """

    def __init__(
        self,
        providers: list[LLMProvider],
        model_map: dict[str, list[str]],
    ) -> None:
        self._providers: dict[str, LLMProvider] = {}
        self._model_map: dict[str, list[str]] = {}

        for provider in providers:
            self._providers[provider.name] = provider

        for model, provider_names in model_map.items():
            for name in provider_names:
                if name not in self._providers:
                    msg = f"model_map references unknown provider '{name}'"
                    raise ProviderError(msg)
            self._model_map[model] = list(provider_names)

    def register(self, provider: LLMProvider, models: list[str]) -> None:
        """Add a provider and bind it to the given model aliases.

        If the provider is already registered, it is replaced. For each model
        alias, the provider is appended to the end of the fallback list.
        """
        self._providers[provider.name] = provider
        for model in models:
            if model not in self._model_map:
                self._model_map[model] = []
            if provider.name not in self._model_map[model]:
                self._model_map[model].append(provider.name)

    def call(
        self,
        *,
        prompt: str,
        model: str,
        tools: list[str],
        timeout: int,
        cwd: str | None = None,
        resume_session_id: str | None = None,
        provider: str | None = None,
    ) -> dict[str, Any]:
        """Route an LLM call to the appropriate provider with failover.

        If ``provider`` is given, routes directly to that provider (no model_map
        lookup). Supports ``model@provider`` format via ``parse_model_spec()``.

        Returns:
            The raw dict from the successful provider's call().

        Raises:
            ProviderError: If model/provider is not found, or all providers fail.
        """
        if provider is not None:
            # Direct routing — skip model_map
            if provider not in self._providers:
                raise ProviderError(f"unknown provider: '{provider}'")
            provider_names = [provider]
        else:
            provider_names = self._model_map.get(model)
            if not provider_names:
                # No explicit mapping — try all providers (pass model alias through)
                provider_names = list(self._providers.keys())
                if not provider_names:
                    raise ProviderError(f"no providers registered for model '{model}'")

        errors: list[tuple[str, Exception]] = []
        for name in provider_names:
            provider = self._providers[name]
            try:
                return provider.call(
                    prompt,
                    model=model,
                    tools=tools,
                    timeout=timeout,
                    cwd=cwd,
                    resume_session_id=resume_session_id,
                )
            except Exception as exc:
                errors.append((name, exc))
                logger.warning("provider '%s' failed for model '%s': %s", name, model, exc)

        details = "; ".join(f"{name}: {exc}" for name, exc in errors)
        msg = f"all providers failed for model '{model}': {details}"
        raise ProviderError(msg)
