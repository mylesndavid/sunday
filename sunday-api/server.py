"""Sunday API — the thin hosted backbone for Sunday accounts.

What this is (docs/sunday-accounts-plan.md): the small managed layer that turns
"who you are" into "a Sunday account." It does four jobs and nothing cleverer:

  1. AUTH. Sits in front of WorkOS AuthKit. The daemon opens a browser at
     `/auth/start`; WorkOS authenticates the human; we exchange the code, map the
     WorkOS user to a stable Sunday account, and hand the daemon back its creds.
  2. IDENTITY ISSUANCE. On first sign-in we mint a stable `agent_id`,
     `relay_token`, and `sunday_key` and reuse them forever after — so a
     reinstall recovers the same identity (and the same relay URLs).
  3. A FREE-TIER MODEL GATEWAY. `/v1/chat/completions` proxies to OpenRouter on
     Sunday's master key, metering each call against a per-account free budget.
  4. RELAY VALIDATION. An internal endpoint the relay calls to replace its
     trust-on-first-use enrollment with "this agent_id belongs to an account."

Self-hosted Sunday never touches this — it's BYO relay + BYO model keys. The
account/plan is the default, never a dependency (plan §"Self-hosted").

This service is the OUTBOUND mirror of the relay's INBOUND job (plan §"The
frame"): the relay proxies inbound events to your agent; this proxies your
agent's model calls out through a Sunday plan. SQLite on a Fly volume at
/data/sunday.db is the source of truth (see db.py).

WorkOS endpoints used (User Management / AuthKit REST API — plain httpx, no SDK):
  - authorize:     GET  https://api.workos.com/user_management/authorize
                   ?client_id&redirect_uri&response_type=code&provider=authkit&state
                   (https://workos.com/docs/reference/user-management/authentication/get-authorization-url)
  - code→token:    POST https://api.workos.com/user_management/authenticate
                   {grant_type:"authorization_code", client_id, client_secret, code}
                   → {user:{id,email,...}, access_token, ...}
                   (https://workos.com/docs/reference/user-management/authentication/code)
  The User object exposes `id` and `email` (object="user"); that's all we read.

Routes:
  GET  /health                     → {ok:true}
  GET  /auth/start?cb=<localhost>   → 302 to WorkOS AuthKit (cb packed into state)
  GET  /auth/callback?code&state    → exchange, upsert account, 302 back to cb
  GET  /account                     (Bearer sunday_key) → plan + usage
  POST /v1/chat/completions         (Bearer sunday_key) → metered OpenRouter proxy
  POST /internal/validate-agent     (Bearer INTERNAL_SECRET) → {ok:true}|404
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
import structlog
from aiohttp import web

from db import AccountStore

log = structlog.get_logger("sunday-api")

# ─── config (env-overridable; this is a normal hosted service, every knob is an
# env var so a deploy can be reconfigured without a code change) ───────────────

# WorkOS User Management credentials. WORKOS_API_KEY is the secret used as the
# client_secret on the token exchange; WORKOS_CLIENT_ID identifies the
# environment; WORKOS_REDIRECT_URI is THIS service's own /auth/callback URL and
# MUST be registered in the WorkOS dashboard's Redirects list.
WORKOS_API_KEY = os.environ.get("WORKOS_API_KEY", "")
WORKOS_CLIENT_ID = os.environ.get("WORKOS_CLIENT_ID", "")
WORKOS_REDIRECT_URI = os.environ.get("WORKOS_REDIRECT_URI", "")
WORKOS_API_BASE = os.environ.get("WORKOS_API_BASE", "https://api.workos.com")

# The gateway's upstream. Sunday already routes models through OpenRouter, so the
# free tier is a metered budget on top of Sunday's master OpenRouter key.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = os.environ.get(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)

# Shared secret the relay presents on /internal/validate-agent. Unset → that
# route refuses everything (fail closed), same posture as the relay's unset
# ADMIN_TOKEN disabling /admin/register.
INTERNAL_SECRET = os.environ.get("INTERNAL_SECRET", "")

# Free-tier budget: total metered tokens (in+out) per account per calendar month.
# Generous for onboarding, farm-resistant given accounts gate it. The open
# question of the exact number lives in the plan; 1,000,000 is the default knob.
FREE_TIER_TOKENS = int(os.environ.get("FREE_TIER_TOKENS", "1000000"))

# Upstream timeouts. The model call can be slow (especially streaming), so the
# read timeout is generous; connect stays short to fail fast on a dead upstream.
UPSTREAM_TIMEOUT = httpx.Timeout(
    float(os.environ.get("SUNDAY_UPSTREAM_TIMEOUT", "120")), connect=10.0
)


def _current_period() -> str:
    """The usage bucket: current month "YYYY-MM". datetime.now() IS allowed here
    — this is a normal hosted service, not the workflow sandbox, so wall-clock
    time is the right and expected source for the metering window."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


