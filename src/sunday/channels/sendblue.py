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
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.sendblue")

SENDBLUE_API_BASE = "https://api.sendblue.com/api"
SENDBLUE_SEND     = f"{SENDBLUE_API_BASE}/send-message"
SENDBLUE_TYPING   = f"{SENDBLUE_API_BASE}/send-typing-indicator"
SENDBLUE_MESSAGES = "https://api.sendblue.com/accounts/messages"

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
    payload: dict[str, Any] = {"number": to}
    if from_number:
        payload["from_number"] = from_number
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(SENDBLUE_TYPING, headers=headers, json=payload)
    if res.status_code >= 400:
        log.warning("sendblue typing failed", status=res.status_code, body=res.text[:200])
        return {"error": f"sendblue typing {res.status_code}"}
    return {"ok": True}


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
    outbound send."""
    t0 = time.perf_counter()

    # Typing indicator: fire-and-forget, but time its own round-trip — it's the
    # first signal the user sees ("read"/typing), so if it's slow that's felt.
    async def _typing_timed() -> None:
        _t = time.perf_counter()
        await _send_typing(sender, from_number=sunday_number or None)
        log.info("sendblue typing sent", ms=round((time.perf_counter() - _t) * 1000), source=source)
    asyncio.create_task(_typing_timed())

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
                    "memory": daemon.memory, "runtime": getattr(daemon, "runtime", None)},
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
    register_webhook("/webhooks/sendblue", _webhook_handler)
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
