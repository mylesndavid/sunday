"""Nango client — self-hosted OAuth + API proxy.

Sunday stores no provider OAuth secrets. Nango owns the OAuth apps and
refreshes tokens; Sunday calls provider APIs through Nango's `/proxy`
endpoint, passing only the integration id (provider_config_key) and the
connection id. The proxy + connection-listing endpoints are stable across
Nango versions; the connect handshake is version-sensitive and is built
best-effort + configurable.

Config (via the credential store / env):
  NANGO_HOST          base URL of your Nango instance (e.g. http://localhost:3003)
  NANGO_SECRET_KEY    environment secret key
  NANGO_CONNECTION_ID connection id Sunday uses (default 'sunday')
  NANGO_PUBLIC_KEY    (optional) for the legacy oauth-connect URL flow
Per-provider integration ids default sensibly and can be overridden:
  NANGO_KEY_GMAIL     (default 'google-mail')
  NANGO_KEY_CALENDAR  (default 'google-calendar')
  NANGO_KEY_SLACK     (default 'slack')
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from sunday.credentials import get_credential

log = structlog.get_logger("sunday.integrations.nango")

# friendly provider -> default Nango integration id (provider_config_key)
PROVIDERS = {
    "gmail":    {"label": "Gmail",          "key_env": "NANGO_KEY_GMAIL",    "default_key": "google-mail"},
    "calendar": {"label": "Google Calendar","key_env": "NANGO_KEY_CALENDAR", "default_key": "google-calendar"},
    "slack":    {"label": "Slack",          "key_env": "NANGO_KEY_SLACK",    "default_key": "slack"},
}


def host() -> str | None:
    h = get_credential("NANGO_HOST")
    return h.rstrip("/") if h else None


def secret() -> str | None:
    return get_credential("NANGO_SECRET_KEY")


def connection_id() -> str:
    return get_credential("NANGO_CONNECTION_ID") or "sunday"


def public_url() -> str | None:
    u = get_credential("NANGO_PUBLIC_URL")
    return u.rstrip("/") if u else None


def public_key() -> str | None:
    return get_credential("NANGO_PUBLIC_KEY")


def configured() -> bool:
    return bool(host() and secret())


def provider_key(provider: str) -> str:
    p = PROVIDERS.get(provider)
    if not p:
        return provider
    return get_credential(p["key_env"]) or p["default_key"]


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {secret()}", "Content-Type": "application/json"}


async def list_connections() -> list[dict[str, Any]]:
    if not configured():
        return []
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(f"{host()}/connection", headers=_headers())
    if res.status_code >= 400:
        log.warning("nango list connections failed", status=res.status_code, body=res.text[:200])
        return []
    data = res.json()
    return data.get("connections", data if isinstance(data, list) else [])


async def is_connected(provider: str) -> bool:
    key = provider_key(provider)
    cid = connection_id()
    for c in await list_connections():
        if c.get("provider_config_key") == key and str(c.get("connection_id")) == cid:
            return True
    return False


async def create_connect_session(provider: str, end_user: dict | None = None) -> dict[str, Any]:
    """Current Nango flow: mint a connect session token the Connect UI uses.
    Returns {token, connect_url} best-effort."""
    if not configured():
        return {"error": "Nango isn't configured. Set NANGO_HOST + NANGO_SECRET_KEY in Settings."}
    key = provider_key(provider)
    body = {
        "end_user": end_user or {"id": connection_id()},
        "allowed_integrations": [key],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(f"{host()}/connect/sessions", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"nango connect session failed ({res.status_code}): {res.text[:200]}"}
    token = (res.json().get("data") or {}).get("token")
    out = {"token": token, "provider_config_key": key}
    # Reliable no-SDK path: the OAuth-connect URL the user opens in a
    # browser. Nango runs the consent + handles the callback.
    if public_url() and public_key():
        out["connect_url"] = (
            f"{public_url()}/oauth/connect/{key}"
            f"?connection_id={connection_id()}&public_key={public_key()}"
        )
    return out


async def proxy(
    method: str,
    endpoint: str,
    provider: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> dict[str, Any]:
    """Call a provider API through Nango. Nango injects the OAuth token and
    the provider base URL; we pass the integration id + connection id."""
    if not configured():
        return {"error": "Nango isn't configured. Connect the integration in Settings → Connections."}
    headers = {
        "Authorization": f"Bearer {secret()}",
        "Provider-Config-Key": provider_key(provider),
        "Connection-Id": connection_id(),
        "Content-Type": "application/json",
    }
    url = f"{host()}/proxy/{endpoint.lstrip('/')}"
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.request(method.upper(), url, headers=headers, params=params, json=json)
    if res.status_code == 404 or (res.status_code == 400 and "connection" in res.text.lower()):
        return {"error": f"{PROVIDERS.get(provider, {}).get('label', provider)} isn't connected — connect it in Settings → Connections."}
    if res.status_code >= 400:
        return {"error": f"{provider} api {res.status_code}: {res.text[:300]}"}
    try:
        return res.json()
    except Exception:  # noqa: BLE001
        return {"raw": res.text}