# ─── auth helpers ─────────────────────────────────────────────────────────────


def _bearer(request: web.Request) -> str:
    """Pull the bearer token from the Authorization header. '' if absent/malformed
    — callers treat '' as unauthorized."""
    raw = request.headers.get("Authorization", "")
    if raw.startswith("Bearer "):
        return raw[len("Bearer ") :].strip()
    return ""


async def _account_for_request(request: web.Request) -> dict | None:
    """Resolve the account behind a Bearer `sunday_key`, or None. Shared by
    /account and the gateway so the auth path is identical for both."""
    key = _bearer(request)
    if not key:
        return None
    store: AccountStore = request.app["store"]
    return await store.account_by_sunday_key(key)


def _safe_local_cb(cb: str) -> bool:
    """Only allow the daemon's callback to be a loopback URL. The daemon listens
    on 127.0.0.1/localhost (mirroring Sunday's existing Codex OAuth callback
    flow), so we refuse to bounce the issued creds anywhere else — an open
    redirect here would leak a freshly-minted sunday_key to an attacker's host.
    """
    try:
        parts = urlsplit(cb)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _append_query(url: str, params: dict[str, str]) -> str:
    """Append query params to a URL, preserving any it already carries. Used to
    tack the issued creds onto the daemon's callback URL."""
    parts = urlsplit(url)
    existing = dict(parse_qsl(parts.query))
    existing.update(params)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(existing), parts.fragment)
    )


# ─── routes ───────────────────────────────────────────────────────────────────


async def health_handler(_request: web.Request) -> web.Response:
    """Liveness probe for Fly's health check / uptime monitors. Leaks nothing."""
    return web.json_response({"ok": True})


async def auth_start_handler(request: web.Request) -> web.Response:
    """Step 1 of the daemon sign-in handshake.

    The daemon has opened a localhost callback server and points the user's
    browser here with `?cb=<that-localhost-url>`. We 302 the browser to WorkOS's
    AuthKit authorize URL, packing `cb` into the OAuth `state` param so we get it
    back verbatim on the callback (state is the standard place to carry
    round-trip app context — WorkOS hands it back unmodified).

    WorkOS authorize endpoint (provider=authkit hosts the full sign-in UI):
      GET https://api.workos.com/user_management/authorize
    """
    cb = request.query.get("cb", "")
    if not cb or not _safe_local_cb(cb):
        # Refuse a missing or non-loopback callback (open-redirect guard, see
        # _safe_local_cb). Better a clear 400 than bouncing creds off-box.
        return web.json_response(
            {"error": "cb must be a loopback (localhost) URL"}, status=400
        )
    if not (WORKOS_CLIENT_ID and WORKOS_REDIRECT_URI):
        return web.json_response({"error": "auth not configured"}, status=500)

    authorize_url = f"{WORKOS_API_BASE}/user_management/authorize?" + urlencode(
        {
            "client_id": WORKOS_CLIENT_ID,
            # WorkOS redirects the browser back to OUR /auth/callback (must be a
            # registered redirect URI), not to the daemon — the daemon's cb rides
            # in `state` and we bounce to it at the end of the callback.
            "redirect_uri": WORKOS_REDIRECT_URI,
            "response_type": "code",
            "provider": "authkit",  # AuthKit-hosted sign-in (magic link / Google)
            "state": cb,
        }
    )
    raise web.HTTPFound(authorize_url)


