"""Tool registry and execution context.

Each tool is a name + description + JSON-schema parameters + async run
function. The brain registers tools at startup and offers them to the
model on every chat-completion call.

The default registry composes Sunday's first-party tools: Hermes sub-agent
delegation, iMessage send, VAPI call, Cloudflare browser/sandbox, remote
device control. Each module exposes a `register(registry, config)` hook
so wiring is local to the file that defines the tool.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sunday.chat import Chat
from sunday.config import SundayConfig

ToolFn = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema (OpenAI tool calling format)
    run: ToolFn


@dataclass
class ToolContext:
    """Runtime handles a tool may need.

    Tools should depend on this struct rather than reaching for module-level
    state, so they're testable in isolation.
    """
    chat: Chat
    config: SundayConfig
    modality: str
    extras: dict[str, Any] = field(default_factory=dict)

    async def broadcast(self, event: dict[str, Any]) -> None:
        """Push an event to any WS clients listening (Electron app, live-view viewers).

        Set up by the daemon when it builds the ToolContext; a no-op when the
        tool is invoked outside daemon scope (tests, scripts).
        """
        callback = self.extras.get("broadcast")
        if callback is not None:
            await callback(event)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def as_openai_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, arguments_json: str, ctx: ToolContext) -> Any:
        tool = self.get(name)
        if tool is None:
            return {"error": f"unknown tool: {name}"}
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as exc:
            return {"error": f"invalid JSON arguments: {exc}"}
        if not isinstance(args, dict):
            return {"error": "arguments must be a JSON object"}
        try:
            return await tool.run(args, ctx)
        except Exception as exc:  # noqa: BLE001 — tool-execution boundary
            return {"error": f"{type(exc).__name__}: {exc}"}


def default_registry(config: SundayConfig) -> ToolRegistry:
    """Build the canonical Sunday tool registry.

    Imports are local so a missing optional dep (e.g. the device subsystem)
    can't take the whole daemon down — we register what we can and log
    what we can't.
    """
    import structlog
    log = structlog.get_logger("sunday.tools")
    registry = ToolRegistry()

    for module_path in (
        "sunday.memory_tools",
        "sunday.skills",
        "sunday.subagents.native",
        "sunday.subagents.hermes",
        "sunday.channels.messages_local",
        "sunday.channels.sendblue",
        "sunday.channels.vapi",
        "sunday.cloud.cloudflare",
        "sunday.devices.tools",
    ):
        try:
            mod = __import__(module_path, fromlist=["register"])
            mod.register(registry, config)
        except Exception as exc:  # noqa: BLE001 — best-effort wiring
            log.warning("tool module skipped", module=module_path, error=str(exc))

    return registry
