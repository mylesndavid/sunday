"""sunday_config — lets Sunday read its own runtime config.

The agent asked for this: when it hits a limit it couldn't even tell the user
what the limit was. This tool surfaces the model, the per-turn tool ceiling,
the version, how memory + context work, the tool count, and connected
integrations — so Sunday can self-diagnose instead of guessing.
"""

from __future__ import annotations

from typing import Any

from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry


async def _t_sunday_config(args: dict[str, Any], ctx: ToolContext) -> Any:
    from sunday import __version__
    from sunday.brain import MAX_TOOL_ITERATIONS
    from sunday.compaction import TAIL_MAX_MESSAGES, TAIL_TOKEN_BUDGET
    from sunday.tools import CORE_TOOLS

    cfg: SundayConfig = ctx.config
    reg = ctx.extras.get("registry")
    mem = ctx.extras.get("memory")

    out: dict[str, Any] = {
        "version": __version__,
        "model": f"{cfg.model.provider}/{cfg.model.name}",
        "max_tool_calls_per_turn": MAX_TOOL_ITERATIONS,
        "context": {
            "shape": "rolling summary + token-budgeted recent tail + always-injected memory core",
            "tail_token_budget": TAIL_TOKEN_BUDGET,
            "tail_max_messages": TAIL_MAX_MESSAGES,
            "note": "Older turns are folded into a rolling summary; use search_history to read the verbatim log.",
        },
        "tools": {
            "core_always_on": sorted(CORE_TOOLS),
            "total_available": len(reg.names()) if reg else None,
            "note": "Beyond the core set, call find_tools to surface + activate the rest.",
        },
    }
    if mem is not None:
        out["memory"] = {
            "available": getattr(mem, "available", False),
            "facts_stored": mem.count() if getattr(mem, "available", False) else 0,
            "mode": "local FTS5 keyword search + always-injected core (no embeddings)",
        }
    # connected MCP servers, best-effort
    try:
        from sunday import mcp
        out["mcp"] = mcp.STATUS
    except Exception:  # noqa: BLE001
        pass
    return out


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="sunday_config",
        description=(
            "Read your own runtime configuration — your model, your per-turn "
            "tool-call ceiling, your version, how your memory and context work, "
            "how many tools you have, and which integrations are connected. Use "
            "it to answer questions about yourself or to self-diagnose when you "
            "hit a limit (e.g. so you can tell the user what the limit actually is)."
        ),
        parameters={"type": "object", "properties": {}},
        run=_t_sunday_config,
    ))