async def auth_callback_handler(request: web.Request) -> web.Response:
    """Step 2 of the daemon sign-in handshake — the WorkOS redirect lands here.

    WorkOS has authenticated the human and redirected the browser back to this
    service's registered redirect URI with `?code=...&state=<the daemon cb>`. We:
      1. exchange the code with WorkOS for the user (id, email),
      2. upsert → the stable Sunday account (mint on first sign-in, reuse after),
      3. 302 the browser back to the daemon's loopback `cb` with the issued creds
         (agent_id, relay_token, sunday_key) as query params.

    The daemon's local callback server reads those params and persists them —
    this is the same browser-callback shape as Sunday's existing Codex OAuth flow.

    Token exchange (returns the user object):
      POST https://api.workos.com/user_management/authenticate
    """
    code = request.query.get("code", "")
    cb = request.query.get("state", "")  # the daemon's loopback URL we packed in
    if not code:
        return web.json_response({"error": "missing code"}, status=400)
    # Re-validate cb on the way out too — state is attacker-influenceable, so we
    # never trust it to redirect anywhere but a loopback URL.
    if not cb or not _safe_local_cb(cb):
        return web.json_response({"error": "invalid state/cb"}, status=400)
    if not (WORKOS_CLIENT_ID and WORKOS_API_KEY):
        return web.json_response({"error": "auth not configured"}, status=500)

    # (1) exchange the code. client_secret is the WORKOS_API_KEY (the WorkOS
    # User Management secret). Plain httpx form POST — no SDK.
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            resp = await client.post(
                f"{WORKOS_API_BASE}/user_management/authenticate",
                data={
                    "grant_type": "authorization_code",
                    "client_id": WORKOS_CLIENT_ID,
                    "client_secret": WORKOS_API_KEY,
                    "code": code,
                },
            )
    except httpx.HTTPError as exc:
        log.warning("workos exchange transport error", error=str(exc))
        return web.json_response({"error": "auth upstream error"}, status=502)

    if resp.status_code != 200:
        # Bad/expired/replayed code, or a misconfigured client. Surface a clear
        # failure; never echo WorkOS's raw body (could contain detail we don't
        # want to leak to a browser).
        log.warning("workos exchange failed", status=resp.status_code)
        return web.json_response({"error": "authentication failed"}, status=401)

    payload = resp.json()
    user = payload.get("user") or {}
    workos_user_id = str(user.get("id") or "")
    email = str(user.get("email") or "")
    if not workos_user_id:
        log.warning("workos exchange missing user id")
        return web.json_response({"error": "authentication failed"}, status=401)

    # (2) mint-or-reuse the stable Sunday account.
    store: AccountStore = request.app["store"]
    account = await store.upsert_account(workos_user_id, email)

    # (3) bounce back to the daemon's loopback callback with the issued creds.
    # These three are the daemon's whole identity: agent_id + relay_token wire up
    # the relay, sunday_key wires up this service's model gateway.
    target = _append_query(
        cb,
        {
            "agent_id": account["agent_id"],
            "relay_token": account["relay_token"],
            "sunday_key": account["sunday_key"],
        },
    )
    raise web.HTTPFound(target)


async def account_handler(request: web.Request) -> web.Response:
    """GET /account (Bearer sunday_key) → the account's identity + free-tier
    standing. The daemon shows this in its "Signed in to Sunday" surface.

    {email, agent_id, plan:"free", used, limit} where `used` = this period's
    metered tokens (in+out) and `limit` = FREE_TIER_TOKENS.
    """
    account = await _account_for_request(request)
    if account is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    store: AccountStore = request.app["store"]
    tin, tout = await store.usage_for(account["sunday_key"], _current_period())
    return web.json_response(
        {
            "email": account["email"],
            "agent_id": account["agent_id"],
            "plan": "free",
            "used": tin + tout,
            "limit": FREE_TIER_TOKENS,
        }
    )


