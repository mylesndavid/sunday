"""Sendblue iMessage channel — Sunday's own phone number.

Inbound: webhook PRIMARY + polling BACKUP.
Outbound: send with retry on transient 5xx (Sendblue's sender pool has
intermittent outages — error code 5503).

Sendblue's free_api plan has shown two recurring issues we have to
defend against:
  1. /webhooks/sendblue stops firing after a stretch of failed
     deliveries — observed twice in 24h. Polling /accounts/messages
     every 30s catches drops within ~30s.
  2. Outbound /send-message returns 5503 ("sender unavailable") for
     a few minutes at a time. We retry on 5503 with backoff so a
     transient blip doesn't lose a reply.

Credentials:
  SENDBLUE_API_KEY_ID
  SENDBLUE_API_SECRET_KEY
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from aiohttp import web

from sunday.brain import respond
from sunday.config import SundayConfig
from sunday.credentials import get_credential, set_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.sendblue")

SENDBLUE_API_BASE = "https://api.sendblue.com/api"
SENDBLUE_SEND     = f"{SENDBLUE_API_BASE}/send-message"
SENDBLUE_TYPING   = f"{SENDBLUE_API_BASE}/send-typing-indicator"
SENDBLUE_MARK_READ = f"{SENDBLUE_API_BASE}/mark-read"
SENDBLUE_MESSAGES = "https://api.sendblue.com/accounts/messages"
SENDBLUE_WEBHOOKS = f"{SENDBLUE_API_BASE}/account/webhooks"

# Polling cadence for the inbound backup loop.
POLL_INTERVAL_SECONDS = 30
POLL_LIMIT            = 20
# How far back to look for inbound RECEIVED messages on first boot —
# protects against "deploy restart swallowed the message" without
# replaying ancient history.
SEED_GRACE_SECONDS    = 600

# Outbound retry config for Sendblue's transient 5503 ("sender
# unavailable") errors.
RETRY_TRANSIENT_CODES = {5503}
RETRY_ATTEMPTS        = 4   # roughly 1+2+4+8 = 15s of backoff total


def _sendblue_headers() -> dict[str, str] | None:
    api_key = get_credential("SENDBLUE_API_KEY_ID")
    api_secret = get_credential("SENDBLUE_API_SECRET_KEY")
    if not api_key or not api_secret:
        return None
    return {
        "sb-api-key-id": api_key,
        "sb-api-secret-key": api_secret,
        "Content-Type": "application/json",
    }


def normalize_phone(raw: str) -> str:
    """Best-effort E.164 for US numbers so the owner can type a bare
    10-digit number: '5550101234' -> '+15550101234', '15550101234' ->
    '+15550101234'. Anything already starting with '+' is left as-is, and
    unusual lengths fall through untouched for Sendblue to validate."""
    s = (raw or "").strip()
    if s.startswith("+"):
        return s
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return s


# ─── Sunday's own number (required as from_number on free_api sends) ───────
# Sendblue's free_api plan rejects /send-message with 400 "missing required
# parameter: from_number". GET /accounts exposes only a phoneID, not the
# number — so we discover Sunday's number from the most recent message's
# `sendblueNumber` and cache it (persisted to credentials, so later sends are
# instant and survive restarts). Inbound replies already pass the number they
# were texted from; this covers the test send and the brain's own outbound.

_default_from_number: str | None = None


async def discover_from_number() -> str | None:
    """Sunday's own Sendblue number, used as the required `from_number` on
    sends. Cached in-process + persisted; falls back to scanning recent
    messages for `sendblueNumber`. None only when there's no history yet."""
    global _default_from_number
    if _default_from_number:
        return _default_from_number
    stored = get_credential("SENDBLUE_FROM_NUMBER")
    if stored:
        _default_from_number = stored
        return stored
    headers = _sendblue_headers()
    if headers is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.get(SENDBLUE_MESSAGES, headers=headers, params={"limit": 25})
        if res.status_code != 200:
            return None
        counts: dict[str, int] = {}
        for m in _extract_messages(res.json()):
            num = m.get("sendblueNumber") or m.get("from_number")
            if num:
                counts[num] = counts.get(num, 0) + 1
        if not counts:
            return None
        best = max(counts, key=counts.get)
        _default_from_number = best
        set_credential("SENDBLUE_FROM_NUMBER", best)
        log.info("sendblue from_number discovered", number=best)
        return best
    except Exception as exc:  # noqa: BLE001
        log.warning("sendblue from_number discovery failed", error=str(exc))
        return None


