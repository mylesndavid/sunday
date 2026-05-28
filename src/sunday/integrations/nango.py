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

# friendly provider -> Nango integration id (provider_config_key), the Nango
# provider template, the OAuth scopes, and which env vars hold the OAuth app
# client id/secret. Set those env vars and Sunday auto-provisions the
# integration in Nango on startup — no dashboard clicking.
PROVIDERS = {
    "gmail": {
        "label": "Gmail", "key_env": "NANGO_KEY_GMAIL", "default_key": "google-mail",
        "template": "google-mail",
        "scopes": ["https://www.googleapis.com/auth/gmail.modify"],
        "client_id_env": "GOOGLE_CLIENT_ID", "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "calendar": {
        "label": "Google Calendar", "key_env": "NANGO_KEY_CALENDAR", "default_key": "google-calendar",
        "template": "google-calendar",
        "scopes": ["https://www.googleapis.com/auth/calendar"],
        "client_id_env": "GOOGLE_CLIENT_ID", "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
    "slack": {
        "label": "Slack", "key_env": "NANGO_KEY_SLACK", "default_key": "slack",
        "template": "slack",
        "scopes": ["channels:read", "channels:history", "chat:write", "users:read"],
        "client_id_env": "SLACK_CLIENT_ID", "client_secret_env": "SLACK_CLIENT_SECRET",
    },
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


async def get_integration(key: str) -> dict | None:
    if not configured():
        return None
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.get(f"{host()}/integrations/{key}", headers=_headers())
    if res.status_code == 200:
        return res.json().get("data", res.json())
    return None


async def ensure_integration(provider: str) -> dict[str, Any]:
    """Declaratively create the Nango integration from env-held OAuth client
    creds, if it doesn't already exist. Idempotent. (Nango 0.70.x API.)"""
    p = PROVIDERS.get(provider)
    if not p:
        return {"error": f"unknown provider {provider}"}
    if not configured():
        return {"error": "Nango not configured"}
    key = provider_key(provider)
    if await get_integration(key):
        return {"ok": True, "existed": True, "key": key}
    client_id = get_credential(p["client_id_env"])
    client_secret = get_credential(p["client_secret_env"])
    if not client_id or not client_secret:
        return {"skipped": True, "reason": f"set {p['client_id_env']} + {p['client_secret_env']}"}
    body = {
        "provider": p["template"],
        "unique_key": key,
        "credentials": {
            "type": "OAUTH2",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": ",".join(p["scopes"]),
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(f"{host()}/integrations", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"create integration failed ({res.status_code}): {res.text[:200]}"}
    log.info("nango integration provisioned", provider=provider, key=key)
    return {"ok": True, "created": True, "key": key}


async def provision_from_env() -> dict[str, Any]:
    """Ensure every provider whose OAuth client env vars are set has its
    Nango integration. Called on daemon startup + via the API."""
    if not configured():
        return {"configured": False}
    out = {}
    for provider in PROVIDERS:
        try:
            out[provider] = await ensure_integration(provider)
        except Exception as exc:  # noqa: BLE001
            out[provider] = {"error": str(exc)}
    return out


def connect_url_base() -> str | None:
    u = get_credential("NANGO_CONNECT_URL")
    return u.rstrip("/") if u else None


async def create_connect_session(provider: str, end_user: dict | None = None) -> dict[str, Any]:
    """Mint a connect session and return the public Connect UI URL the user
    opens in a browser. Ensures the integration exists first (env-driven)."""
    if not configured():
        return {"error": "Nango isn't configured. Set NANGO_HOST + NANGO_SECRET_KEY."}
    ens = await ensure_integration(provider)
    if ens.get("error"):
        return {"error": ens["error"]}
    if ens.get("skipped"):
        return {"error": f"{PROVIDERS[provider]['label']} has no OAuth client configured yet — {ens['reason']}."}
    key = provider_key(provider)
    body = {
        "end_user": end_user or {"id": connection_id()},
        "allowed_integrations": [key],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(f"{host()}/connect/sessions", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"nango connect session failed ({res.status_code}): {res.text[:200]}"}
    data = res.json().get("data") or {}
    token = data.get("token")
    # Nango's connect_link points at the internal connect-ui host
    # (localhost:3009); rebuild it against the public Connect UI URL.
    base = connect_url_base()
    link = f"{base}/?session_token={token}" if base else data.get("connect_link", "")
    return {"token": token, "provider_config_key": key, "connect_url": link}


# ─── catalog: read Nango's bundled provider list (the 830-entry yaml) ──────

# Cached so we don't refetch on every keystroke in the search box. Nango's
# /providers list is static across a release, refreshes on container restart.
_PROVIDER_CACHE: dict[str, Any] = {"data": None, "ts": 0.0}


async def list_providers(force: bool = False) -> list[dict[str, Any]]:
    """Fetch Nango's full provider catalog. Each entry carries display_name,
    categories, auth_mode, docs URLs, credentials schema — everything the
    setup card needs. Cached for 1h."""
    import time
    if not configured():
        return []
    if not force and _PROVIDER_CACHE["data"] is not None and (time.time() - _PROVIDER_CACHE["ts"] < 3600):
        return _PROVIDER_CACHE["data"]
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.get(f"{host()}/providers", headers=_headers())
    if res.status_code >= 400:
        log.warning("nango list providers failed", status=res.status_code)
        return []
    body = res.json()
    data = body.get("data", body) if isinstance(body, dict) else body
    # Normalize: Nango may return either a list or a dict-of-name->entry.
    if isinstance(data, dict):
        items = [{"name": k, **v} for k, v in data.items() if isinstance(v, dict)]
    else:
        items = data
    _PROVIDER_CACHE.update({"data": items, "ts": time.time()})
    return items


async def get_provider(name: str) -> dict[str, Any] | None:
    """Fetch a single provider entry. Resolves the `alias` chain so OAuth2
    fields (authorization_url, token_url, scopes) defined on the parent
    template are present on the child (Gmail inherits from Google, etc).
    """
    items = await list_providers()
    by_name = {p.get("name"): p for p in items if p.get("name")}
    target = by_name.get(name)
    if target is None:
        return None
    # Walk alias chain; child wins on key collisions.
    merged: dict[str, Any] = {}
    chain: list[str] = []
    cursor: dict[str, Any] | None = target
    seen: set[str] = set()
    while cursor and cursor.get("name") not in seen:
        seen.add(cursor.get("name", ""))
        chain.append(cursor.get("name", ""))
        # Merge parent first (older becomes base), then layer child on top.
        alias = cursor.get("alias")
        if alias and alias in by_name:
            cursor = by_name[alias]
        else:
            cursor = None
    # Apply in reverse so the deepest ancestor is the base, target overrides.
    for nm in reversed(chain):
        for k, v in by_name[nm].items():
            merged[k] = v
    merged["_chain"] = chain
    return merged


async def provision(
    provider_template: str,
    unique_key: str,
    credentials: dict[str, Any] | None = None,
    connection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Nango integration. Body shape depends on auth_mode:

      OAuth2 family (OAUTH2, OAUTH2_CC, APP, …) — `credentials` carries the
        DEVELOPER's app credentials (client_id + client_secret + scopes),
        stored at the integration level. The user's tokens land on the
        connection later via the Connect flow.

      API_KEY / BASIC / TWO_STEP / etc. — the integration itself stores
        NO credentials. The user's actual key/password lands on the
        connection during Connect. So we omit the credentials field entirely.

    Idempotent: returns the existing integration if one already exists under
    `unique_key`.
    """
    if not configured():
        return {"error": "Nango isn't configured."}
    existing = await get_integration(unique_key)
    if existing:
        return {"ok": True, "existed": True, "key": unique_key, "integration": existing}
    body: dict[str, Any] = {
        "provider": provider_template,
        "unique_key": unique_key,
    }
    if credentials:
        body["credentials"] = credentials
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(f"{host()}/integrations", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"create integration failed ({res.status_code}): {res.text[:300]}"}
    log.info("nango integration provisioned (dynamic)", provider=provider_template, key=unique_key)
    return {"ok": True, "created": True, "key": unique_key, "integration": res.json().get("data")}


def _camel_to_snake(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


async def create_connection_direct(
    unique_key: str,
    connection_id_val: str,
    auth_mode: str,
    user_credentials: dict[str, Any],
    connection_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a Nango connection directly (no browser hop) for auth modes
    where the user already has the credential in hand — API_KEY, BASIC,
    TWO_STEP, etc. The integration must already exist under `unique_key`.

    Nango's catalog names credential fields in camelCase (`apiKey`); the
    connection API expects them in snake_case (`api_key`). We convert here
    so callers can just pass whatever the catalog gave them.
    """
    if not configured():
        return {"error": "Nango isn't configured."}
    body: dict[str, Any] = {
        "connection_id": connection_id_val,
        "provider_config_key": unique_key,
    }
    # Flatten + normalize: camelCase → snake_case.
    for k, v in user_credentials.items():
        body[_camel_to_snake(k)] = v
    if connection_config:
        body["connection_config"] = connection_config
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(f"{host()}/connection", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"nango connection create failed ({res.status_code}): {res.text[:300]}"}
    log.info("nango connection created (direct)", key=unique_key, auth_mode=auth_mode)
    return {"ok": True, "direct": True}


async def create_connect_session_for_key(unique_key: str, end_user: dict | None = None) -> dict[str, Any]:
    """Mint a Connect UI session for a Nango integration that's already been
    provisioned (skips the env-driven `ensure_integration` step). Used by the
    dynamic-card flow where the user has just supplied their own creds."""
    if not configured():
        return {"error": "Nango isn't configured."}
    body = {
        "end_user": end_user or {"id": connection_id()},
        "allowed_integrations": [unique_key],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        res = await client.post(f"{host()}/connect/sessions", headers=_headers(), json=body)
    if res.status_code >= 400:
        return {"error": f"nango connect session failed ({res.status_code}): {res.text[:300]}"}
    data = res.json().get("data") or {}
    token = data.get("token")
    base = connect_url_base()
    link = f"{base}/?session_token={token}" if base else data.get("connect_link", "")
    return {"token": token, "provider_config_key": unique_key, "connect_url": link}


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
