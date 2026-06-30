"""Relay state store — the small, durable record of the relay toggle.

The relay needs three things to survive daemon restarts: whether it's
`enabled`, which `url` it dials (Sunday-hosted default or a BYO relay), and
the minted `agent_id` (the public, unguessable coarse-auth that the public
URLs embed — so it MUST be stable across restarts, or every provider's URL
breaks on reboot).

There is no general config-to-disk path in this project yet, so this is a
dedicated tiny JSON store, mirroring `connectors.py`'s load/save style:
  ~/.sunday/relay.json  →  {"enabled": bool, "url": str, "agent_id": str}

RELAY_TOKEN deliberately does NOT live here — it's a credential (the socket
secret), stored in credentials.env via get_credential/set_credential, same as
the Sendblue keys. This file holds only non-secret, UI-readable state.

Tolerant of a missing or corrupt file: every read falls back to defaults so a
hand-edited or truncated relay.json can never wedge the daemon.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sunday.paths import sunday_home

# The Sunday-hosted relay default. Kept in sync with RelayConfig.url so the
# overlay never silently disagrees with config's notion of "the default".
DEFAULT_URL = "wss://sunday-relay.fly.dev"

_DEFAULTS: dict[str, Any] = {"enabled": False, "url": DEFAULT_URL, "agent_id": ""}


def _path() -> Path:
    return sunday_home() / "relay.json"


def load() -> dict[str, Any]:
    """The persisted relay state, defaults overlaid for any missing/corrupt
    fields. Always returns a complete {enabled, url, agent_id} dict."""
    out = dict(_DEFAULTS)
    p = _path()
    if not p.exists():
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    if not isinstance(data, dict):
        return out
    if isinstance(data.get("enabled"), bool):
        out["enabled"] = data["enabled"]
    if isinstance(data.get("url"), str) and data["url"].strip():
        out["url"] = data["url"].strip()
    if isinstance(data.get("agent_id"), str):
        out["agent_id"] = data["agent_id"]
    return out


def save(state: dict[str, Any]) -> None:
    """Persist the full state dict. Merges onto current state so a partial
    dict only updates the keys it carries."""
    merged = load()
    for key in ("enabled", "url", "agent_id"):
        if key in state:
            merged[key] = state[key]
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, indent=2), encoding="utf-8")


# ─── small typed helpers ───────────────────────────────────────────────────

def get_agent_id() -> str:
    return str(load().get("agent_id") or "")


def set_agent_id(agent_id: str) -> None:
    save({"agent_id": agent_id})


def is_enabled() -> bool:
    return bool(load().get("enabled"))


def set_enabled(enabled: bool) -> None:
    save({"enabled": bool(enabled)})


def get_url() -> str:
    return str(load().get("url") or DEFAULT_URL)


def set_url(url: str) -> None:
    save({"url": url})
