"""Sunday Relay — a dumb, stateless socket-broker for inbound webhooks.

What this is (spec §0, §2, §3): the *only* hosted component in Sunday, and
deliberately the least powerful one. It gives every Sunday daemon a public URL
for inbound events without the user touching DNS, Tailscale Funnel, Caddy, or
port-forwarding. The daemon dials *out* over one persistent WebSocket; the
relay forwards inbound HTTP down that socket and waits for the daemon's reply.
This is the tmate model: the thing being reached makes the outbound connection,
the relay just brokers.

The guiding constraint is that this process is a DUMB PIPE:
  - Stateless: it holds no agent state, no brain, no memory, no message store.
  - Payload-blind: inbound bodies are forwarded *opaquely* — never parsed.
  - Capability-poor: a relay breach leaks routing, not capability (spec §3.1).

It does exactly four things:
  1. Accept daemon WebSocket connections and authenticate them (`hello`).
  2. Map `agent_id -> open socket`.
  3. On a public webhook POST, forward a frame down the right socket and relay
     the daemon's ack back to the original HTTP caller.
  4. Rate-limit per agent and buffer a tiny ring of unacked frames so a 2s
     daemon reconnect doesn't lose an event.

Everything cleverer than that lives on the daemon, on purpose — that's what
keeps the relay BYO-able (spec §9): self-hosting it is "deploy a small
socket-broker, set a URL," not "stand up a copy of Sunday."

Wire protocol (MUST stay byte-identical to the daemon's relay client, spec §2):

  daemon -> relay on connect:
    {"type":"hello","agent_id":"<id>","token":"<secret>","version":1}

  relay -> daemon, forwarding an inbound request:
    {"type":"webhook","id":"<req-id>","slug":"<str>","method":"POST",
     "path":"/webhooks/<...>","headers":{...},"body":"<raw string>"}

  daemon -> relay, ack (carries the loopback response):
    {"type":"ack","id":"<req-id>","status":200,"body":"<raw string>"}

  liveness: {"type":"ping"} / {"type":"pong"} ~every 20s.

Public webhook route:  POST /u/{agent_id}/{slug}
Daemon socket route:   GET  /ws
Health:                GET  /health
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from typing import Any

import structlog
from aiohttp import WSMsgType, web

from registry import AgentRegistry

log = structlog.get_logger("relay")

# ─── tunables (env-overridable so deploys can tighten without a code change) ──
# Why env vars and not config files: the relay is meant to be deployed anywhere
# in one command (Dockerfile + a few -e flags). Keeping every knob on the
# environment is what makes "deploy a small socket-broker, set a URL" true.

# How long the public HTTP caller will wait for the daemon's ack before we give
# up and return 504. Providers (Stripe/Sendblue/AgentMail) retry on 5xx, so a
# 504 is recoverable — better than holding the connection open forever when the
# daemon is wedged.
ACK_TIMEOUT_SECONDS = float(os.environ.get("RELAY_ACK_TIMEOUT", "25"))

# Liveness. We send a ping every PING_INTERVAL and drop a socket that misses
# MISSED_PONGS in a row. The daemon reconnects with backoff and resumes from the
# buffer, so an aggressive drop here is cheap — a stale socket that silently
# stopped delivering is far worse than a fast reconnect.
PING_INTERVAL_SECONDS = float(os.environ.get("RELAY_PING_INTERVAL", "20"))
MISSED_PONGS_BEFORE_DROP = int(os.environ.get("RELAY_MISSED_PONGS", "2"))

# Per-agent token bucket (spec §3.4 — the one shared chokepoint). Defaults are
# generous for normal provider traffic but cap a runaway/abusive source. Refill
# is continuous (RATE_REFILL tokens/sec) up to RATE_BURST.
RATE_BURST = float(os.environ.get("RELAY_RATE_BURST", "60"))
RATE_REFILL_PER_SEC = float(os.environ.get("RELAY_RATE_REFILL", "10"))

# Trust-on-first-use enrollment (spec §3, multi-tenant + BYO). An UNKNOWN
# agent_id is enrolled on its first `hello` — no admin token needed. That single
# decision is what makes BOTH Sunday's hosted relay and a self-hosted BYO relay
# work by just pointing a URL and connecting; nobody hand-registers a daemon.
# It's safe because the agent_id is an unguessable 256-bit value (you can't
# claim what you can't guess) and the bound token gates every later connect.
# The only residual abuse vector is enrollment SPAM from one source, so we cap
# NEW enrollments per IP per window (known agents reconnecting are never limited)
# and bound total registry growth.
ENROLL_MAX_PER_IP  = int(os.environ.get("RELAY_ENROLL_MAX_PER_IP", "20"))
ENROLL_WINDOW_SECS = float(os.environ.get("RELAY_ENROLL_WINDOW", "3600"))
ENROLL_MAX_AGENTS  = int(os.environ.get("RELAY_ENROLL_MAX_AGENTS", "10000"))

# Reconnect buffer (spec §2, §8). Ring of recent *unacked* webhook frames per
# agent, replayed once on reconnect so a brief daemon blip doesn't drop an
# event. Bounded by count AND age — anything older than the TTL falls through to
# the daemon's poller backstop, which is the by-design safety net, not a bug.
BUFFER_MAX_FRAMES = int(os.environ.get("RELAY_BUFFER_FRAMES", "32"))
BUFFER_TTL_SECONDS = float(os.environ.get("RELAY_BUFFER_TTL", "30"))

# Hard cap on inbound body size we'll forward. The relay is payload-blind but
# not payload-infinite — an unbounded body is a memory-exhaustion vector on a
# shared box. 2 MiB comfortably covers webhook payloads (emails, JSON events).
MAX_BODY_BYTES = int(os.environ.get("RELAY_MAX_BODY", str(2 * 1024 * 1024)))

# Headers we strip before forwarding. Hop-by-hop headers are meaningless past
# the relay edge, and Authorization/Cookie are stripped so we never carry a
# provider-or-caller credential down to the daemon (the daemon trusts the
# loopback hop, not these). The relay stays as payload/credential-blind as it
# can while still forwarding what handlers need (Content-Type, signature hdrs).
_STRIPPED_HEADERS = {
    "host", "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
    "authorization", "cookie", "content-length",
}


# ─── per-agent connection state ──────────────────────────────────────────────


class AgentConn:
    """Everything the relay tracks for one connected daemon. Created on a valid
    `hello`, torn down on socket close. Deliberately holds NO agent data — only
    the socket, the in-flight ack futures, the rate bucket, and the replay ring.
    """

    def __init__(self, agent_id: str, ws: web.WebSocketResponse) -> None:
        self.agent_id = agent_id
        self.ws = ws
        # id -> Future the public-webhook coroutine is awaiting. The ack handler
        # resolves these by id; this is the whole request/response correlation.
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Token bucket (see _allow). Start full so a fresh connection isn't
        # rate-limited on its first event.
        self.tokens = RATE_BURST
        self.last_refill = time.monotonic()
        # Reconnect ring: (enqueued_at, frame_dict) for frames sent but not yet
        # acked. Replayed on the NEXT connection for this agent_id.
        self.buffer: deque[tuple[float, dict[str, Any]]] = deque(maxlen=BUFFER_MAX_FRAMES)
        # Liveness bookkeeping. Bumped on any inbound frame (a pong, an ack, or
        # anything) — proves the socket is alive, not just the pong path.
        self.pongs_outstanding = 0

    def allow(self) -> bool:
        """Per-agent token bucket. Continuous refill, capped at RATE_BURST.
        Returns False when the agent is over budget (caller answers 429)."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.last_refill = now
        self.tokens = min(RATE_BURST, self.tokens + elapsed * RATE_REFILL_PER_SEC)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def buffer_frame(self, frame: dict[str, Any]) -> None:
        """Remember a sent-but-unacked frame for possible replay on reconnect.
        The deque's maxlen drops the oldest automatically; TTL is enforced at
        replay time so we never replay something the poller already covers."""
        self.buffer.append((time.monotonic(), frame))

    def ack_frame(self, req_id: str) -> None:
        """Drop a frame from the replay ring once its ack lands — an acked frame
        was delivered, so it must not be replayed on a later reconnect."""
        # deque has no by-key removal; rebuild without the acked id. The ring is
        # tiny (<=32), so this is cheap and keeps the buffer honest.
        self.buffer = deque(
            ((ts, f) for (ts, f) in self.buffer if f.get("id") != req_id),
            maxlen=BUFFER_MAX_FRAMES,
        )


