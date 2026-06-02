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
    # Codex (ChatGPT subscription) — available whenever the machine is logged
    # into the codex CLI; no API key needed.
    if "codex" != primary:
        try:
            from sunday.runtime.providers.codex import codex_available
            if codex_available():
                chain.append("codex")
        except Exception:  # noqa: BLE001
            pass
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
        if provider_name == "codex":
            # Codex uses your ChatGPT subscription; ChatGPT accounts accept the
            # chat models, not the -codex variants.
            cloned.model = replace(cloned.model, name="gpt-5.2", base_url="")
        elif provider_name in defaults:
            model, base_url = defaults[provider_name]
            cloned.model = replace(cloned.model, name=model, base_url=base_url)

    # Ollama always points at the local OpenAI-compatible endpoint (whether it's
    # the primary or a fallback) — the model name is the user's pick.
    if provider_name == "ollama" and not cloned.model.base_url:
        cloned.model = replace(cloned.model, base_url="http://localhost:11434/v1")

    if provider_name == "codex":
        from sunday.runtime.providers.codex import CodexProvider
        return CodexProvider(cloned)
    from sunday.runtime.providers.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(cloned)


def _is_image_unsupported(exc: BaseException) -> bool:
    s = str(exc).lower()
    return ("image input" in s or "support image" in s or "no endpoints" in s) and (
        "image" in s or "vision" in s
    )


def _has_images(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(p, dict) and p.get("type") == "image_url" for p in c):
            return True
    return False


def _strip_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace image parts with a short text note so a text-only model can
    still answer the turn instead of erroring on the image."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            parts, dropped = [], 0
            for p in c:
                if isinstance(p, dict) and p.get("type") == "image_url":
                    dropped += 1
                else:
                    parts.append(p)
            if dropped:
                parts.append({"type": "text", "text": f"[{dropped} image(s) attached — not shown; this model can't view images]"})
            out.append({**m, "content": parts if parts else "[image omitted]"})
        else:
            out.append(m)
    return out


class RouterProvider:
    """Wraps multiple providers, tries them in order, fails over on
    credit/rate-limit errors. Built providers are cached so the inner
    httpx client + TLS connection stay warm across turns — every call
    after the first skips the handshake."""

    name = "router"

    def __init__(self, config: SundayConfig) -> None:
        self.config = config
        self._chain = _credentialed_providers(config)
        self._provider_cache: dict[str, Provider] = {}
        log.info("router built", chain=self._chain)

    def _provider(self, name: str) -> Provider:
        cached = self._provider_cache.get(name)
        if cached is not None:
            return cached
        built = _build_provider(self.config, name)
        self._provider_cache[name] = built
        return built

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        on_delta: DeltaHandler | None = None,
        on_reasoning: DeltaHandler | None = None,
        purpose: str | None = None,
    ) -> CompletionResult:
        last_exc: BaseException | None = None
        for provider_name in self._chain:
            try:
                provider = self._provider(provider_name)
                try:
                    result = await provider.complete(
                        system_prompt=system_prompt,
                        messages=messages,
                        tools_schema=tools_schema,
                        on_delta=on_delta,
                        on_reasoning=on_reasoning,
                        purpose=purpose,
                    )
                except Exception as exc:  # noqa: BLE001
                    # The current model can't accept images. Rather than fail
                    # this turn — and every later turn that still has the image
                    # in context (fail forever) — strip images to text notes
                    # and answer anyway, once.
                    if _is_image_unsupported(exc) and _has_images(messages):
                        log.info("model lacks vision — retrying text-only", provider=provider_name)
                        result = await provider.complete(
                            system_prompt=system_prompt,
                            messages=_strip_images(messages),
                            tools_schema=tools_schema,
                            on_delta=on_delta,
                            on_reasoning=on_reasoning,
                            purpose=purpose,
                        )
                    else:
                        raise
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
