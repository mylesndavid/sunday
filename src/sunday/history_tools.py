"""search_history — let Sunday reach back into the verbatim conversation log.

Sunday's sent context is [rolling summary] + [recent tail] (see
sunday.compaction). The summary is lossy by design, so without a way to query
the raw log, anything that aged out and wasn't captured in the summary or the
memory DB would be unreachable — even though it's sitting right there in
SQLite. This tool closes that gap: keyword search over the actual messages,
with the ability to pull the surrounding exchange around a hit.

Distinct from `recall` (which searches the extracted-facts memory DB). recall
answers "what do I know about them"; search_history answers "what was actually
said, when".
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sunday.tools import Tool, ToolContext, ToolRegistry


def _ago(ts: float) -> str:
    secs = max(0, time.time() - (ts or 0))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{int(secs // n)}{unit} ago"
    return "just now"


def _stamp(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError):
        return "?"


def _fmt(m, max_chars: int) -> dict[str, Any]:
    who = "you" if m.role == "sunday" else ("them" if m.role == "user" else m.role)
    body = (m.content or "").strip()
    return {
        "id": m.id,
        "who": who,
        "when": f"{_stamp(m.created_at)} ({_ago(m.created_at)})",
        "text": body[:max_chars] + ("…" if len(body) > max_chars else ""),
    }


async def _t_search_history(args: dict[str, Any], ctx: ToolContext) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = max(1, min(int(args.get("limit") or 8), 25))
    hits = ctx.chat.search(query, limit=limit)
    if not hits:
        return {"matches": [], "note": f"Nothing in the conversation log matches '{query}'."}

    matches = [_fmt(m, 600) for m in hits]

    # If the model asked to expand a specific hit, attach the surrounding turns.
    around_id = args.get("around")
    if around_id is not None:
        try:
            ctx_rows = ctx.chat.around(int(around_id), span=int(args.get("span") or 3))
            return {
                "matches": matches,
                "context_around": [_fmt(m, 800) for m in ctx_rows],
            }
        except (TypeError, ValueError):
            pass
    return {"matches": matches, "note": "Pass `around` with a match id to see the surrounding exchange."}


def register(registry: ToolRegistry, config) -> None:
    registry.register(Tool(
        name="search_history",
        description=(
            "Search your entire verbatim conversation history with this person "
            "(everything ever said, not just what's in your recent context). "
            "Use it before saying you don't remember something — the words are "
            "almost always still here. Returns matching turns with timestamps "
            "and message ids; pass `around` with an id to read the surrounding "
            "exchange. This is the raw log; `recall` is for learned facts."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords to search for (all must appear)."},
            "limit": {"type": "integer", "description": "Max matches (default 8)."},
            "around": {"type": "integer", "description": "A match id to expand with surrounding turns."},
            "span": {"type": "integer", "description": "Turns on each side when using `around` (default 3)."},
        }, "required": ["query"]},
        run=_t_search_history,
    ))