_account_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def account_status() -> dict[str, Any]:
    """Connection facts for the desktop's Sendblue panel — so saving keys
    shows a real 'connected' state (Sunday's number, plan, owner email)
    instead of an empty-looking form. Cached 30s, keyed by the API key id so a
    key change busts it immediately."""
    headers = _sendblue_headers()
    if headers is None:
        return {"configured": False, "connected": False}
    key = headers.get("sb-api-key-id", "")
    now = time.monotonic()
    cached = _account_status_cache.get(key)
    if cached and (now - cached[0]) < 30:
        return cached[1]
    out: dict[str, Any] = {"configured": True}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get("https://api.sendblue.com/accounts", headers=headers)
        if res.status_code == 200:
            data = res.json().get("data") or {}
            out["connected"] = True
            out["plan"] = data.get("plan")
            emails = data.get("accountOwnerEmails") or []
            out["owner"] = (emails[0] if emails else None) or (data.get("email") or None)
        else:
            out["connected"] = False
            out["error"] = f"keys rejected (sendblue {res.status_code})"
    except Exception as exc:  # noqa: BLE001
        out["connected"] = False
        out["error"] = f"can't reach Sendblue — {type(exc).__name__}"
    out["number"] = await discover_from_number()
    _account_status_cache[key] = (now, out)
    return out


# ─── webhook secret ───────────────────────────────────────────────────────
# The inbound webhook is auth-exempt (external services can't carry our bearer
# token). When the brain runs on a private box reached via Tailscale Funnel,
# Funnel exposes ONLY /webhooks/sendblue/<secret> to the public internet — so
# the unguessable secret in the path IS the bearer token. The bare
# /webhooks/sendblue path stays registered for tailnet/local callers but is
# never funneled.

def _webhook_secret() -> str | None:
    return get_credential("SENDBLUE_WEBHOOK_SECRET")


def get_or_create_webhook_secret() -> str:
    """Stable, unguessable path segment that gates the public webhook.
    Generated once and persisted to ~/.sunday/credentials.env."""
    existing = _webhook_secret()
    if existing:
        return existing
    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    set_credential("SENDBLUE_WEBHOOK_SECRET", token)
    return token


def webhook_path() -> str:
    """The secret-gated inbound path, e.g. /webhooks/sendblue/<secret>."""
    return f"/webhooks/sendblue/{get_or_create_webhook_secret()}"


def public_webhook_url(dns_name: str | None) -> str | None:
    """The full https URL to paste into Sendblue, given the node's MagicDNS
    name. None when Tailscale isn't up yet (no public host to point at)."""
    if not dns_name:
        return None
    return f"https://{dns_name}{webhook_path()}"


# ─── webhook registration (no dashboard paste) ────────────────────────────
# Sendblue exposes account-level webhook management at /api/account/webhooks
# (GET list, POST append, PUT replace-all, DELETE). We use it to point the
# inbound "receive" event at our public Funnel URL over the API, so the owner
# never copies a webhook into the Sendblue dashboard. The API key + secret
# themselves can't be minted programmatically — those are still copied once
# from dashboard.sendblue.com — but everything after that is hands-off.

def _receive_webhook_urls(payload: Any) -> list[str]:
    """Extract the currently-registered 'receive' webhook URLs from a
    GET /account/webhooks response. Tolerates both shapes Sendblue's docs
    show: {"webhooks": {"receive": [...]}} and a flat list of {url, type}."""
    if not isinstance(payload, dict):
        return []
    hooks = payload.get("webhooks")
    out: list[str] = []
    if isinstance(hooks, dict):
        recv = hooks.get("receive")
        if isinstance(recv, list):
            for item in recv:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict) and item.get("url"):
                    out.append(str(item["url"]))
    elif isinstance(hooks, list):
        for item in hooks:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and item.get("url") and item.get("type", "receive") == "receive":
                out.append(str(item["url"]))
    return out