async def chat_completions_handler(request: web.Request) -> web.StreamResponse:
    """POST /v1/chat/completions (Bearer sunday_key) — the free-tier model gateway.

    Flow (plan §"Model gateway + free tier"):
      1. Resolve the account from the sunday_key (401 if unknown).
      2. If this month's metered tokens ≥ FREE_TIER_TOKENS → 402, clear message.
      3. Proxy the request body VERBATIM to OpenRouter on Sunday's master key.
      4. Meter the `usage` block from the response into the account's budget.

    Streaming (`stream:true`) is supported: we pass the upstream SSE through
    byte-for-byte. OpenRouter's final stream chunk carries a `usage` block when
    asked, so we meter from the last `data:` line we see. Non-streaming is the
    simpler path and reads `usage` straight off the JSON response.
    """
    account = await _account_for_request(request)
    if account is None:
        return web.json_response({"error": "unauthorized"}, status=401)
    if not OPENROUTER_API_KEY:
        return web.json_response({"error": "gateway not configured"}, status=500)

    sunday_key = account["sunday_key"]
    period = _current_period()
    store: AccountStore = request.app["store"]

    # (2) free-tier gate — checked BEFORE proxying so an exhausted account never
    # spends Sunday's upstream credit. Counts both directions against one budget.
    tin, tout = await store.usage_for(sunday_key, period)
    if (tin + tout) >= FREE_TIER_TOKENS:
        return web.json_response(
            {
                "error": "free tier exhausted",
                "used": tin + tout,
                "limit": FREE_TIER_TOKENS,
            },
            status=402,
        )

    # Read the body as raw bytes and proxy it VERBATIM — we don't reshape the
    # caller's request (model, messages, params all pass through untouched). We
    # only peek at one flag to decide streaming vs buffered handling.
    body = await request.read()
    try:
        parsed = json.loads(body) if body else {}
        wants_stream = bool(parsed.get("stream"))
    except (ValueError, TypeError):
        parsed = {}
        wants_stream = False

    upstream_headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    if wants_stream:
        return await _proxy_streaming(
            request, store, sunday_key, period, body, upstream_headers
        )
    return await _proxy_buffered(
        store, sunday_key, period, body, upstream_headers
    )


async def _proxy_buffered(
    store: AccountStore,
    sunday_key: str,
    period: str,
    body: bytes,
    upstream_headers: dict[str, str],
) -> web.Response:
    """Non-streaming proxy: one round trip, read `usage` off the JSON, meter,
    return the body verbatim. The simple, reliable path."""
    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            resp = await client.post(
                OPENROUTER_URL, content=body, headers=upstream_headers
            )
    except httpx.HTTPError as exc:
        log.warning("openrouter transport error", error=str(exc))
        return web.json_response({"error": "model upstream error"}, status=502)

    # Meter only on a successful completion — an upstream error shouldn't draw
    # down the user's free budget.
    if resp.status_code == 200:
        try:
            usage = (resp.json() or {}).get("usage") or {}
            await store.add_usage(
                sunday_key,
                period,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
            )
        except (ValueError, TypeError) as exc:
            # A 200 with an unparseable body shouldn't fail the user's request —
            # just skip metering this call (loud in logs).
            log.warning("could not meter response", error=str(exc))

    # Return OpenRouter's response verbatim (status + body + content type), so
    # the caller sees exactly what a direct OpenRouter call would have returned.
    return web.Response(
        status=resp.status_code,
        body=resp.content,
        content_type=resp.headers.get("Content-Type", "application/json").split(";")[0],
    )