# ─── the relay application ───────────────────────────────────────────────────


class Relay:
    """Holds the live `agent_id -> AgentConn` map and the agent registry. One
    instance per process; stashed on the aiohttp app so handlers can reach it.
    """

    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry
        # The single piece of mutable routing state in the whole service.
        self.conns: dict[str, AgentConn] = {}
        # Per-IP timestamps of recent NEW enrollments (TOFU spam guard). Only
        # touched on first-ever hello for an unknown agent_id; reconnects skip it.
        self._enroll_hits: dict[str, deque] = {}

    def _enroll_allowed(self, ip: str) -> bool:
        """Gate trust-on-first-use enrollment of a brand-new agent_id. Caps NEW
        enrollments per source IP per window, and total registry size. Returns
        False to refuse (the socket is then closed 4029)."""
        if len(self.registry._agents) >= ENROLL_MAX_AGENTS:
            return False
        now = time.monotonic()
        hits = self._enroll_hits.setdefault(ip or "?", deque())
        while hits and now - hits[0] > ENROLL_WINDOW_SECS:
            hits.popleft()
        if len(hits) >= ENROLL_MAX_PER_IP:
            return False
        hits.append(now)
        return True

    # ─── daemon socket: GET /ws ──────────────────────────────────────────────

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """The endpoint a daemon dials OUT to. First frame must be `hello`; we
        authenticate it against the registry, then pump frames until close.

        One coroutine owns each socket end-to-end: it reads frames, resolves ack
        futures, and answers pings. A sibling task (started here) sends pings on
        a timer and drops the socket after MISSED_PONGS_BEFORE_DROP misses.
        """
        ws = web.WebSocketResponse(heartbeat=None, max_msg_size=MAX_BODY_BYTES + 64 * 1024)
        await ws.prepare(request)

        # ── handshake: the first frame MUST be a valid `hello` ──────────────
        # We don't map the socket until it authenticates, so an unauthenticated
        # connection can never receive a webhook frame.
        try:
            first = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            await ws.close(code=4001, message=b"hello timeout")
            return ws
        if first.type is not WSMsgType.TEXT:
            await ws.close(code=4001, message=b"expected hello frame")
            return ws
        try:
            hello = json.loads(first.data)
        except (ValueError, TypeError):
            await ws.close(code=4001, message=b"hello not JSON")
            return ws

        agent_id = str(hello.get("agent_id") or "")
        token = str(hello.get("token") or "")
        if hello.get("type") != "hello" or not agent_id or not token:
            await ws.close(code=4001, message=b"malformed hello")
            return ws
        # Auth + trust-on-first-use enrollment (spec §3.2). The socket token
        # proves "I am agent X's daemon" — a separate credential from the public
        # URL's agent_id. A KNOWN agent_id must present its bound token (constant
        # time). An UNKNOWN agent_id is enrolled on the spot — no admin token —
        # so a daemon (Sunday's relay OR a BYO relay) comes online by just
        # connecting. The agent_id is unguessable, so enrollment can't hijack
        # anyone; we only rate-limit enrollment spam per source IP.
        if self.registry.has_agent(agent_id):
            if not self.registry.verify_token(agent_id, token):
                log.warning("ws auth rejected", agent_id=_redact(agent_id))
                await ws.close(code=4003, message=b"auth failed")
                return ws
        else:
            if not self._enroll_allowed(request.remote or ""):
                log.warning("ws enroll rate-limited", ip=request.remote)
                await ws.close(code=4029, message=b"enroll rate limited")
                return ws
            self.registry.register(agent_id, token)
            log.info("ws enrolled (tofu)", agent_id=_redact(agent_id))

        # ── replace any prior socket for this agent (reconnect) ─────────────
        # A daemon that reconnects before the old socket's close was processed
        # would otherwise leave a stale entry. New socket wins; old is closed.
        prior = self.conns.get(agent_id)
        conn = AgentConn(agent_id, ws)
        if prior is not None:
            # Carry the prior socket's replay ring forward so its unacked frames
            # get a second delivery chance on this fresh connection.
            conn.buffer = prior.buffer
            try:
                await prior.ws.close(code=4000, message=b"superseded")
            except Exception:  # noqa: BLE001
                pass
        self.conns[agent_id] = conn
        log.info("ws connected", agent_id=_redact(agent_id), version=hello.get("version"))

        # Confirm the handshake so the daemon knows it's mapped and can stop
        # treating the connection as tentative.
        await self._send(conn, {"type": "welcome", "agent_id": agent_id})

        # Replay any buffered unacked frames (spec §2 resume). Strictly best
        # effort — anything past the TTL is dropped to the poller backstop.
        await self._replay_buffer(conn)

        ping_task = asyncio.create_task(self._ping_loop(conn))
        try:
            await self._read_loop(conn)
        finally:
            ping_task.cancel()
            # Only unmap if we're still the current socket — a newer reconnect
            # may have already replaced us, and we must not evict it.
            if self.conns.get(agent_id) is conn:
                del self.conns[agent_id]
            # Fail any still-pending acks so their HTTP callers get a 504 rather
            # than hanging until ACK_TIMEOUT.
            for fut in conn.pending.values():
                if not fut.done():
                    fut.cancel()
            log.info("ws disconnected", agent_id=_redact(agent_id))
        return ws

    async def _read_loop(self, conn: AgentConn) -> None:
        """Pump inbound frames from one daemon socket. Resolves ack futures,
        answers pings, tracks liveness. Returns when the socket closes."""
        async for msg in conn.ws:
            if msg.type is WSMsgType.TEXT:
                conn.pongs_outstanding = 0  # any inbound frame proves liveness
                try:
                    frame = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue  # the relay is dumb — ignore garbage, don't crash
                ftype = frame.get("type")
                if ftype == "ack":
                    self._handle_ack(conn, frame)
                elif ftype == "ping":
                    # Daemon-initiated ping — answer so its liveness check passes.
                    await self._send(conn, {"type": "pong"})
                elif ftype == "pong":
                    pass  # liveness already credited above
                # Any other frame type is ignored: forward-compat, dumb-pipe.
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    def _handle_ack(self, conn: AgentConn, frame: dict[str, Any]) -> None:
        """Correlate an ack back to the waiting public-webhook coroutine by id,
        and drop the frame from the replay ring (it was delivered)."""
        req_id = str(frame.get("id") or "")
        conn.ack_frame(req_id)
        fut = conn.pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result(frame)
        # An ack with no waiting future means the caller already timed out (504)
        # — harmless; we just drop it.

    async def _ping_loop(self, conn: AgentConn) -> None:
        """Send a ping every PING_INTERVAL; drop the socket after N missed
        pongs. Runs alongside the read loop for the life of the connection."""
        try:
            while not conn.ws.closed:
                await asyncio.sleep(PING_INTERVAL_SECONDS)
                if conn.pongs_outstanding >= MISSED_PONGS_BEFORE_DROP:
                    log.warning("ws missed pongs, dropping", agent_id=_redact(conn.agent_id))
                    await conn.ws.close(code=4002, message=b"missed pongs")
                    return
                conn.pongs_outstanding += 1
                await self._send(conn, {"type": "ping"})
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            return

    async def _replay_buffer(self, conn: AgentConn) -> None:
        """On reconnect, re-send buffered unacked frames that are still within
        TTL. Past-TTL frames are dropped (poller backstop covers them)."""
        if not conn.buffer:
            return
        now = time.monotonic()
        replayed = 0
        # Snapshot then clear: replayed frames re-enter the ring via _send's
        # buffering path, with a fresh timestamp, so TTL is measured from the
        # replay, not the original enqueue.
        snapshot = list(conn.buffer)
        conn.buffer.clear()
        for ts, frame in snapshot:
            if (now - ts) > BUFFER_TTL_SECONDS:
                continue  # too old — let the daemon's poller catch it
            await self._send(conn, frame)
            conn.buffer_frame(frame)
            replayed += 1
        if replayed:
            log.info("ws replayed buffered frames", agent_id=_redact(conn.agent_id), count=replayed)

    # ─── public webhook: POST /u/{agent_id}/{slug} ───────────────────────────

    async def webhook_handler(self, request: web.Request) -> web.Response:
        """The PUBLIC endpoint providers POST to. The hot path of the service.

        Steps (spec §2):
          1. Rate-limit per agent_id (token bucket).
          2. Look up the agent's open socket (503 if not connected).
          3. Optional per-slug token / HMAC authorization (spec §3.3).
          4. Derive the local path from the slug (default /webhooks/<slug>).
          5. Forward a `webhook` frame down the socket, OPAQUE body.
          6. Await the matching ack (504 on timeout); relay its status+body back.
        """
        agent_id = request.match_info["agent_id"]
        slug = request.match_info["slug"]

        # The unguessable agent_id is itself the coarse auth (spec §3.2): an
        # agent we've never registered can't have a socket and shouldn't even be
        # probeable. Reject unknown agents identically to disconnected ones is
        # tempting, but 404 here vs 503 below leaks "registered but offline" —
        # we accept that: it's routing info, not capability, and the daemon
        # poller is the real delivery guarantee anyway.
        conn = self.conns.get(agent_id)
        if conn is None:
            # Not connected (or never registered). Provider should retry.
            return web.json_response(
                {"error": "agent not connected"}, status=503,
                headers={"Retry-After": "5"},
            )

        # (1) rate limit — the one shared chokepoint (spec §3.4)
        if not conn.allow():
            log.warning("rate limited", agent_id=_redact(agent_id), slug=slug)
            return web.json_response(
                {"error": "rate limited"}, status=429,
                headers={"Retry-After": "1"},
            )

        # (3) per-slug authorization. The agent_id is the coarse gate; a per-slug
        # token (and, for providers that sign, an HMAC) is the fine gate. Both
        # are optional per slug — the registry decides. A configured-but-wrong
        # token is a hard 403; an absent config means "the unguessable path is
        # the gate" (spec §3.3).
        body_bytes = await self._read_body(request)
        if body_bytes is None:
            return web.json_response({"error": "body too large"}, status=413)
        authz = self.registry.authorize_webhook(
            agent_id, slug,
            query_token=request.query.get("token"),
            header_token=request.headers.get("X-Relay-Token"),
            body=body_bytes,
            headers=request.headers,
        )
        if not authz:
            log.warning("webhook authz rejected", agent_id=_redact(agent_id), slug=slug)
            return web.json_response({"error": "unauthorized"}, status=403)

        # (4) derive the local path. Default identity mapping keeps the relay
        # dumb — the daemon does any real slug->path remap (spec §2). A registry
        # override exists only for the rare blessed case (e.g. a sendblue secret
        # baked into the path), but is discouraged.
        path = self.registry.local_path(slug)

        # (5) forward an OPAQUE frame. We never json.loads the body — payload
        # blindness is the security property (spec §3.1). Body goes down as a
        # raw string; the daemon hands it to the channel byte-identically to a
        # Funnel delivery.
        req_id = uuid.uuid4().hex
        frame = {
            "type": "webhook",
            "id": req_id,
            "slug": slug,
            "method": request.method,
            "path": path,
            "headers": _forwardable_headers(request.headers),
            "body": body_bytes.decode("utf-8", errors="replace"),
        }

        # Register the ack future BEFORE sending, so an instant ack can't race us.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        conn.pending[req_id] = fut
        try:
            await self._send(conn, frame)
        except Exception as exc:  # noqa: BLE001
            conn.pending.pop(req_id, None)
            log.warning("webhook send failed", agent_id=_redact(agent_id), error=str(exc))
            return web.json_response({"error": "agent socket error"}, status=503)
        # Buffer for replay only after a successful send — a frame we couldn't
        # even send shouldn't be replayed (the provider will retry the POST).
        conn.buffer_frame(frame)

        # (6) await the ack, bounded. On timeout the provider gets a 504 and
        # retries; the frame stays buffered for a possible reconnect replay.
        try:
            ack = await asyncio.wait_for(asyncio.shield(fut), timeout=ACK_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            conn.pending.pop(req_id, None)
            log.warning("webhook ack timeout", agent_id=_redact(agent_id), slug=slug)
            return web.json_response({"error": "agent timeout"}, status=504)
        except asyncio.CancelledError:
            # Socket died mid-wait (the disconnect path cancels pending futures).
            return web.json_response({"error": "agent disconnected"}, status=503)

        # Relay the daemon's real status + body back to the provider verbatim, so
        # the provider sees exactly what the channel handler returned — a
        # relay-delivered webhook is indistinguishable from a Funnel one.
        status = int(ack.get("status") or 200)
        ack_body = ack.get("body")
        if ack_body is None:
            ack_body = ""
        return web.Response(
            status=status,
            text=ack_body if isinstance(ack_body, str) else json.dumps(ack_body),
            content_type="application/json",
        )

    # ─── helpers ─────────────────────────────────────────────────────────────

    async def _read_body(self, request: web.Request) -> bytes | None:
        """Read the inbound body with a hard size cap. Returns None if the body
        exceeds MAX_BODY_BYTES (caller answers 413). We read raw bytes and never
        parse — payload blindness (spec §3.1)."""
        # Cheap pre-check on the declared length, then a real cap while reading
        # in case Content-Length lied.
        declared = request.content_length
        if declared is not None and declared > MAX_BODY_BYTES:
            return None
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    async def _send(self, conn: AgentConn, frame: dict[str, Any]) -> None:
        """Send one JSON frame down a socket. Centralized so every write goes
        through one place (easier to reason about + log)."""
        await conn.ws.send_str(json.dumps(frame))


# ─── header forwarding ───────────────────────────────────────────────────────


def _forwardable_headers(headers: Any) -> dict[str, str]:
    """Copy inbound request headers minus hop-by-hop and credential headers
    (see _STRIPPED_HEADERS). What survives is what a channel handler needs:
    Content-Type and any provider signature headers (Stripe-Signature,
    X-AgentMail-Signature, etc.) so the daemon can verify signatures itself."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _STRIPPED_HEADERS:
            continue
        out[name] = value
    return out


def _redact(agent_id: str) -> str:
    """Log a prefix of the agent_id, never the whole thing. The full id is a
    credential (it's the unguessable URL gate, spec §3.2) — keep it out of logs
    that might end up shipped off-box."""
    return (agent_id[:8] + "…") if len(agent_id) > 8 else agent_id


# ─── plain HTTP routes ───────────────────────────────────────────────────────


async def health_handler(_request: web.Request) -> web.Response:
    """Liveness probe for the deploy platform / uptime checks. Deliberately
    leaks nothing — not even the connected-agent count, which is routing info."""
    return web.json_response({"ok": True})


async def register_handler(request: web.Request) -> web.Response:
    """Admin route a daemon self-registers through (spec §2 "Identity &
    registration"). Gated by ADMIN_TOKEN so only a trusted caller can mint
    routes; off entirely when ADMIN_TOKEN is unset (operator edits agents.json
    by hand instead — both paths are supported by design).

    Idempotent (mirrors Sendblue's GET-then-POST idempotency): re-POSTing the
    same agent_id/token is a no-op upsert, so a daemon can register on every
    boot without accumulating duplicates.

    Body: {"agent_id": "<id>", "token": "<socket-token>", "slugs": {...}?}
    Auth: Authorization: Bearer <ADMIN_TOKEN>
    """
    admin_token = os.environ.get("RELAY_ADMIN_TOKEN", "")
    if not admin_token:
        # No admin token configured -> registration-by-route is disabled. This
        # is a deliberate posture choice the operator makes per deploy.
        return web.json_response({"error": "registration disabled"}, status=404)
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {admin_token}"
    # Constant-time compare on the admin token, same discipline as socket auth.
    import hmac as _hmac
    if not _hmac.compare_digest(expected, supplied):
        return web.json_response({"error": "unauthorized"}, status=403)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent_id = str(payload.get("agent_id") or "")
    token = str(payload.get("token") or "")
    if not agent_id or not token:
        return web.json_response({"error": "agent_id and token required"}, status=400)
    relay: Relay = request.app["relay"]
    relay.registry.register(agent_id, token, payload.get("slugs"))
    return web.json_response({"ok": True, "agent_id": agent_id})


# ─── app factory ─────────────────────────────────────────────────────────────


def make_app() -> web.Application:
    """Build the aiohttp app. Separated from `main` so tests can construct the
    app without binding a port."""
    registry = AgentRegistry.from_env()
    relay = Relay(registry)

    app = web.Application(client_max_size=MAX_BODY_BYTES + 64 * 1024)
    app["relay"] = relay  # stash for tests / introspection

    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws", relay.ws_handler)
    # Admin self-registration (ADMIN_TOKEN-gated; 404 when unset).
    app.router.add_post("/admin/register", register_handler)
    # The public webhook route. {slug} is greedy-ish but single-segment here;
    # multi-segment user hooks (spec §4, /hook/<user-slug>) collapse to one slug
    # on the daemon side — the relay only needs *a* slug to forward.
    app.router.add_post("/u/{agent_id}/{slug:.*}", relay.webhook_handler)

    return app


def main() -> None:
    """Entrypoint. Binds host/port from the environment (PORT for PaaS
    compatibility — Render/Fly/Railway all inject it)."""
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", os.environ.get("RELAY_PORT", "8787")))
    log.info("relay starting", host=host, port=port)
    web.run_app(make_app(), host=host, port=port, print=None)


if __name__ == "__main__":
    main()
