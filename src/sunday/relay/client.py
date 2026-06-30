"""Relay client — the daemon's outbound WebSocket to the hosted relay.

This is the DAEMON side of the relay (spec §2). It registers ONE background
task: a connect loop that dials the relay over a persistent WebSocket, says
hello, and then services inbound `webhook` frames by replaying them onto the
daemon's OWN loopback HTTP surface. The reply status/body go back up as an
`ack`. Liveness rides ping/pong; a dropped socket reconnects with exponential
backoff + jitter.

Why loopback delivery is the right seam (spec §2):
    The relay client does NOT import channels, does NOT parse payloads, does
    NOT know what AgentMail or Sendblue are. On a `webhook` frame it does a
    plain HTTP POST to http://127.0.0.1:<port><path> with the forwarded
    method/headers/body. Because /webhooks/* is auth-exempt, no token plumbing
    is needed on that hop. Result: a relay-delivered webhook is byte-identical
    to a Funnel-delivered one. Channels can't tell the difference, so nothing
    channel-side ever changes.

Wire protocol (JSON frames over the WS), MUST match the relay service:
    daemon→relay  {"type":"hello","agent_id":<id>,"token":<RELAY_TOKEN>,"version":1}
    relay→daemon  {"type":"webhook","id":<req-id>,"slug":<str>,"method":"POST",
                   "path":"/webhooks/<...>","headers":{...},"body":<raw string>}
    daemon→relay  {"type":"ack","id":<req-id>,"status":<int>,"body":<raw string>}
    liveness      {"type":"ping"} / {"type":"pong"} ~every 20s

Two credentials, two jobs (spec §3): RELAY_TOKEN authenticates the SOCKET
("I am agent X's daemon"); the unguessable agent_id in the public URL authorizes
inbound POSTs ("this POST may reach agent X"). Compromising one doesn't grant
the other. Both are minted on first enable and persisted — agent_id into config
(SUNDAY_RELAY_AGENT_ID-overridable), RELAY_TOKEN into credentials.env, the same
store Sendblue uses.
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
from typing import Any
from urllib.parse import urlparse, urlunparse

import aiohttp
import structlog

from sunday.config import SundayConfig
from sunday.credentials import get_credential, set_credential
from sunday.tools import ToolRegistry

log = structlog.get_logger("sunday.relay")

# How often we proactively ping the relay, and how long we wait for ANY frame
# before assuming the socket is wedged and tearing it down to reconnect.
PING_INTERVAL_SECONDS = 20
RECV_TIMEOUT_SECONDS = 45        # ~2 missed pings → reconnect

# Reconnect backoff: exponential with full jitter, capped. A 2s flap shouldn't
# lose an event (the relay buffers a small unacked ring per agent, spec §2);
# anything older falls to the poller backup — by design, not perfection.
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 30.0

# Loopback delivery: how long we give the daemon's own webhook handler to run
# before giving up and acking an error upstream. Generous — a webhook handler
# may drive the brain (respond()) which can take seconds.
LOOPBACK_TIMEOUT_SECONDS = 120


# ─── identity (minted once, persisted) ─────────────────────────────────────
# On first enable the daemon needs a stable agent_id (public, unguessable, the
# coarse auth in the URL) and a RELAY_TOKEN (the socket secret). agent_id lives
# in config so the UI can read it; RELAY_TOKEN lives in credentials.env so it's
# never serialized into config.yaml.

def _ensure_agent_id(config: SundayConfig) -> str:
    """Return the agent_id, minting + persisting one on first enable.

    An explicit config/env value (SUNDAY_RELAY_AGENT_ID) wins. Otherwise we mint
    a urlsafe token and stash it back on the config object so the rest of this
    process — and public_url() — sees it immediately. (YAML persistence of the
    minted id rides the daemon's existing config-write path; we don't reach into
    config.yaml from here.)"""
    if config.relay.agent_id:
        return config.relay.agent_id
    agent_id = secrets.token_urlsafe(18)
    config.relay.agent_id = agent_id
    log.info("relay agent_id minted", agent_id=agent_id)
    return agent_id


def _ensure_relay_token() -> str:
    """Return RELAY_TOKEN, minting a 256-bit secret on first enable. Persisted
    to credentials.env (same store as the Sendblue keys), so it survives
    restarts and is never written into config."""
    existing = get_credential("RELAY_TOKEN")
    if existing:
        return existing
    token = secrets.token_urlsafe(32)   # 256 bits
    set_credential("RELAY_TOKEN", token)
    log.info("relay token minted")
    return token


# ─── public URL helper (for other modules / UI) ────────────────────────────

def _relay_host(url: str) -> str:
    """The relay's host[:port] from its ws/wss URL, e.g.
    'wss://relay.sunday.xyz' → 'relay.sunday.xyz'."""
    parsed = urlparse(url)
    return parsed.netloc or parsed.path  # tolerate a bare host with no scheme


def public_url(config: SundayConfig, slug: str) -> str | None:
    """The public https URL a provider posts to, for `slug`:
        https://<relay-host>/u/<agent-id>/<slug>
    Derived from config.relay.url (ws/wss → https). None when the relay isn't
    configured with an agent_id yet (nothing to point at)."""
    agent_id = config.relay.agent_id
    if not agent_id:
        return None
    host = _relay_host(config.relay.url)
    if not host:
        return None
    return f"https://{host}/u/{agent_id}/{slug.strip('/')}"


# ─── slug → local path mapping (the relay stays dumb) ──────────────────────
# The relay forwards an opaque `slug`; the daemon maps it to a /webhooks/...
# path. Default is identity (slug → /webhooks/<slug>). Blessed channels can
# override (e.g. Sendblue's secret-gated path) — but the relay frame already
# carries an explicit `path` in the common case, which we honor first. This map
# is the fallback when only a slug arrives.

def _slug_to_path(slug: str) -> str:
    slug = (slug or "").strip().strip("/")
    if not slug:
        return "/webhooks/"
    return f"/webhooks/{slug}"


# ─── frame handling ────────────────────────────────────────────────────────

async def _deliver_loopback(daemon: Any, frame: dict[str, Any]) -> dict[str, Any]:
    """Replay one `webhook` frame onto the daemon's own loopback HTTP surface
    and return {"status": int, "body": str} for the ack.

    The seam: we POST to http://127.0.0.1:<port><path>, forwarding the method,
    headers, and raw body verbatim. /webhooks/* is auth-exempt so we carry no
    token. The response is byte-identical to what a Funnel/direct caller would
    have seen — the channel handler can't tell this came via the relay."""
    port = daemon.config.server.port
    method = (frame.get("method") or "POST").upper()
    path = frame.get("path") or _slug_to_path(frame.get("slug", ""))
    body = frame.get("body")
    if body is None:
        body = ""
    body_bytes = body.encode("utf-8") if isinstance(body, str) else bytes(body)

    # Forward the provider's headers, but drop hop-by-hop / length headers that
    # aiohttp must set itself for the loopback request to be well-formed.
    headers = {}
    for k, v in (frame.get("headers") or {}).items():
        if k.lower() in ("host", "content-length", "connection", "transfer-encoding"):
            continue
        headers[k] = v

    url = f"http://127.0.0.1:{port}{path}"
    try:
        timeout = aiohttp.ClientTimeout(total=LOOPBACK_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.request(method, url, data=body_bytes, headers=headers) as resp:
                text = await resp.text()
                return {"status": resp.status, "body": text}
    except Exception as exc:  # noqa: BLE001
        log.warning("relay loopback delivery failed", path=path, error=str(exc))
        return {"status": 502, "body": json.dumps({"error": f"loopback: {type(exc).__name__}"})}


async def _handle_frame(daemon: Any, ws: aiohttp.ClientWebSocketResponse, frame: dict[str, Any]) -> None:
    """Dispatch one inbound frame. webhook → loopback → ack; ping → pong."""
    ftype = frame.get("type")
    if ftype == "ping":
        await ws.send_json({"type": "pong"})
        return
    if ftype == "pong":
        return  # liveness, nothing to do
    if ftype == "webhook":
        req_id = frame.get("id")
        result = await _deliver_loopback(daemon, frame)
        try:
            await ws.send_json({
                "type": "ack",
                "id": req_id,
                "status": result["status"],
                "body": result["body"],
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("relay ack send failed", id=req_id, error=str(exc))
        return
    log.debug("relay ignoring unknown frame", type=ftype)


async def _pinger(ws: aiohttp.ClientWebSocketResponse) -> None:
    """Send a ping every PING_INTERVAL_SECONDS so the relay knows we're alive
    and so a dead-but-not-closed socket surfaces via the recv timeout."""
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL_SECONDS)
            await ws.send_json({"type": "ping"})
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        return  # socket's going down; the connect loop will reconnect


async def _run_socket(daemon: Any, config: SundayConfig, agent_id: str, token: str) -> None:
    """One connection's lifetime: dial, hello, then service frames until the
    socket drops. Raises on any failure so the outer loop reconnects."""
    session = aiohttp.ClientSession()
    pinger: asyncio.Task | None = None
    # The relay serves the daemon socket at /ws; config.relay.url is the BASE
    # (public_url derives the https host from it), so append /ws here rather
    # than baking it into config. Idempotent if a base already ends in /ws.
    _base = config.relay.url.rstrip("/")
    ws_url = _base if _base.endswith("/ws") else _base + "/ws"
    try:
        async with session.ws_connect(ws_url, heartbeat=None) as ws:
            await ws.send_json({
                "type": "hello",
                "agent_id": agent_id,
                "token": token,
                "version": 1,
            })
            log.info("relay connected", url=ws_url, agent_id=agent_id)
            pinger = asyncio.create_task(_pinger(ws))

            while True:
                try:
                    msg = await ws.receive(timeout=RECV_TIMEOUT_SECONDS)
                except asyncio.TimeoutError:
                    log.warning("relay recv timeout — reconnecting")
                    return
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        frame = json.loads(msg.data)
                    except (json.JSONDecodeError, TypeError):
                        log.warning("relay got non-JSON frame")
                        continue
                    if isinstance(frame, dict):
                        # Each frame is serviced concurrently: a webhook that
                        # drives the brain (slow) must not block ping/pong or a
                        # second inbound event.
                        asyncio.create_task(_handle_frame(daemon, ws, frame))
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    log.warning("relay socket closing", msg_type=str(msg.type))
                    return
    finally:
        if pinger is not None:
            pinger.cancel()
        await session.close()


async def _connect_loop(daemon: Any) -> None:
    """The background task. Reconnects forever with exponential backoff + full
    jitter. Mints/loads identity once, up front, so the first hello is valid."""
    config: SundayConfig = daemon.config
    if not config.relay.enabled:
        log.info("relay disabled — connect loop not started")
        return

    agent_id = _ensure_agent_id(config)
    token = _ensure_relay_token()
    pub = public_url(config, "<slug>")
    log.info("relay client starting", url=config.relay.url, public_url=pub)

    backoff = BACKOFF_BASE_SECONDS
    while True:
        try:
            await _run_socket(daemon, config, agent_id, token)
            backoff = BACKOFF_BASE_SECONDS  # a clean disconnect resets backoff
        except asyncio.CancelledError:
            log.info("relay connect loop cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("relay connection failed", error=str(exc))
        # Full jitter: sleep uniformly in [0, backoff], then grow the ceiling.
        delay = random.uniform(0, backoff)
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, BACKOFF_MAX_SECONDS)


# ─── registration (channel-style) ──────────────────────────────────────────

def register(registry: ToolRegistry, config: SundayConfig) -> None:
    """Channel-style entry point. Registers the relay connect loop as a daemon
    background task — but ONLY when the relay is enabled, so a default install
    (relay.enabled = False) does exactly today's thing: no socket, no relay.

    No tools and no webhook handlers register here: the relay is a transport,
    not a channel. It delivers OTHER channels' webhooks via loopback."""
    if not config.relay.enabled:
        log.info("relay not enabled — skipping registration")
        return
    from sunday.daemon import register_background_task
    register_background_task(_connect_loop)
    log.info("relay client registered", url=config.relay.url)
