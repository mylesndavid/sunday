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
