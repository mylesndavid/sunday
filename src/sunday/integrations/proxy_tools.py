"""Auto-generated per-provider proxy tools.

For every Nango connection we don't have a hand-written tool module for,
register exactly one tool of the form:

    use_<provider>_api(method, path, body?, params?)

Named per provider so the model picks the right one for the right service —
`use_notion_api` is unambiguous about what it operates on. The body of every
call routes through nango.proxy(), which injects the user's OAuth token and
the provider's base URL.

When a hand-curated module (e.g. fireflies.py) ships for a provider, its
named tools (`fireflies_list_transcripts`, …) take precedence — the agent
prefers the more specific tool. The generic proxy stays as a fallback for
endpoints the curated module hasn't wrapped yet.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

from sunday.integrations import nango
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.integrations.proxy_tools")


def tool_name_for(provider_key: str) -> str:
    """Provider keys can have hyphens (google-mail); tool names can't —
    OpenAI's function-calling schema rejects them. Normalize to underscore."""
    safe = re.sub(r"[^a-z0-9_]+", "_", provider_key.lower())
    return f"use_{safe}_api"


def _build_tool(provider_key: str, display_name: str, base_url: str, docs_url: str) -> Tool:
    pretty = display_name or provider_key

    async def _run(args: dict[str, Any], ctx: ToolContext) -> Any:
        method = (args.get("method") or "GET").upper()
        path   = (args.get("path") or "").strip().lstrip("/")
        body   = args.get("body")
        params = args.get("params")
        return await nango.proxy(method, path, provider_key, params=params, json=body)

    description = (
        f"Call the {pretty} API. Use this for any {pretty} operation. "
        f"`path` is relative to {base_url or 'the API base URL'}; "
        f"`method` is the HTTP verb (GET/POST/PATCH/DELETE); "
        f"`body` is a JSON object for POST/PATCH; "
        f"`params` are query-string parameters."
        + (f" Docs: {docs_url}." if docs_url else "")
    )

    return Tool(
        name=tool_name_for(provider_key),
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PATCH", "PUT", "DELETE"]},
                "path":   {"type": "string", "description": "Endpoint path, relative to the API base URL."},
                "body":   {"type": "object", "description": "JSON body for POST/PATCH/PUT."},
                "params": {"type": "object", "description": "Query-string parameters."},
            },
            "required": ["method", "path"],
        },
        run=_run,
    )


async def register_proxies_for_connected(registry: ToolRegistry, *, skip: set[str] | None = None) -> list[str]:
    """Enumerate live Nango connections; register a proxy tool for each
    provider not in `skip` (typically: providers with hand-written modules).
    Returns the list of provider keys we registered tools for.
    """
    if not nango.configured():
        return []
    skip = skip or set()
    try:
        conns = await nango.list_connections()
    except Exception as exc:  # noqa: BLE001
        log.warning("nango list_connections failed", error=str(exc))
        return []

    # Distinct provider keys present in the user's live connections.
    keys = sorted({c.get("provider_config_key") for c in conns if c.get("provider_config_key")})
    if not keys:
        return []

    # Pull the catalog once so we can fill in display_name + base_url + docs.
    try:
        catalog = {p.get("name"): p for p in await nango.list_providers() if p.get("name")}
    except Exception:  # noqa: BLE001
        catalog = {}

    registered: list[str] = []
    for key in keys:
        if key in skip:
            continue
        if registry.get(tool_name_for(key)):
            continue   # idempotent — already registered this turn
        entry = catalog.get(key, {})
        display = entry.get("display_name") or key
        base_url = (entry.get("proxy") or {}).get("base_url") or ""
        docs_url = entry.get("docs") or ""
        registry.register(_build_tool(key, display, base_url, docs_url))
        registered.append(key)

    if registered:
        log.info("proxy tools registered", providers=registered)
    return registered
