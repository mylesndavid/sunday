"""Connector toggles — which connected integrations are 'pinned' as
always-on tools vs. only reachable via find_tools.

The flow:
  1. The user connects a service via Settings → Connections (Nango).
  2. By default the new connection is OFF — its tools are NOT in the every-turn
     schema. The agent can still find them via find_tools when relevant.
  3. The user toggles the connector ON. Its tools join the always-on set —
     visible to the brain on every turn, no discovery hop needed.

This mirrors the Claude/Manus connector pattern: long-tail providers are
catalog-browsable, top-N are pinned. Bounds the per-turn schema budget no
matter how many services are connected.

Persistence: ~/.sunday/connectors.json, schema {"enabled": ["provider-key", …]}.
"""

from __future__ import annotations

import json
from pathlib import Path

from sunday.paths import sunday_home

# Maps Nango provider key → tool-name prefixes belonging to that connector.
# When `provider` is toggled on, every tool whose name starts with one of
# these prefixes joins the always-on schema set.
#
# Add entries here when a new per-provider tool module ships. The prefix is
# the user-visible verb stem (e.g. `gmail_send`, `gmail_search` → `gmail_`).
PROVIDER_TOOL_PREFIXES: dict[str, tuple[str, ...]] = {
    "google-mail":     ("gmail_",),
    "google-calendar": ("calendar_",),
    "fireflies":       ("fireflies_",),
}


def _path() -> Path:
    return sunday_home() / "connectors.json"


def load() -> set[str]:
    p = _path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("enabled") or [])
    except (json.JSONDecodeError, OSError):
        return set()


def save(enabled: set[str]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"enabled": sorted(enabled)}, indent=2), encoding="utf-8")


def toggle(provider: str, on: bool) -> set[str]:
    """Flip one connector's pinned state. Returns the new full set."""
    enabled = load()
    if on:
        enabled.add(provider)
    else:
        enabled.discard(provider)
    save(enabled)
    return enabled


def tool_names_for(provider: str, registry) -> set[str]:
    """Tool names owned by this provider, looked up from the live registry.

    Includes both hand-curated tools (matched by name prefix from
    PROVIDER_TOOL_PREFIXES) AND the auto-generated proxy tool
    `use_<provider>_api` that the daemon registers for every connection.
    A connector is never "empty" anymore — the proxy is the floor.
    """
    out: set[str] = set()
    for prefix in PROVIDER_TOOL_PREFIXES.get(provider, ()):
        out |= {t.name for t in registry.list_tools() if t.name.startswith(prefix)}
    # Always include the generic proxy tool if it's been registered.
    from sunday.integrations.proxy_tools import tool_name_for as _proxy_name
    proxy = _proxy_name(provider)
    if registry.get(proxy):
        out.add(proxy)
    return out


def active_tool_names(registry) -> set[str]:
    """The union of tools that should be in every turn's schema because
    their connector is toggled on. Cheap — single disk read + a registry
    scan, well under a millisecond at our scale."""
    enabled = load()
    out: set[str] = set()
    for provider in enabled:
        out |= tool_names_for(provider, registry)
    return out


def providers_with_tools() -> list[str]:
    """The list of providers we know how to render tools for (the keys of
    PROVIDER_TOOL_PREFIXES). Used by the UI to decide which connected
    providers can be toggled — Nango may have other connections but if we
    haven't shipped tools for them yet, no toggle to show."""
    return sorted(PROVIDER_TOOL_PREFIXES.keys())
