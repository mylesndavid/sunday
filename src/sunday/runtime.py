"""Sunday's runtime abstraction.

The brain shouldn't care whether the model is being driven through Hermes
(subprocess CLI, dcharness pattern) or via a direct OpenAI-compatible call.
Both implementations return the same CompletionResult shape, and brain.py
runs the same tool-call loop regardless.

This is the single place to swap LLM plumbing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sunday.config import SundayConfig


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


class Runtime(Protocol):
    """Abstract completion backend."""

    name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
    ) -> CompletionResult: ...


def hermes_binary_path(config: SundayConfig) -> str | None:
    """Resolve the Hermes binary if it's available on this machine."""
    name = config.hermes.binary
    found = shutil.which(name)
    if found:
        return found
    candidate = Path("~/.hermes/bin").expanduser() / name
    if candidate.exists():
        return str(candidate)
    return None


def build_runtime(config: SundayConfig) -> Runtime:
    """Pick the best runtime based on config + what's installed.

    Priority: explicit config.hermes.runtime_choice → Hermes if available →
    OpenAI direct.
    """
    choice = config.hermes.runtime_choice

    if choice == "openai":
        from sunday.runtime_openai import OpenAIRuntime
        return OpenAIRuntime(config)

    if choice == "hermes":
        path = hermes_binary_path(config)
        if not path:
            raise RuntimeError(
                f"runtime_choice='hermes' but '{config.hermes.binary}' not found on PATH "
                f"or in ~/.hermes/bin/. Install Hermes or set runtime_choice='openai'."
            )
        from sunday.runtime_hermes import HermesRuntime
        return HermesRuntime(config, binary_path=path)

    # auto
    path = hermes_binary_path(config)
    if path:
        from sunday.runtime_hermes import HermesRuntime
        return HermesRuntime(config, binary_path=path)
    from sunday.runtime_openai import OpenAIRuntime
    return OpenAIRuntime(config)
