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
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from sunday.chat import Chat
from sunday.config import SundayConfig

ToolFn = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]

# The lean always-on toolset sent every turn. Everything else (MCP servers,
# the long tail of device/google/rewind tools) is deferred and pulled in on
# demand via find_tools — so the per-turn schema stays small no matter how
# many servers are connected. Names that don't exist in a given build are
# simply ignored.
CORE_TOOLS = frozenset({
    "find_tools", "sunday_config",
    "remember", "recall", "search_history",
    "list_skills", "load_skill", "save_skill", "search_skills", "install_skill",
    "device_screen_text", "device_screenshot", "device_run_command",
    "browser_read", "browser_click", "browser_type",
    "app_snapshot", "app_click", "app_type",
    "imessage_read_recent", "imessage_read_thread", "imessage_search", "imessage_send",
    "delegate", "delegate_parallel",
})


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

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def as_openai_schema(self, names: set[str] | None = None) -> list[dict[str, Any]]:
        tools = self._tools.values() if names is None else (t for t in self._tools.values() if t.name in names)
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    def search(self, query: str, limit: int = 15) -> list[Tool]:
        """Rank tools by a cheap keyword match against name + description."""
        terms = [w for w in re.split(r"\W+", (query or "").lower()) if len(w) > 1]
        scored = []
        for t in self._tools.values():
            hay = (t.name + " " + (t.description or "")).lower()
            score = sum(hay.count(term) for term in terms) + (3 if any(term in t.name.lower() for term in terms) else 0)
            if score or not terms:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:limit]]

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


async def _t_find_tools(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Discovery: surface tools beyond the always-on core and activate them
    for the rest of the conversation so they can be called."""
    query = (args.get("query") or "").strip()
    reg: ToolRegistry | None = ctx.extras.get("registry")
    active: set | None = ctx.extras.get("active_tools")
    if reg is None:
        return {"error": "tool registry unavailable"}
    matches = reg.search(query, limit=int(args.get("limit") or 15))
    # don't bother re-listing what's already always-on
    matches = [t for t in matches if t.name not in CORE_TOOLS]
    if active is not None:
        for t in matches:
            active.add(t.name)
    return {
        "activated": [t.name for t in matches],
        "tools": [{"name": t.name, "description": (t.description or "")[:160]} for t in matches],
        "note": "These are now available to call this conversation.",
    }


def _find_tools_tool() -> Tool:
    return Tool(
        name="find_tools",
        description=(
            "Find and activate tools beyond the always-on core. You have many "
            "more tools than are shown — email, calendar, screen history, "
            "browser control, any connected MCP server (e.g. AgentOS: tasks, "
            "wiki, CRM), etc. When a task needs something you don't see, call "
            "find_tools with a few keywords ('gmail', 'calendar event', "
            "'agentos tasks', 'screen history'); the matches become callable "
            "right after. Cheap — use it whenever you're unsure a tool exists."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords for what you need to do."},
            "limit": {"type": "integer"},
        }, "required": ["query"]},
        run=_t_find_tools,
    )


def default_registry(config: SundayConfig) -> ToolRegistry:
    """Build the canonical Sunday tool registry.

    Imports are local so a missing optional dep (e.g. the device subsystem)
    can't take the whole daemon down — we register what we can and log
    what we can't.
    """
    import structlog
    log = structlog.get_logger("sunday.tools")
    registry = ToolRegistry()
    registry.register(_find_tools_tool())

    for module_path in (
        "sunday.memory_tools",
        "sunday.history_tools",
        "sunday.introspect",
        "sunday.skills",
        "sunday.subagents.native",
        "sunday.subagents.hermes",
        "sunday.channels.messages_local",
        "sunday.channels.sendblue",
        "sunday.channels.vapi",
        "sunday.cloud.cloudflare",
        "sunday.devices.tools",
        "sunday.integrations.google",
        # Direct Gmail (IMAP/SMTP app password) registered AFTER the Nango
        # google module so it overrides gmail_search/read/send by name when
        # an app password is configured.
        "sunday.gmail_tools",
        "sunday.integrations.fireflies",
    ):
        try:
            mod = __import__(module_path, fromlist=["register"])
            mod.register(registry, config)
        except Exception as exc:  # noqa: BLE001 — best-effort wiring
            log.warning("tool module skipped", module=module_path, error=str(exc))

    return registry