async def register_receive_webhook(dns_name: str | None) -> dict[str, Any]:
    """Point Sendblue's inbound 'receive' webhook at our public Funnel URL over
    the API, so the owner never pastes it into the dashboard.

    Idempotent: GET the current webhooks first and skip the POST if our exact
    URL is already registered — Sendblue's POST *appends*, so without this we'd
    accumulate duplicate hooks every time setup runs. Returns:
      {"ok": True,  "url": ..., "already": bool}
      {"ok": False, "error": ..., "url": ...|None}
    """
    headers = _sendblue_headers()
    if headers is None:
        return {"ok": False, "error": "Sendblue API key/secret not set", "url": None}
    url = public_webhook_url(dns_name)
    if not url:
        return {"ok": False, "error": "no public webhook URL yet — set up Tailscale Funnel first", "url": None}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            try:
                cur = await client.get(SENDBLUE_WEBHOOKS, headers=headers)
                if cur.status_code == 200 and url in _receive_webhook_urls(cur.json()):
                    log.info("sendblue webhook already registered", url=url)
                    return {"ok": True, "url": url, "already": True}
            except Exception as exc:  # noqa: BLE001
                log.warning("sendblue webhook GET failed, posting anyway", error=str(exc))
            res = await client.post(
                SENDBLUE_WEBHOOKS, headers=headers,
                json={"webhooks": [url], "type": "receive"},
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}
    if res.status_code >= 400:
        log.warning("sendblue webhook register failed", status=res.status_code, body=res.text[:200])
        return {"ok": False, "error": f"sendblue {res.status_code}: {res.text[:160]}", "url": url}
    log.info("sendblue webhook registered via API", url=url)
    return {"ok": True, "url": url, "already": False}


def _extract_messages(payload: Any) -> list[dict[str, Any]]:
    """Sendblue's /accounts/messages returns {messages: [...]}; some other
    endpoints wrap in {data: {messages: [...]}}. Handle either shape."""
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("messages"), list):
        return payload["messages"]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return data["messages"]
    return []


# ─── outbound ────────────────────────────────────────────────────────────


async def _send_typing(to: str, from_number: str | None = None) -> dict[str, Any]:
    """Show "Sunday is typing…" — best-effort, never blocks the brain."""
    headers = _sendblue_headers()
    if headers is None:
        return {"error": "credentials missing"}
    if from_number is None:
        from_number = await discover_from_number()
    payload: dict[str, Any] = {"number": to}
    if from_number:
        payload["from_number"] = from_number
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(SENDBLUE_TYPING, headers=headers, json=payload)
    if res.status_code >= 400:
        log.warning("sendblue typing failed", status=res.status_code, body=res.text[:200])
        return {"error": f"sendblue typing {res.status_code}"}
    return {"ok": True}


async def _send_read_receipt(to: str, from_number: str | None = None) -> dict[str, Any]:
    """Mark the sender's message as read — best-effort, never blocks.

    Read receipts must be enabled on the Sendblue account by their team;
    if it isn't, Sendblue returns an error we log and swallow."""
    headers = _sendblue_headers()
    if headers is None:
        return {"error": "credentials missing"}
    if from_number is None:
        from_number = await discover_from_number()
    payload: dict[str, Any] = {"number": to}
    if from_number:
        payload["from_number"] = from_number
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(SENDBLUE_MARK_READ, headers=headers, json=payload)
    if res.status_code >= 400:
        log.warning("sendblue read receipt failed", status=res.status_code, body=res.text[:200])
        return {"error": f"sendblue mark-read {res.status_code}"}
    return {"ok": True}


