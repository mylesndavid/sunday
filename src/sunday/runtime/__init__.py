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
    "repair_tool_call_arguments",
]


def build_runtime(config: SundayConfig) -> Provider:
    """Pick a provider based on config. For now: OpenAI-compatible only.

    Multi-provider auto-fallback (Hermes's auxiliary_client pattern) lands
    in a follow-up — anthropic.py + a router that tries the configured
    provider first and falls back on 402 / rate-limit.
    """
    from sunday.runtime.providers.openai_compat import OpenAICompatProvider
    return OpenAICompatProvider(config)
