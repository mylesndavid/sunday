"""Public data types for sunday.runtime.

These are the contract between providers and the agent loop in core.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

DeltaHandler = Callable[[str], Awaitable[None]]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON-encoded


@dataclass
class CompletionResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    """One backend (OpenRouter, OpenAI direct, Anthropic, …)."""

    name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
        on_delta: DeltaHandler | None = None,
    ) -> CompletionResult: ...


# Legacy alias — earlier code imported `Runtime` rather than `Provider`.
Runtime = Provider
