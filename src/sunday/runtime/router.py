"""Multi-provider router with auto-fallback.

Adapted from the spirit of hermes/agent/auxiliary_client.py (MIT, (c) 2025
Nous Research) — that file's resolution chain is the gold standard but
also heavy with Hermes-specific concerns (Nous Portal auth, Codex OAuth,
vision routing, etc). Sunday's version is the Hermes pattern stripped to
what fits our single-user-personal goal:

  Primary provider (per config)  →  on 402 / credit exhausted / rate limit:
  Fallback chain in order, each provider tried once, first non-error wins.

Default chain:
  1. The provider explicitly chosen by config.model.provider
  2. OpenRouter   (if OPENROUTER_API_KEY set)
  3. OpenAI       (if OPENAI_API_KEY set)
  4. DeepSeek     (if DEEPSEEK_API_KEY set)
  (Same provider isn't tried twice.)
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.runtime.types import CompletionResult, DeltaHandler, Provider

log = structlog.get_logger("sunday.runtime.router")

# Substrings that indicate "the provider can't service this request — try
# another." Borrowed from Hermes's auxiliary_client.py credit-exhausted
# detection list.
_FAILOVER_HINTS = (
    "402",
    "insufficient_quota",
    "credit",
    "credits",
    "rate_limit",
    "rate limit",
    "out of credits",
    "billing",
    "payment",
)


def _is_failover_worthy(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _FAILOVER_HINTS)


def _credentialed_providers(config: SundayConfig) -> list[str]:
    """Order of providers to TRY based on which credentials exist on this host."""
    primary = config.model.provider
    chain: list[str] = [primary]
    for candidate in ("openrouter", "openai", "deepseek-direct"):
        if candidate == primary:
            continue
        cred_key = {
            "openrouter": "OPENROUTER_API_KEY",
            "openai": "OPENAI_API_KEY",
            "deepseek-direct": "DEEPSEEK_API_KEY",
        }[candidate]
        if get_credential(cred_key):
            chain.append(candidate)
    return chain


def _build_provider(config: SundayConfig, provider_name: str) -> Provider:
    """Build a Provider for a specific backend, with a per-call config clone
    so the swap doesn't mutate the global Daemon.config."""
    cloned = copy.copy(config)
    cloned.model = replace(config.model, provider=provider_name)
    # Reasonable default model + base_url per provider — only used when
    # the caller hasn't overridden them. The primary provider keeps the
    # user's config; fallbacks pick a sensible default for THAT provider.
    if provider_name != config.model.provider:
        defaults = {
            "openrouter":      ("deepseek/deepseek-chat",         "https://openrouter.ai/api/v1"),
            "openai":          ("gpt-4o-mini",                    "https://api.openai.com/v1"),
            "deepseek-direct": ("deepseek-chat",                  "https://api.deepseek.com/v1"),
        }
        if provider_name in defaults:
            model, base_url = defaults[provider_name]
            cloned.model = replace(cloned.model, name=model, base_url=base_url)

    from sunday.runtime.providers.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(cloned)


class RouterProvider:
    """Wraps multiple providers, tries them in order, fails over on
    credit/rate-limit errors. The agent loop calls this exactly like a
    single provider — failover is invisible from above."""

    name = "router"

    def __init__(self, config: SundayConfig) -> None:
        self.config = config
        self._chain = _credentialed_providers(config)
        log.info("router built", chain=self._chain)

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        on_delta: DeltaHandler | None = None,
    ) -> CompletionResult:
        last_exc: BaseException | None = None
        for provider_name in self._chain:
            try:
                provider = _build_provider(self.config, provider_name)
                result = await provider.complete(
                    system_prompt=system_prompt,
                    messages=messages,
                    tools_schema=tools_schema,
                    on_delta=on_delta,
                )
                if provider_name != self._chain[0]:
                    log.info("provider failover succeeded", used=provider_name, primary=self._chain[0])
                return result
            except RuntimeError as exc:
                # Config / credential errors — try the next provider quietly.
                log.info("provider unavailable", provider=provider_name, error=str(exc))
                last_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001
                if _is_failover_worthy(exc):
                    log.warning("provider failover triggered", provider=provider_name, error=str(exc)[:200])
                    last_exc = exc
                    continue
                # Permanent failure — surface it.
                raise

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("no providers configured")