async def _ack_inbound(sender: str, sunday_number: str | None, source: str) -> None:
    """The instant we see an inbound text, fire a read receipt + typing
    indicator — concurrently, fire-and-forget — so the sender sees "Read"
    and "typing…" before the brain (which can take seconds) even starts.

    Texting is latency-sensitive: this is the perceived-latency win, and it
    ships independent of however long respond() actually takes. Resolve the
    Sunday number once (it's on the inbound payload in the common case, so no
    network round-trip) and hand it to both acks."""
    from_number = sunday_number or await discover_from_number()

    async def _read() -> None:
        _t = time.perf_counter()
        await _send_read_receipt(sender, from_number=from_number)
        log.info("sendblue read receipt sent", ms=round((time.perf_counter() - _t) * 1000), source=source)

    async def _typing() -> None:
        _t = time.perf_counter()
        await _send_typing(sender, from_number=from_number)
        log.info("sendblue typing sent", ms=round((time.perf_counter() - _t) * 1000), source=source)

    asyncio.create_task(_read())
    asyncio.create_task(_typing())


async def _send_sendblue(
    to: str,
    body: str,
    media_url: str | None = None,
    from_number: str | None = None,
) -> dict[str, Any]:
    """Send a message with retry on transient 5xx (5503 "sender
    unavailable" is Sendblue's most common flap)."""
    headers = _sendblue_headers()
    if headers is None:
        return {
            "error": "SENDBLUE_API_KEY_ID / SENDBLUE_API_SECRET_KEY missing. "
                     "Run: sunday credential set SENDBLUE_API_KEY_ID <key>"
        }

    # free_api requires from_number on every send; default to Sunday's own
    # number when the caller didn't pass one (test send, brain-initiated text).
    if from_number is None:
        from_number = await discover_from_number()

    payload: dict[str, Any] = {"number": to, "content": body}
    if media_url:
        payload["media_url"] = media_url
    if from_number:
        payload["from_number"] = from_number

    backoff = 1.0
    last_err: dict[str, Any] = {}
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(SENDBLUE_SEND, headers=headers, json=payload)
        try:
            data = res.json()
        except ValueError:
            data = {"raw": res.text}

        # HTTP layer success — Sendblue might still embed an error in body
        if res.status_code < 400:
            ec = data.get("error_code") if isinstance(data, dict) else None
            if ec in RETRY_TRANSIENT_CODES:
                last_err = {"error_code": ec, "error_message": data.get("error_message"), "attempt": attempt}
                log.warning("sendblue transient error, retrying", error_code=ec, attempt=attempt)
                if attempt < RETRY_ATTEMPTS:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                return {"error": f"sendblue {ec}: {data.get('error_message')}", "attempts": attempt}
            return {"ok": True, "to": to, "data": data}

        # HTTP error — retry once on 5xx, give up on 4xx
        if 500 <= res.status_code < 600 and attempt < RETRY_ATTEMPTS:
            last_err = {"http": res.status_code, "attempt": attempt}
            log.warning("sendblue 5xx, retrying", status=res.status_code, attempt=attempt)
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        return {"error": f"sendblue {res.status_code}: {data}"}

    return {"error": "sendblue retries exhausted", **last_err}


# ─── inbound: webhook (primary) ──────────────────────────────────────────


