"""Tools that let the brain interact with Sunday's memory explicitly.

The automatic recall + extract happens in brain.py / daemon.py — these
tools are for moments when the model wants to be deliberate, e.g. "the
user just told me their birthday, store that" or "what do I know about
their job?"
"""

from __future__ import annotations

from typing import Any

from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry


_NOT_AVAILABLE = {
    "error": "Memory subsystem is not initialized (could not open the local memories database)."
}


def _mem(ctx: ToolContext):
    return ctx.extras.get("memory")


async def _t_remember(args: dict[str, Any], ctx: ToolContext) -> Any:
    content = (args.get("content") or "").strip()
    if not content:
        return {"error": "'content' is required"}
    mem = _mem(ctx)
    if mem is None or not getattr(mem, "available", False):
        return _NOT_AVAILABLE
    mid = await mem.store(content, source="tool")
    if mid is None:
        return {"error": "store failed (see daemon log)"}
    return {"ok": True, "id": mid}


async def _t_recall(args: dict[str, Any], ctx: ToolContext) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "'query' is required"}
    mem = _mem(ctx)
    if mem is None or not getattr(mem, "available", False):
        return _NOT_AVAILABLE
    top_k = int(args.get("top_k") or 8)
    # Hybrid FTS+vector recall — all local; degrades to plain FTS when no
    # local embedder is running. Zero network either way.
    hits = await mem.search_hybrid(query, limit=top_k)
    return {
        "results": [
            {"id": h.id, "content": h.content, "source": h.source}
            for h in hits
        ],
    }


async def _t_forget(args: dict[str, Any], ctx: ToolContext) -> Any:
    mid = args.get("id")
    if mid is None:
        return {"error": "'id' is required"}
    mem = _mem(ctx)
    if mem is None or not getattr(mem, "available", False):
        return _NOT_AVAILABLE
    ok = mem.forget(int(mid))
    return {"ok": ok}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="remember",
        description=(
            "Store a durable fact about the user in Sunday's long-term memory. "
            "Use for things she should reliably know across future conversations "
            "(preferences, relationships, decisions, locations, habits). NOT for "
            "transient current-task state."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "A complete self-contained sentence about the user.",
                },
            },
            "required": ["content"],
        },
        run=_t_remember,
    ))
    registry.register(Tool(
        name="recall",
        description=(
            "Keyword-search Sunday's long-term memory for facts. Note: the full "
            "set of known facts is ALREADY in your context every turn, so you "
            "rarely need this — use it only when memory has grown large and you "
            "want to pull a specific fact that may not be in the always-on set."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "top_k": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
        run=_t_recall,
    ))
    registry.register(Tool(
        name="forget",
        description="Permanently delete a stored memory by its id. Use sparingly — irreversible.",
        parameters={
            "type": "object",
            "properties": {"id": {"type": "integer", "description": "Memory id from recall()."}},
            "required": ["id"],
        },
        run=_t_forget,
    ))
