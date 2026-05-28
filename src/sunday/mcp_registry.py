"""Model Context Protocol Registry client + installer.

The Registry (registry.modelcontextprotocol.io) is the ecosystem's catalog of
MCP servers — thousands of pre-built tool collections, maintained by the
people who actually build the integrations. This is the natural-tools layer
that obviates writing per-provider modules.

Two operations:

  list(q, category)  — query the registry, return lean cards for the UI to
                       render in Settings → Connections.

  install(server)    — add a remote MCP server to Sunday's mcp.json so its
                       tools land on the next MCP reconnect. Today we support
                       hosted/remote servers (a single URL) cleanly; local
                       'packages' (npm/uvx-launched) are recognized but
                       deferred — running arbitrary npm processes in the
                       daemon's environment is a separate trust + sandboxing
                       problem and isn't the 80% case.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
import structlog

log = structlog.get_logger("sunday.mcp_registry")

REGISTRY_BASE = "https://registry.modelcontextprotocol.io"

# Cache the full catalog — registry API is rate-limited and we'd otherwise
# hit it on every keystroke in the search box. Cleared by force=True.
_CACHE: dict[str, Any] = {"items": None, "ts": 0.0}


async def _fetch(path: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(f"{REGISTRY_BASE}{path}", params=params or {})
    res.raise_for_status()
    return res.json()


def _flatten(entry: dict) -> dict:
    """Pull the lean record we render in the UI out of the registry's wrapper.

    The registry wraps every server in `{server: {...}, _meta: {...}}`; the
    `server` block carries the actual data. We collapse it and add a couple
    of derived fields (kind: remote|local) so the UI doesn't have to know
    about the registry's exact shape.
    """
    srv = entry.get("server") or entry  # tolerate already-unwrapped
    remotes = srv.get("remotes") or []
    packages = srv.get("packages") or []
    kind = "remote" if remotes else ("local" if packages else "unknown")
    return {
        "name":        srv.get("name", ""),
        "title":       srv.get("title") or _derive_title(srv.get("name", "")),
        "description": (srv.get("description") or "").strip(),
        "version":     srv.get("version", ""),
        "kind":        kind,
        "remotes":     remotes,
        "packages":    packages,
    }


def _derive_title(name: str) -> str:
    """If a server didn't set a title, derive one from its dot-separated id:
    'ai.smithery/smithery-notion' -> 'Smithery Notion'."""
    last = name.split("/")[-1] if "/" in name else name.split(".")[-1]
    return re.sub(r"[-_]+", " ", last).strip().title()


async def list_servers(q: str = "", limit: int = 30, force: bool = False) -> list[dict]:
    """Browse the registry. Without `q`, returns a curated 'popular' slice
    (the registry's top results). With `q`, server-side searches by keyword.
    """
    import time
    q = (q or "").strip()
    # Server-side search is free, no need to cache results-by-query — but a
    # 5-minute keystroke window is fine.
    cache_key = f"q::{q.lower()}"
    cached = _CACHE.get(cache_key)
    if cached and not force and (time.time() - cached["ts"] < 300):
        return cached["items"][:limit]
    try:
        params: dict[str, Any] = {"limit": min(limit, 50)}
        if q:
            params["search"] = q
        body = await _fetch("/v0/servers", params=params)
    except Exception as exc:  # noqa: BLE001
        log.warning("registry browse failed", error=str(exc), q=q)
        return []
    items = [_flatten(s) for s in (body.get("servers") or [])]
    _CACHE[cache_key] = {"items": items, "ts": time.time()}
    return items[:limit]


async def get_server(name: str) -> dict | None:
    """Fetch one server's full record by its reverse-DNS name."""
    try:
        # The registry encodes the name in the path; use a search fallback
        # because individual-server endpoints have varying availability.
        body = await _fetch("/v0/servers", params={"search": name, "limit": 5})
        for entry in body.get("servers") or []:
            srv = entry.get("server") or entry
            if srv.get("name") == name:
                return _flatten(entry)
    except Exception as exc:  # noqa: BLE001
        log.warning("registry fetch one failed", error=str(exc), name=name)
    return None


# ─── install: write the server into Sunday's mcp.json ────────────────────

def _slugify(name: str) -> str:
    """Reverse-DNS names aren't valid JSON keys in user-facing config.
    'ai.smithery/smithery-notion' -> 'smithery-notion'. Idempotent."""
    last = name.split("/")[-1] if "/" in name else name.split(".")[-1]
    return re.sub(r"[^a-z0-9_-]+", "-", last.lower()).strip("-") or "server"


_PLACEHOLDER = re.compile(r"\{([a-zA-Z0-9_]+)\}")


def required_fields(server: dict) -> list[dict[str, Any]]:
    """Return the schema for any user-input fields the server's first remote
    needs — typically API keys baked into Authorization headers. Each field:
    {key, header, description, secret}.

    Derived from each header's `value` template, e.g. `Bearer {smithery_api_key}`
    yields one field with key=`smithery_api_key`.
    """
    remotes = server.get("remotes") or []
    if not remotes:
        return []
    fields: dict[str, dict[str, Any]] = {}
    for h in (remotes[0].get("headers") or []):
        value = h.get("value") or ""
        for placeholder in _PLACEHOLDER.findall(value):
            # First mention wins; keep the originating header's description
            # since that's where the user reads what to paste.
            fields.setdefault(placeholder, {
                "key":         placeholder,
                "header":      h.get("name", ""),
                "description": h.get("description") or "",
                "secret":      bool(h.get("isSecret")) or "secret" in placeholder.lower() or "key" in placeholder.lower() or "token" in placeholder.lower(),
                "required":    bool(h.get("isRequired", True)),
            })
    return list(fields.values())


def install_remote(server: dict, mcp_config: dict, secrets: dict | None = None) -> dict[str, Any]:
    """Mutates `mcp_config` (the parsed mcp.json) to add this server's
    first remote URL + resolved headers under a friendly key. Returns the
    updated config plus the chosen slug — the daemon then re-saves + reconnects.

    `secrets` maps the placeholder names from `required_fields()` to their
    user-supplied values. If any required field is missing, returns
    {missing_fields: [...]} rather than silently writing a broken config.
    """
    remotes = server.get("remotes") or []
    if not remotes:
        return {"error": "no remote URL on this server — local install isn't supported yet"}
    first = remotes[0]
    url = first.get("url")
    if not url:
        return {"error": "remote entry has no url"}

    secrets = secrets or {}
    fields = required_fields(server)
    missing = [f["key"] for f in fields if f["required"] and not secrets.get(f["key"])]
    if missing:
        return {"missing_fields": missing, "fields": fields}

    # Resolve {placeholder} substitutions in each header's value.
    resolved_headers: dict[str, str] = {}
    for h in (first.get("headers") or []):
        value = h.get("value") or ""
        for placeholder, val in secrets.items():
            value = value.replace("{" + placeholder + "}", val)
        if value:
            resolved_headers[h.get("name", "")] = value

    slug = _slugify(server.get("name", ""))
    entry: dict[str, Any] = {"url": url}
    if resolved_headers:
        entry["headers"] = resolved_headers
    mcp_config.setdefault("mcpServers", {})[slug] = entry
    return {"slug": slug, "config": mcp_config}


def uninstall(slug: str, mcp_config: dict) -> dict[str, Any]:
    """Remove a previously-installed server by its slug."""
    servers = mcp_config.get("mcpServers") or {}
    if slug not in servers:
        return {"error": f"no server installed under '{slug}'"}
    servers.pop(slug, None)
    mcp_config["mcpServers"] = servers
    return {"slug": slug, "config": mcp_config}