async def _process_inbound(
    daemon: Any,
    sender: str,
    sunday_number: str,
    text: str,
    media_url: str | None,
    source: str,
    uid: str | None = None,
) -> None:
    """Drive the brain pipeline + send the reply. Shared by webhook + poll.

    Every phase is timed and logged ("sendblue turn timing") so we can see
    exactly where wall-clock goes: the typing-indicator round-trip, the agent
    loop (broken down further by respond()'s own "turn timing"), and the
    outbound send.

    Read receipt + typing already fired at the call site (`_ack_inbound`)
    the moment the message was seen — so the user sees "Read"/"typing…"
    before this even starts."""
    t0 = time.perf_counter()

    timings: dict[str, Any] = {}
    t_respond = time.perf_counter()
    try:
        reply = await respond(
            daemon.chat,
            text,
            f"imessage_sendblue:{source}",
            daemon.config,
            daemon.registry,
            runtime=getattr(daemon, "runtime", None),
            extras={"broadcast": daemon._broadcast, "devices": daemon.devices,
                    "memory": daemon.memory, "runtime": getattr(daemon, "runtime", None),
                    # Tiered tools, same as the desktop chat path: send the lean
                    # core (~4.3k tok) instead of the full 72-tool schema (~10.5k
                    # tok), and let find_tools surface the rest on demand. Texting
                    # was the one channel still paying for every tool every turn —
                    # ~6k tokens of prefill on a latency-sensitive path. Shares the
                    # daemon's session-wide active set with the chat UI.
                    "registry": daemon.registry,
                    "active_tools": daemon._active_tools},
            timings=timings,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("inbound brain failed", source=source, uid=uid)
        return
    respond_ms = round((time.perf_counter() - t_respond) * 1000)

    t_send = time.perf_counter()
    await _send_sendblue(sender, reply, from_number=sunday_number or None)
    send_ms = round((time.perf_counter() - t_send) * 1000)

    log.info(
        "sendblue turn timing",
        source=source,
        total_ms=round((time.perf_counter() - t0) * 1000),
        respond_ms=respond_ms,
        send_ms=send_ms,
        llm_calls_ms=timings.get("llm_calls_ms"),
        memory_ms=round(timings.get("memory_ms", 0)),
        tools_ms=round(timings.get("tools_ms", 0)),
        iterations=timings.get("iterations"),
        tools=timings.get("tool_names", []),
        reply_chars=len(reply or ""),
    )


async def _webhook_handler(request: web.Request, daemon: Any) -> web.Response:
    """Sendblue inbound webhook — first chance to see a message."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)

    if request.path == "/webhooks/sendblue" and _webhook_secret():
        log.warning("sendblue hit on legacy unsecured path — point Sendblue at the secret webhook URL")

    if body.get("is_outbound"):
        return web.json_response({"ok": True, "skipped": "outbound receipt"})
    status = (body.get("status") or "").upper()
    if status and status not in ("RECEIVED", "DELIVERED"):
        return web.json_response({"ok": True, "skipped": f"status={status}"})

    sender        = body.get("number") or ""
    sunday_number = body.get("from_number") or ""
    text          = body.get("content") or ""
    media_url     = body.get("media_url")
    uid           = body.get("uuid") or body.get("externalId")

    if not text and media_url:
        text = f"(media: {media_url})"
    if not sender or not text:
        return web.json_response({"ok": True, "skipped": "missing sender/content"})

    # First action, before anything else: ack the message (read receipt +
    # typing) so the sender gets instant feedback while the brain runs.
    await _ack_inbound(sender, sunday_number, "webhook")

    # Mark UID seen so the poller doesn't double-process this in 30s.
    if uid:
        daemon._sendblue_seen_uids.add(uid)

    log.info("sendblue webhook hit", uid=uid, sender=sender)
    await _process_inbound(daemon, sender, sunday_number, text, media_url, "webhook", uid)
    return web.json_response({"ok": True})


# ─── inbound: polling (backup) ───────────────────────────────────────────


async def start_poller(daemon: Any) -> None:
    """Backup loop for when Sendblue stops firing webhooks.

    On boot: seed `_sendblue_seen_uids` with the current inbox so we don't
    replay history. Inbound RECEIVED messages from the last SEED_GRACE
    window stay processable so a deploy restart doesn't swallow real
    incoming texts.

    Each tick: fetch the most recent N messages, process any UID we
    haven't seen yet that's an inbound RECEIVED. The webhook handler
    also marks UIDs seen — so the typical "webhook fired first" path
    is a no-op here.
    """
    if _sendblue_headers() is None:
        log.info("sendblue poller disabled — credentials missing")
        return

    # Seed
    now = datetime.now(timezone.utc).timestamp()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(SENDBLUE_MESSAGES, headers=_sendblue_headers(), params={"limit": POLL_LIMIT})
        if res.status_code == 200:
            seeded = 0
            inbound_left = 0
            for m in _extract_messages(res.json()):
                uid = m.get("uuid") or m.get("externalId")
                if not uid:
                    continue
                is_inbound = not (m.get("isOutbound") or m.get("is_outbound"))
                status     = (m.get("status") or "").upper()
                created    = m.get("createdAt") or ""
                try:
                    created_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except (ValueError, AttributeError):
                    created_ts = 0
                if is_inbound and status == "RECEIVED" and (now - created_ts) < SEED_GRACE_SECONDS:
                    inbound_left += 1
                    continue
                daemon._sendblue_seen_uids.add(uid)
                seeded += 1
            log.info("sendblue poller seeded", seen=seeded, inbound_left_for_processing=inbound_left)
    except Exception as exc:  # noqa: BLE001
        log.warning("sendblue poller seed failed", error=str(exc))

    # Tick
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(SENDBLUE_MESSAGES, headers=_sendblue_headers(), params={"limit": POLL_LIMIT})
            if res.status_code != 200:
                log.warning("sendblue poll non-200", status=res.status_code)
                continue
            msgs = _extract_messages(res.json())

            for m in reversed(msgs):  # oldest unseen first
                uid = m.get("uuid") or m.get("externalId")
                if not uid or uid in daemon._sendblue_seen_uids:
                    continue
                daemon._sendblue_seen_uids.add(uid)

                if m.get("isOutbound") or m.get("is_outbound"):
                    continue
                if (m.get("status") or "").upper() != "RECEIVED":
                    continue

                sender_phone  = m.get("number") or ""
                sunday_phone  = m.get("from_number") or m.get("sendblueNumber") or ""
                text          = m.get("content") or ""
                media_url     = m.get("mediaUrl") or m.get("media_url")
                if not text and media_url:
                    text = f"(media: {media_url})"
                if not sender_phone or not text:
                    continue

                log.info("sendblue poll picked up message", uuid=uid, sender=sender_phone)
                await _ack_inbound(sender_phone, sunday_phone, "poll")
                await _process_inbound(daemon, sender_phone, sunday_phone, text, media_url, "poll", uid)
        except asyncio.CancelledError:
            log.info("sendblue poller cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("sendblue poll iteration failed", error=str(exc))


# ─── outbound tool (brain-callable) ──────────────────────────────────────


_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient phone number in E.164 ('+15551234567')."},
        "body": {"type": "string", "description": "Message body."},
        "media_url": {"type": "string", "description": "Optional public URL of media to attach."},
        "from_number": {"type": "string", "description": "Optional override of the Sendblue-side number."},
    },
    "required": ["to", "body"],
}


async def _t_sendblue_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    body = args.get("body")
    if not to or not body:
        return {"error": "'to' and 'body' are required"}
    return await _send_sendblue(
        str(to),
        str(body),
        args.get("media_url"),
        args.get("from_number"),
    )


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    from sunday.daemon import register_webhook, register_background_task
    # Secret-gated path is the one we expose publicly via Funnel. The bare path
    # stays for tailnet/local callers (and back-compat with VPS deployments that
    # already point Sendblue at it) but is never funneled.
    secret_path = webhook_path()
    register_webhook(secret_path, _webhook_handler)
    register_webhook("/webhooks/sendblue", _webhook_handler)
    log.info("sendblue webhook registered", secured_path_len=len(secret_path))
    register_background_task(_init_seen_uids_then_poll)

    registry.register(Tool(
        name="sendblue_send",
        description=(
            "Send an iMessage from Sunday's own Sendblue number to any phone. "
            "Auto-retries on Sendblue's transient sender-unavailable errors. "
            "Use this when Sunday is reaching out from her own number — not "
            "when replying as the user (for that, use imessage_send through a "
            "connected satellite)."
        ),
        parameters=_SEND_PARAMS,
        run=_t_sendblue_send,
    ))


async def _init_seen_uids_then_poll(daemon: Any) -> None:
    """Background-task entry. Ensures daemon._sendblue_seen_uids exists
    (the webhook handler also writes to it) before the poll loop starts."""
    if not hasattr(daemon, "_sendblue_seen_uids"):
        daemon._sendblue_seen_uids = set()
    await start_poller(daemon)