async def _proxy_streaming(
    request: web.Request,
    store: AccountStore,
    sunday_key: str,
    period: str,
    body: bytes,
    upstream_headers: dict[str, str],
) -> web.StreamResponse:
    """Streaming proxy: pass the upstream SSE through byte-for-byte while keeping
    the last seen `data:` JSON line so we can meter from its `usage` block after
    the stream ends. Best-effort metering — if no usage chunk arrives (the caller
    didn't ask OpenRouter to include it), we simply don't meter that call."""
    response = web.StreamResponse(
        status=200, headers={"Content-Type": "text/event-stream"}
    )
    prepared = False
    last_data_line = b""

    try:
        async with httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT) as client:
            async with client.stream(
                "POST", OPENROUTER_URL, content=body, headers=upstream_headers
            ) as upstream:
                if upstream.status_code != 200:
                    # Upstream rejected before any stream started — surface its
                    # status/body as a normal (non-stream) response.
                    err = await upstream.aread()
                    return web.Response(
                        status=upstream.status_code,
                        body=err,
                        content_type="application/json",
                    )
                response.headers["Content-Type"] = upstream.headers.get(
                    "Content-Type", "text/event-stream"
                )
                await response.prepare(request)
                prepared = True

                # Pass raw bytes straight through; remember the most recent
                # non-empty SSE `data:` line for post-stream metering. OpenRouter
                # emits a final chunk carrying `usage` (and a `[DONE]` sentinel).
                async for chunk in upstream.aiter_bytes():
                    await response.write(chunk)
                    for line in chunk.split(b"\n"):
                        line = line.strip()
                        if line.startswith(b"data:") and b"[DONE]" not in line:
                            last_data_line = line
    except httpx.HTTPError as exc:
        log.warning("openrouter stream transport error", error=str(exc))
        if not prepared:
            return web.json_response({"error": "model upstream error"}, status=502)
        # Stream already started; we can't change status now — just stop writing.
        return response

    # Meter from the last data chunk's usage block, if present (best-effort).
    if last_data_line:
        try:
            obj = json.loads(last_data_line[len(b"data:") :].strip())
            usage = obj.get("usage") or {}
            tin = int(usage.get("prompt_tokens") or 0)
            tout = int(usage.get("completion_tokens") or 0)
            if tin or tout:
                await store.add_usage(sunday_key, period, tin, tout)
        except (ValueError, TypeError):
            pass  # no usage in the final chunk → skip metering this call

    await response.write_eof()
    return response


async def validate_agent_handler(request: web.Request) -> web.Response:
    """POST /internal/validate-agent (Bearer INTERNAL_SECRET) — the hook the
    relay calls to replace trust-on-first-use with account-gating (plan §1).

    Body {agent_id} → {ok:true} if that agent_id belongs to a Sunday account,
    else 404. Not wired into the relay yet — exposed so it can be, later.

    Gated by a shared secret (constant-time compare). Unset INTERNAL_SECRET →
    refuse everything, so a misconfigured deploy fails closed.
    """
    if not INTERNAL_SECRET:
        return web.json_response({"error": "internal endpoint disabled"}, status=404)
    supplied = _bearer(request)
    # Constant-time compare so we don't leak the secret via timing.
    if not hmac.compare_digest(supplied, INTERNAL_SECRET):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        payload = await request.json()
    except (ValueError, TypeError):
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent_id = str(payload.get("agent_id") or "")
    if not agent_id:
        return web.json_response({"error": "agent_id required"}, status=400)
    store: AccountStore = request.app["store"]
    account = await store.account_by_agent_id(agent_id)
    if account is None:
        return web.json_response({"error": "unknown agent"}, status=404)
    return web.json_response({"ok": True})


# ─── app factory ──────────────────────────────────────────────────────────────


def make_app() -> web.Application:
    """Build the aiohttp app. Separated from `main` so tests can construct the
    app (and the store) without binding a port."""
    store = AccountStore.open()
    app = web.Application()
    app["store"] = store  # stash for handlers / tests

    app.router.add_get("/health", health_handler)
    # WorkOS AuthKit sign-in handshake (browser-facing).
    app.router.add_get("/auth/start", auth_start_handler)
    app.router.add_get("/auth/callback", auth_callback_handler)
    # Account + model gateway (Bearer sunday_key).
    app.router.add_get("/account", account_handler)
    app.router.add_post("/v1/chat/completions", chat_completions_handler)
    # Relay validation (Bearer INTERNAL_SECRET).
    app.router.add_post("/internal/validate-agent", validate_agent_handler)

    return app


def main() -> None:
    """Entrypoint. Binds host/port from the environment (PORT for PaaS
    compatibility — Fly/Render/Railway all inject it)."""
    host = os.environ.get("SUNDAY_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    log.info("sunday-api starting", host=host, port=port)
    web.run_app(make_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
