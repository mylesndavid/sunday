"""sunday.runtime — agent runtime.

Public surface kept stable so existing brain.py keeps working:
    from sunday.runtime import build_runtime, CompletionResult, ToolCall, DeltaHandler, Runtime

See NOTICE.md for credits — the iteration budget + tool-argument repair
are forked from Hermes (Nous Research, MIT). The agent loop and provider
adapters are Sunday's own, Hermes-shaped.
"""

from __future__ import annotations

from sunday.config import SundayConfig
from sunday.runtime.iteration_budget import IterationBudget
from sunday.runtime.sanitize import (
    repair_message_sequence,
    sanitize_messages_surrogates,
)
from sunday.runtime.tool_args import repair_tool_call_arguments
from sunday.runtime.types import (
    CompletionResult,
    DeltaHandler,
    Provider,
    Runtime,        # alias for Provider, kept for backward compat
    ToolCall,
)

__all__ = [
    "CompletionResult",
    "DeltaHandler",
    "IterationBudget",
    "Provider",
    "Runtime",
    "ToolCall",
    "build_runtime",
    "repair_message_sequence",
    "repair_tool_call_arguments",
    "sanitize_messages_surrogates",
]


def build_runtime(config: SundayConfig) -> Provider:
    """Build the multi-provider router. Primary provider per config; auto-
    fallback to any other provider with credentials available on this host
    when the primary hits 402 / credit-exhausted / rate-limit errors.
    """
    from sunday.runtime.router import RouterProvider
    return RouterProvider(config)


# Cheap/fast model per provider for background "utility" work (fact extraction,
# graph building). These are distillation tasks — they don't need the big chat
# model or reasoning. Ollama isn't listed, so it stays on the user's local model
# (already small + free), just with reasoning off.
_UTILITY_MODEL = {
    "openrouter": "openai/gpt-4o-mini",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "deepseek-direct": "deepseek-chat",
    "codex": "gpt-5.2",
}


def build_utility_runtime(config: SundayConfig) -> Provider:
    """Like build_runtime, but on a cheap, fast model with reasoning OFF — for
    background extraction/graph work, never the chat brain. Falls back to the
    user's model (e.g. local Ollama) when there's no cheaper sibling."""
    import copy
    from dataclasses import replace
    cloned = copy.copy(config)
    name = _UTILITY_MODEL.get(config.model.provider, config.model.name)
    cloned.model = replace(config.model, name=name, reasoning=False)
    from sunday.runtime.router import RouterProvider
    return RouterProvider(cloned)
