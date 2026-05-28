"""Fireflies tools, called through Nango's proxy.

Fireflies' API is GraphQL at https://api.fireflies.ai/graphql. Nango's
provider template sets that as the base URL for the proxy, so the endpoint
path here is the empty string — every request is a POST to the root with
a GraphQL query + variables in the body.

API reference: https://docs.fireflies.ai/graphql-api/query
"""

from __future__ import annotations

from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.integrations import nango
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.integrations.fireflies")

# Common GraphQL fragments — keep one source of truth so tool outputs stay
# consistent across list/get.
_TRANSCRIPT_LIST_FIELDS = "id title date duration meeting_attendees { displayName email }"
_TRANSCRIPT_FULL_FIELDS = (
    "id title date duration host_email organizer_email "
    "meeting_attendees { displayName email } "
    "summary { keywords action_items overview shorthand_bullet } "
    "sentences { index speaker_name text start_time }"
)


async def _gql(query: str, variables: dict[str, Any] | None = None) -> Any:
    """Send a GraphQL query through Nango's Fireflies proxy. Returns the
    `data` block on success, the {error: …} envelope on failure."""
    body = {"query": query, "variables": variables or {}}
    res = await nango.proxy("POST", "graphql", "fireflies", json=body)
    if "error" in res:
        return res
    if res.get("errors"):
        # GraphQL-level errors land in `errors`, not a top-level `error`.
        return {"error": "; ".join(e.get("message", "graphql error") for e in res["errors"])}
    return res.get("data", res)


async def _list_transcripts(args: dict[str, Any], ctx: ToolContext) -> Any:
    limit = max(1, min(int(args.get("limit") or 10), 50))
    from_date = (args.get("from_date") or "").strip()
    to_date   = (args.get("to_date") or "").strip()
    # Fireflies' transcripts(...) query accepts limit + optional date filters.
    filters = ["limit: $limit"]
    var_defs = ["$limit: Int"]
    variables: dict[str, Any] = {"limit": limit}
    if from_date:
        filters.append("fromDate: $fromDate")
        var_defs.append("$fromDate: DateTime")
        variables["fromDate"] = from_date
    if to_date:
        filters.append("toDate: $toDate")
        var_defs.append("$toDate: DateTime")
        variables["toDate"] = to_date
    query = (
        f"query Transcripts({', '.join(var_defs)}) {{\n"
        f"  transcripts({', '.join(filters)}) {{\n"
        f"    {_TRANSCRIPT_LIST_FIELDS}\n"
        f"  }}\n"
        f"}}"
    )
    data = await _gql(query, variables)
    if "error" in data:
        return data
    return {"transcripts": data.get("transcripts", []), "count": len(data.get("transcripts", []))}


async def _get_transcript(args: dict[str, Any], ctx: ToolContext) -> Any:
    tid = (args.get("transcript_id") or "").strip()
    if not tid:
        return {"error": "'transcript_id' is required (from fireflies_list_transcripts)"}
    query = (
        f"query Transcript($id: String!) {{\n"
        f"  transcript(id: $id) {{ {_TRANSCRIPT_FULL_FIELDS} }}\n"
        f"}}"
    )
    data = await _gql(query, {"id": tid})
    if "error" in data:
        return data
    return data.get("transcript") or {"error": "transcript not found"}


async def _search_transcripts(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Fireflies has no first-class full-text search endpoint; we list recent
    transcripts, fetch their summaries, and rank by keyword overlap locally.
    Lossy but useful — Fireflies' own UI does the same client-side.
    """
    query_text = (args.get("query") or "").strip().lower()
    if not query_text:
        return {"error": "'query' is required"}
    limit = max(1, min(int(args.get("limit") or 20), 50))
    listed = await _list_transcripts({"limit": limit}, ctx)
    if "error" in listed:
        return listed
    terms = [t for t in query_text.split() if len(t) > 2]
    scored: list[tuple[float, dict]] = []
    for t in listed.get("transcripts", []):
        full = await _get_transcript({"transcript_id": t["id"]}, ctx)
        if "error" in full:
            continue
        # Cheap rank: combine title + overview + bullets.
        s = (full.get("summary") or {})
        haystack = " ".join([
            full.get("title", ""),
            s.get("overview", "") or "",
            " ".join(s.get("shorthand_bullet") or []),
            " ".join(s.get("keywords") or []),
        ]).lower()
        score = sum(1.0 for term in terms if term in haystack)
        if score > 0:
            scored.append((score, {
                "id": full["id"],
                "title": full.get("title"),
                "date": full.get("date"),
                "overview": (s.get("overview") or "")[:600],
                "score": score,
            }))
    scored.sort(key=lambda kv: -kv[0])
    return {"matches": [m for _, m in scored[:10]], "checked": len(listed.get("transcripts", []))}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="fireflies_list_transcripts",
        description="List the user's recent Fireflies meeting transcripts. Returns id, title, date, duration, and attendees. Use from_date/to_date (ISO 8601) to bound the window. Then fireflies_get_transcript(id) for full content + summary.",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "Max transcripts (default 10, max 50)."},
            "from_date": {"type": "string", "description": "ISO 8601 lower bound."},
            "to_date": {"type": "string", "description": "ISO 8601 upper bound."},
        }},
        run=_list_transcripts,
    ))
    registry.register(Tool(
        name="fireflies_get_transcript",
        description="Read a full Fireflies meeting transcript by id (from fireflies_list_transcripts). Returns the AI summary (overview, action items, keywords) plus the sentence-by-sentence transcript with speaker names and timestamps.",
        parameters={"type": "object", "properties": {
            "transcript_id": {"type": "string"},
        }, "required": ["transcript_id"]},
        run=_get_transcript,
    ))
    registry.register(Tool(
        name="fireflies_search_transcripts",
        description="Search across the user's recent Fireflies meetings. Lists recent transcripts, fetches summaries, ranks by keyword overlap with the query. Use this when the user references a meeting topic, person, or decision.",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for (topic, person, decision)."},
            "limit": {"type": "integer", "description": "How many recent transcripts to scan (default 20)."},
        }, "required": ["query"]},
        run=_search_transcripts,
    ))
