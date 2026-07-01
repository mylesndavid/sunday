"""AgentMail email channel — Sunday's own email address.

The email-side mirror of `channels/sendblue.py`: Sendblue gives Sunday her
own phone *number*, AgentMail gives her own email *address* (something like
sunday@agentmail.to) with a REST API + webhooks. This is deliberately NOT
the user's Gmail — that's the Gmail-via-Nango integration ("act on the
user's mail"). AgentMail is "Sunday emailing from her own mailbox."

Inbound: webhook PRIMARY + polling BACKUP — same belt-and-suspenders rationale
Sendblue documents. Hosted webhooks drop (and the relay that will feed this
webhook isn't even built yet, see docs/relay-and-inbox-spec.md §6), so a 30s
poll of the inbox is the safety net. Both paths dedup against each other on
the provider `message_id` so a message processed by one is skipped by the
other.

Outbound: send/reply with retry on transient 5xx, mirroring Sendblue's retry
loop (email APIs flap on their sender pool too).

API facts (verified against AgentMail docs, June 2026 —
https://docs.agentmail.to/llms-full.txt and the per-endpoint .md references):
  base            https://api.agentmail.to/v0
  auth            Authorization: Bearer am_...           (key starts with am_)
  list messages   GET  /inboxes/{inbox_id}/messages?limit=
                       -> {messages:[{message_id, from, subject, preview,
                                      thread_id, timestamp, to, ...}]}
                       (list rows carry a truncated `preview`; fetch the full
                        body via GET .../messages/{message_id})
  get message     GET  /inboxes/{inbox_id}/messages/{message_id}
                       -> {message_id, from, subject, text, html, thread_id,
                           in_reply_to, timestamp, ...}
  send            POST /inboxes/{inbox_id}/messages/send
                       body {to, subject, text, html} -> {message_id, thread_id}
  reply-in-thread POST /inboxes/{inbox_id}/messages/{message_id}/reply
                       body {text, html} -> {message_id, thread_id}
  list inboxes    GET  /inboxes -> {inboxes:[{inbox_id, email, display_name}]}
  webhook event   {event_type:"message.received", message:{message_id, inbox_id,
                       from, to, subject, text, thread_id, timestamp}}

Credentials:
  AGENTMAIL_API_KEY     (required)
  AGENTMAIL_INBOX_ID    (optional — Sunday's inbox id/address; auto-discovered
                         from GET /inboxes when there's exactly one, then cached)
"""

from __future__ import annotations

import asyncio
import re
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

log = structlog.get_logger("sunday.channel.agentmail")

# TODO: verify against AgentMail docs if the API ever moves off /v0. All paths
# below hang off this base, so a version bump is a one-line change here.
AGENTMAIL_API_BASE = "https://api.agentmail.to/v0"
AGENTMAIL_INBOXES  = f"{AGENTMAIL_API_BASE}/inboxes"

# Polling cadence for the inbound backup loop — matches Sendblue's 30s so the
# Inbox feels equally live regardless of channel.
POLL_INTERVAL_SECONDS = 30
POLL_LIMIT            = 20
# How far back to look for inbound mail on first boot — protects against
# "deploy restart swallowed the message" without replaying ancient history.
# Email is less latency-sensitive than texting, so a slightly wider window
# than Sendblue's is fine; 10 min keeps parity and stays cheap.
SEED_GRACE_SECONDS    = 600

# Outbound retry config. AgentMail returns ordinary HTTP status codes (no
# embedded error_code the way Sendblue does), so we retry purely on 5xx —
# transient sender-pool / gateway blips — and give up on 4xx (our bug).
RETRY_ATTEMPTS = 4   # roughly 1+2+4+8 = 15s of backoff total


def _agentmail_headers() -> dict[str, str] | None:
    """The single gate for the whole module: every network call short-circuits
    when this returns None. Mirrors `_sendblue_headers` — no key, no channel."""
    api_key = get_credential("AGENTMAIL_API_KEY")
    if not api_key:
        return None
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


# ─── Sunday's own inbox id (required path segment on every message call) ────
# Unlike Sendblue (where the number rides on each inbound payload), AgentMail
# scopes every messages endpoint under /inboxes/{inbox_id}/... — so we need
# Sunday's inbox id before we can send, reply, list, or poll. Resolution order:
#   1. AGENTMAIL_INBOX_ID credential (explicit override, also the persisted cache)
#   2. GET /inboxes — if the account has exactly one inbox, that's Sunday's;
#      cache its id. (Per the docs the inbox id IS the address, e.g.
#      sunday@agentmail.to, so it doubles as the displayable address.)
# None only when there's no key or no inbox provisioned yet.

_default_inbox_id: str | None = None


async def discover_inbox_id() -> str | None:
    """Sunday's own AgentMail inbox id, used as the {inbox_id} path segment on
    every send/reply/list/poll call. Cached in-process + persisted; falls back
    to GET /inboxes when the account has a single inbox. None when unset."""
    global _default_inbox_id
    if _default_inbox_id:
        return _default_inbox_id
    stored = get_credential("AGENTMAIL_INBOX_ID")
    if stored:
        _default_inbox_id = stored
        return stored
    headers = _agentmail_headers()
    if headers is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.get(AGENTMAIL_INBOXES, headers=headers, params={"limit": 25})
        if res.status_code != 200:
            return None
        inboxes = _extract_inboxes(res.json())
        # Only auto-pick when it's unambiguous. With multiple inboxes we can't
        # know which one is Sunday's, so we leave it unset and let the owner set
        # AGENTMAIL_INBOX_ID explicitly rather than guess wrong.
        if len(inboxes) != 1:
            if len(inboxes) > 1:
                log.warning(
                    "agentmail multiple inboxes — set AGENTMAIL_INBOX_ID to pick Sunday's",
                    count=len(inboxes),
                )
            return None
        inbox = inboxes[0]
        chosen = inbox.get("inbox_id") or inbox.get("email")
        if not chosen:
            return None
        _default_inbox_id = chosen
        set_credential("AGENTMAIL_INBOX_ID", chosen)
        log.info("agentmail inbox discovered", inbox_id=chosen)
        return chosen
    except Exception as exc:  # noqa: BLE001
        log.warning("agentmail inbox discovery failed", error=str(exc))
        return None


def _extract_inboxes(payload: Any) -> list[dict[str, Any]]:
    """GET /inboxes returns {inboxes:[...]}; tolerate a bare list or a
    {data:{inboxes:[...]}} wrapper in case the shape ever shifts."""
    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("inboxes"), list):
        return [i for i in payload["inboxes"] if isinstance(i, dict)]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("inboxes"), list):
        return [i for i in data["inboxes"] if isinstance(i, dict)]
    return []


def _extract_messages(payload: Any) -> list[dict[str, Any]]:
    """GET .../messages returns {messages:[...], count, next_page_token}; tolerate
    a bare list or {data:{messages:[...]}} wrapper the same way Sendblue does."""
    if isinstance(payload, list):
        return [m for m in payload if isinstance(m, dict)]
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("messages"), list):
        return [m for m in payload["messages"] if isinstance(m, dict)]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("messages"), list):
        return [m for m in data["messages"] if isinstance(m, dict)]
    return []


_account_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def account_status() -> dict[str, Any]:
    """Connection facts for the desktop's Inbox -> Email panel — so saving the
    key shows a real 'connected' state (Sunday's address) instead of an empty
    form. Shape mirrors Sendblue's: {configured, connected, address, error?}.
    Cached 30s, keyed by the API key so a key change busts it immediately."""
    headers = _agentmail_headers()
    if headers is None:
        return {"configured": False, "connected": False, "address": None}
    key = headers.get("Authorization", "")
    now = time.monotonic()
    cached = _account_status_cache.get(key)
    if cached and (now - cached[0]) < 30:
        return cached[1]
    out: dict[str, Any] = {"configured": True}
    try:
        # GET /inboxes is the cheapest authenticated call that proves both
        # "key works" and "there's an inbox" — and hands us the address for free.
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(AGENTMAIL_INBOXES, headers=headers, params={"limit": 25})
        if res.status_code == 200:
            inboxes = _extract_inboxes(res.json())
            out["connected"] = True
            target = await discover_inbox_id()
            address = None
            for inbox in inboxes:
                if inbox.get("inbox_id") == target or inbox.get("email") == target:
                    address = inbox.get("email") or inbox.get("inbox_id")
                    break
            # Single-inbox accounts: address is just that inbox even pre-discovery.
            if address is None and len(inboxes) == 1:
                address = inboxes[0].get("email") or inboxes[0].get("inbox_id")
            out["address"] = address or target
        else:
            out["connected"] = False
            out["address"] = None
            out["error"] = f"key rejected (agentmail {res.status_code})"
    except Exception as exc:  # noqa: BLE001
        out["connected"] = False
        out["address"] = None
        out["error"] = f"can't reach AgentMail — {type(exc).__name__}"
    _account_status_cache[key] = (now, out)
    return out


# ─── outbound ────────────────────────────────────────────────────────────


async def _send_agentmail(
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    inbox_id: str | None = None,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    """Send a new email OR reply in-thread, with retry on transient 5xx.

    When `reply_to_message_id` is set we POST to the .../reply endpoint so the
    message threads under the original (no subject needed — AgentMail derives
    the Re: subject + References headers). Otherwise we POST .../send to compose
    a fresh email. This is the one place both outbound shapes live, exactly as
    `_send_sendblue` is the single sender for Sendblue."""
    headers = _agentmail_headers()
    if headers is None:
        return {
            "error": "AGENTMAIL_API_KEY missing. "
                     "Run: sunday credential set AGENTMAIL_API_KEY <key>"
        }

    # Every messages endpoint is scoped under an inbox id; resolve Sunday's own
    # if the caller didn't pass one (brain-initiated compose, poller reply).
    if inbox_id is None:
        inbox_id = await discover_inbox_id()
    if not inbox_id:
        return {
            "error": "no AgentMail inbox to send from — set AGENTMAIL_INBOX_ID "
                     "(or provision a single inbox so it can be auto-discovered)"
        }

    if reply_to_message_id:
        url = f"{AGENTMAIL_INBOXES}/{inbox_id}/messages/{reply_to_message_id}/reply"
        payload: dict[str, Any] = {"text": text}
    else:
        url = f"{AGENTMAIL_INBOXES}/{inbox_id}/messages/send"
        payload = {"to": to, "subject": subject, "text": text}
    if html:
        payload["html"] = html

    backoff = 1.0
    last_err: dict[str, Any] = {}
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, headers=headers, json=payload)
        try:
            data = res.json()
        except ValueError:
            data = {"raw": res.text}

        if res.status_code < 400:
            return {
                "ok": True,
                "to": to,
                "message_id": data.get("message_id") if isinstance(data, dict) else None,
                "thread_id": data.get("thread_id") if isinstance(data, dict) else None,
                "data": data,
            }

        # Retry on transient 5xx (gateway/sender blip); give up on 4xx (our bug).
        if 500 <= res.status_code < 600 and attempt < RETRY_ATTEMPTS:
            last_err = {"http": res.status_code, "attempt": attempt}
            log.warning("agentmail 5xx, retrying", status=res.status_code, attempt=attempt)
            await asyncio.sleep(backoff)
            backoff *= 2
            continue
        return {"error": f"agentmail {res.status_code}: {data}"}

    return {"error": "agentmail retries exhausted", **last_err}


# ─── inbound: shared brain drive ─────────────────────────────────────────


async def _process_inbound(
    daemon: Any,
    inbox_id: str,
    sender: str,
    subject: str,
    text: str,
    source: str,
    message_id: str | None = None,
    thread_id: str | None = None,
) -> None:
    """Drive the brain pipeline + send the reply. Shared by webhook + poll.

    Every phase is timed and logged ("agentmail turn timing") so we can see
    where wall-clock goes: the agent loop (broken down further by respond()'s
    own "turn timing") and the outbound reply.

    The brain wiring — channel tag, extras dict — is copied beat-for-beat from
    sendblue's `_process_inbound`: same broadcast/devices/memory/runtime, same
    tiered-tools (lean core + find_tools) sharing the daemon's session-wide
    active set with the desktop chat, so email doesn't pay for the full schema
    every turn either. The only differences are the channel tag prefix
    ("email_agentmail:") and that the reply goes out via AgentMail's reply
    endpoint instead of Sendblue's send."""
    t0 = time.perf_counter()

    # Record the inbound email into the activity store so it shows in the Inbox,
    # then live-refresh any open Inbox. The provider message_id is the row id so
    # the webhook+poll paths dedup (append is idempotent) and a later resync
    # never double-inserts. Wrapped so a store hiccup never breaks the reply.
    try:
        act = getattr(daemon, "activity", None)
        if act is not None:
            from datetime import datetime as _dt, timezone as _tz
            await act.append({
                "id": message_id,
                "channel": "email",
                "direction": "in",
                "peer": sender,
                "ts": _dt.now(_tz.utc).isoformat(),
                "preview": str(subject or text)[:120],
                "status": "received",
                "thread_id": thread_id,
                "provider_id": message_id,
                "raw_json": {"subject": subject, "text": text, "from": sender, "direction": "in"},
            })
        if hasattr(daemon, "_broadcast"):
            await daemon._broadcast({"type": "inbox", "channel": "email"})
    except Exception:  # noqa: BLE001
        log.warning("agentmail inbound activity record failed", message_id=message_id)

    # Give the brain the email context up front. Sendblue passes bare text; email
    # has a subject + sender that materially change the reply, so we prefix them
    # into the user turn (the brain has no other place to learn them).
    framed = text
    if subject or sender:
        header_lines = []
        if sender:
            header_lines.append(f"From: {sender}")
        if subject:
            header_lines.append(f"Subject: {subject}")
        framed = "\n".join(header_lines) + "\n\n" + text

    timings: dict[str, Any] = {}
    t_respond = time.perf_counter()
    try:
        reply = await respond(
            daemon.chat,
            framed,
            f"email_agentmail:{source}",
            daemon.config,
            daemon.registry,
            runtime=getattr(daemon, "runtime", None),
            extras={"broadcast": daemon._broadcast, "devices": daemon.devices,
                    "memory": daemon.memory, "runtime": getattr(daemon, "runtime", None),
                    # the activity store, so the outbound email tool records sends.
                    "activity": daemon.activity,
                    # The canonical chat, so notify_user can post ONE message to
                    # the MAIN timeline when Sunday needs the user mid-email.
                    # (Email turns are otherwise hidden from the main chat UI.)
                    "chat": daemon.chat,
                    # Tiered tools, same as the desktop chat + Sendblue paths:
                    # send the lean core instead of the full schema and let
                    # find_tools surface the rest on demand. Shares the daemon's
                    # session-wide active set with the chat UI.
                    "registry": daemon.registry,
                    "active_tools": daemon._active_tools},
            timings=timings,
        )
    except Exception:  # noqa: BLE001
        log.exception("inbound brain failed", source=source, message_id=message_id)
        return
    respond_ms = round((time.perf_counter() - t_respond) * 1000)

    t_send = time.perf_counter()
    # Reply in-thread when we have the originating message id (the common case,
    # both webhook + poll carry it) so the conversation stays one thread; fall
    # back to a fresh send (with a Re: subject) only if we somehow lack it.
    if message_id:
        await _send_agentmail(
            sender, subject, reply, inbox_id=inbox_id, reply_to_message_id=message_id,
        )
    else:
        re_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()
        await _send_agentmail(sender, re_subject or "Re:", reply, inbox_id=inbox_id)
    send_ms = round((time.perf_counter() - t_send) * 1000)

    log.info(
        "agentmail turn timing",
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


# ─── inbound: webhook (primary) ──────────────────────────────────────────


async def _webhook_handler(request: web.Request, daemon: Any) -> web.Response:
    """AgentMail inbound webhook — registered at /webhooks/agentmail.

    This is the future PRIMARY path: once the relay (docs spec §6) is live,
    AgentMail will POST to https://relay.sunday.xyz/u/<agent-id>/agentmail,
    the relay loopback-delivers to this handler, and the poller demotes to
    backup. Until then the poller carries inbound and this stays ready for the
    relay to start feeding it — same dual-path design Sendblue ships today."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Only act on inbound mail. AgentMail also fires delivery/bounce/domain
    # events through the same webhook; we want the receive event only. Tolerate
    # the spam/blocked/unauthenticated variants by prefix-matching.
    event_type = (body.get("event_type") or body.get("type") or "")
    if event_type and not event_type.startswith("message.received"):
        return web.json_response({"ok": True, "skipped": f"event={event_type}"})

    # The message object is nested under "message"; tolerate a flat payload too.
    msg = body.get("message") if isinstance(body.get("message"), dict) else body
    message_id = msg.get("message_id") or msg.get("id")
    inbox_id   = msg.get("inbox_id") or body.get("inbox_id")
    sender     = msg.get("from") or ""
    subject    = msg.get("subject") or ""
    text       = msg.get("text") or msg.get("preview") or ""
    thread_id  = msg.get("thread_id")

    if not sender or not (text or subject):
        return web.json_response({"ok": True, "skipped": "missing sender/content"})

    # Dedup against the poller. message_id == the poll's message_id, so whichever
    # path sees the message first marks it seen and the other skips. The webhook
    # MUST check too: the 30s poll can win the race, and without this the webhook
    # would re-run the brain and send a duplicate reply.
    if message_id and message_id in daemon._agentmail_seen_ids:
        return web.json_response({"ok": True, "skipped": "already processed"})
    if message_id:
        daemon._agentmail_seen_ids.add(message_id)

    # The webhook preview can be truncated; if we have the ids, fetch the full
    # body so the brain reasons over the whole email rather than a snippet.
    if inbox_id and message_id:
        full = await _fetch_message(inbox_id, message_id)
        if full:
            sender  = full.get("from") or sender
            subject = full.get("subject") or subject
            text    = full.get("text") or text

    log.info("agentmail webhook hit", message_id=message_id, sender=sender)
    await _process_inbound(
        daemon, inbox_id or await discover_inbox_id() or "", sender, subject,
        text, "webhook", message_id, thread_id,
    )
    return web.json_response({"ok": True})


# ── body cleanup for display ───────────────────────────────────────────────
# An email body carries the whole reply chain (the previous messages re-quoted)
# plus signatures and legal boilerplate. In a threaded Inbox view every prior
# message is already its own bubble, so that quoted history is pure noise. We
# trim, for DISPLAY only, everything from the first reply-attribution / quote /
# signature / legal marker onward — leaving just this message's own new text.

# Ordered so the earliest marker in the body wins the cut.
_QUOTE_END = re.compile(r"wrote:\s*$", re.IGNORECASE)          # "On <date> … wrote:"
_ORIG_MSG  = re.compile(r"^-{2,}\s*original message", re.IGNORECASE)
_AGENTMAIL_FOOTER = re.compile(r"^\s*sent via agentmail", re.IGNORECASE)
_LEGAL = re.compile(
    r"confidentiality (statement|notice)"
    r"|for the sole use of the intended recipient"
    r"|unauthorized (use|review|disclosure)"
    r"|cancellation/rescheduling policy",
    re.IGNORECASE,
)
_ATTRIB_LINE = re.compile(r"^\s*On\b.{0,220}$")               # trailing "On <date>…" line


def clean_email_body(text: str | None) -> str:
    """Strip quoted reply history, the AgentMail footer, and legal boilerplate
    from an email body so the Inbox shows only the message itself. Conservative:
    if trimming would leave nothing, the original is returned unchanged."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    cut = len(lines)
    for i, raw in enumerate(lines):
        s = raw.strip()
        if (
            s.startswith(">")
            or _QUOTE_END.search(s)
            or _ORIG_MSG.match(s)
            or _AGENTMAIL_FOOTER.match(s)
            or _LEGAL.search(s)
        ):
            cut = i
            break
    kept = lines[:cut]
    # Drop a dangling "On <date> … " attribution line that preceded "wrote:".
    while kept and _ATTRIB_LINE.match(kept[-1]) and len(kept[-1].strip()) < 220:
        kept.pop()
    body = "\n".join(kept).strip()
    # Cut a standard "-- " signature delimiter block if present.
    body = re.split(r"\n-- ?\n", body)[0].strip()
    # Collapse runs of blank lines.
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body or text.strip()


async def _fetch_message(inbox_id: str, message_id: str) -> dict[str, Any] | None:
    """GET a single message's full body (list rows + webhook payloads only carry
    a truncated `preview`/`text`). Best-effort: None on any failure, callers fall
    back to whatever snippet they already had."""
    headers = _agentmail_headers()
    if headers is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            res = await client.get(
                f"{AGENTMAIL_INBOXES}/{inbox_id}/messages/{message_id}", headers=headers,
            )
        if res.status_code != 200:
            return None
        data = res.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        log.warning("agentmail message fetch failed", error=str(exc), message_id=message_id)
        return None


# ─── inbound: polling (backup) ───────────────────────────────────────────


def _is_inbound(msg: dict[str, Any], own_address: str | None) -> bool:
    """True when this message was RECEIVED by Sunday (not one she sent).

    AgentMail's list returns both directions in one feed. There's no single
    canonical direction flag across versions, so we infer: an explicit
    label/direction wins; otherwise a message whose `from` is Sunday's own
    address is outbound, anything else is inbound."""
    labels = msg.get("labels")
    if isinstance(labels, list):
        lowered = {str(l).lower() for l in labels}
        if "sent" in lowered or "outbound" in lowered:
            return False
        if "received" in lowered or "inbound" in lowered:
            return True
    direction = (msg.get("direction") or "").lower()
    if direction in ("inbound", "received"):
        return True
    if direction in ("outbound", "sent"):
        return False
    sender = (msg.get("from") or "").lower()
    if own_address and own_address.lower() in sender:
        return False
    return True


def _msg_ts(msg: dict[str, Any]) -> float:
    """Parse a message timestamp to epoch seconds; 0 when absent/unparseable."""
    raw = msg.get("timestamp") or msg.get("createdAt") or msg.get("created_at") or ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _msg_to_event(m: dict[str, Any], own_address: str | None) -> dict[str, Any] | None:
    """Map one AgentMail message → an activity-store event. Both directions: a
    message Sunday SENT (peer = the recipient) and one she RECEIVED (peer = the
    sender) both belong in the Inbox, so it mirrors the account."""
    mid = m.get("message_id") or m.get("id")
    if not mid:
        return None
    inbound = _is_inbound(m, own_address)
    sender = str(m.get("from") or "")
    to = m.get("to")
    to = ", ".join(str(t) for t in to) if isinstance(to, list) else str(to or "")
    subject = str(m.get("subject") or "")
    snippet = str(m.get("preview") or m.get("snippet") or m.get("text") or "")
    raw_ts = m.get("timestamp") or m.get("createdAt") or m.get("created_at")
    return {
        "id": str(mid),
        "channel": "email",
        "direction": "in" if inbound else "out",
        "peer": (sender if inbound else to) or "?",
        "ts": str(raw_ts) if raw_ts else None,
        "preview": (subject or snippet)[:140],
        "status": "received" if inbound else "sent",
        "thread_id": m.get("thread_id"),
        "provider_id": str(mid),
        "raw_json": {"subject": subject, "preview": snippet, "from": sender, "to": to,
                     "direction": "in" if inbound else "out"},
    }


async def _sync_messages_to_store(daemon: Any, messages: list[dict[str, Any]], own_address: str | None) -> int:
    """Mirror AgentMail's message list (SENT + RECEIVED) into the local activity
    store so the Inbox reflects what's actually in the account — not just what we
    happened to catch at send/receive time. upsert() refreshes a row in place, so
    re-syncing the same window every poll is cheap and idempotent."""
    act = getattr(daemon, "activity", None)
    if act is None:
        return 0
    n = 0
    for m in messages:
        ev = _msg_to_event(m, own_address)
        if ev is None:
            continue
        try:
            await act.upsert(ev)
            n += 1
        except Exception:  # noqa: BLE001 — one bad row shouldn't sink the sweep
            log.exception("agentmail store sync row failed", message_id=ev.get("id"))
    return n


async def start_poller(daemon: Any) -> None:
    """Backup loop for when AgentMail's webhook isn't reaching us (no relay yet,
    or a dropped hosted delivery).

    On boot: seed `_agentmail_seen_ids` with the current inbox so we don't
    replay history. Inbound messages from the last SEED_GRACE window stay
    processable so a deploy restart doesn't swallow real incoming mail.

    Each tick: fetch the most recent N messages, process any message_id we
    haven't seen yet that's inbound. The webhook handler also marks ids seen —
    so the typical "webhook fired first" path is a no-op here.
    """
    if _agentmail_headers() is None:
        log.info("agentmail poller disabled — credentials missing")
        return

    inbox_id = await discover_inbox_id()
    if not inbox_id:
        log.info("agentmail poller disabled — no inbox id (set AGENTMAIL_INBOX_ID)")
        return
    messages_url = f"{AGENTMAIL_INBOXES}/{inbox_id}/messages"
    own_address = inbox_id  # inbox id is the address per AgentMail's model

    # Seed
    now = datetime.now(timezone.utc).timestamp()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.get(messages_url, headers=_agentmail_headers(), params={"limit": POLL_LIMIT})
        if res.status_code == 200:
            seed_msgs = _extract_messages(res.json())
            # Mirror the full message list (sent + received) into the activity
            # store up front, so the Inbox shows the account's real history the
            # moment the daemon starts — not just messages that arrive later.
            await _sync_messages_to_store(daemon, seed_msgs, own_address)
            seeded = 0
            inbound_left = 0
            for m in seed_msgs:
                mid = m.get("message_id") or m.get("id")
                if not mid:
                    continue
                if _is_inbound(m, own_address) and (now - _msg_ts(m)) < SEED_GRACE_SECONDS:
                    inbound_left += 1
                    continue
                daemon._agentmail_seen_ids.add(mid)
                seeded += 1
            log.info("agentmail poller seeded", seen=seeded, inbound_left_for_processing=inbound_left)
    except Exception as exc:  # noqa: BLE001
        log.warning("agentmail poller seed failed", error=str(exc))

    # Tick
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get(messages_url, headers=_agentmail_headers(), params={"limit": POLL_LIMIT})
            if res.status_code != 200:
                log.warning("agentmail poll non-200", status=res.status_code)
                continue
            msgs = _extract_messages(res.json())

            # Mirror every message (both directions) into the activity store so
            # the Inbox reflects AgentMail itself, then nudge any open Inbox.
            if await _sync_messages_to_store(daemon, msgs, own_address) and hasattr(daemon, "_broadcast"):
                try:
                    await daemon._broadcast({"type": "inbox", "channel": "email"})
                except Exception:  # noqa: BLE001
                    pass

            for m in reversed(msgs):  # oldest unseen first
                mid = m.get("message_id") or m.get("id")
                if not mid or mid in daemon._agentmail_seen_ids:
                    continue
                daemon._agentmail_seen_ids.add(mid)

                if not _is_inbound(m, own_address):
                    continue

                sender    = m.get("from") or ""
                subject   = m.get("subject") or ""
                text      = m.get("text") or m.get("preview") or ""
                thread_id = m.get("thread_id")
                # List rows carry only a truncated preview — fetch the full body
                # so the brain reads the whole email, not a snippet.
                full = await _fetch_message(inbox_id, mid)
                if full:
                    sender  = full.get("from") or sender
                    subject = full.get("subject") or subject
                    text    = full.get("text") or text
                if not sender or not (text or subject):
                    continue

                log.info("agentmail poll picked up message", message_id=mid, sender=sender)
                await _process_inbound(daemon, inbox_id, sender, subject, text, "poll", mid, thread_id)
        except asyncio.CancelledError:
            log.info("agentmail poller cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("agentmail poll iteration failed", error=str(exc))


# ─── outbound tool (brain-callable) ──────────────────────────────────────


_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient email address."},
        "subject": {"type": "string", "description": "Subject line (ignored when replying in a thread)."},
        "text": {"type": "string", "description": "Plain-text body of the email."},
        "html": {"type": "string", "description": "Optional HTML body for a rich, styled version."},
        "reply_to_message_id": {
            "type": "string",
            "description": (
                "Optional AgentMail message_id to reply to. When set, the email "
                "threads under that message (Re:/References handled by AgentMail) "
                "and `subject` is ignored."
            ),
        },
    },
    "required": ["to", "text"],
}


async def _t_agentmail_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    text = args.get("text")
    if not to or not text:
        return {"error": "'to' and 'text' are required"}
    reply_id = args.get("reply_to_message_id")
    # Subject only matters for a fresh compose; default it so a brain that
    # forgets a subject still sends a sane email rather than an empty one.
    subject = args.get("subject") or ("" if reply_id else "(no subject)")
    result = await _send_agentmail(
        str(to),
        str(subject),
        str(text),
        html=args.get("html"),
        reply_to_message_id=str(reply_id) if reply_id else None,
    )

    # Record the sent email into the activity store so it shows in the Inbox,
    # then live-refresh any open Inbox. Best-effort: a store hiccup or a missing
    # store must never turn a successful send into a tool error.
    if isinstance(result, dict) and result.get("ok"):
        act = ctx.extras.get("activity")
        if act is not None:
            try:
                import uuid as _uuid
                from datetime import datetime as _dt, timezone as _tz
                mid = result.get("message_id") or _uuid.uuid4().hex
                await act.append({
                    "id": mid,
                    "channel": "email",
                    "direction": "out",
                    "peer": str(to),
                    "ts": _dt.now(_tz.utc).isoformat(),
                    "preview": str(subject or text)[:120],
                    "status": "sent",
                    "thread_id": result.get("thread_id"),
                    "provider_id": mid,
                    "raw_json": {"subject": subject, "text": text, "to": to, "direction": "out"},
                })
            except Exception:  # noqa: BLE001
                log.warning("agentmail outbound activity record failed", to=str(to))
        bc = ctx.extras.get("broadcast")
        if bc:
            try:
                await bc({"type": "inbox", "channel": "email"})
            except Exception:  # noqa: BLE001
                pass
    return result


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    from sunday.daemon import register_webhook, register_background_task
    # The relay (docs spec §6) will loopback-deliver AgentMail's webhook here.
    # Registered now so it's ready the moment the relay turns on; until then the
    # poller carries inbound. No secret-gated variant (unlike Sendblue's Funnel
    # path): the relay authenticates at its own edge, so the bare path is the
    # one and only inbound webhook route.
    register_webhook("/webhooks/agentmail", _webhook_handler)
    log.info("agentmail webhook registered")
    register_background_task(_init_seen_ids_then_poll)

    registry.register(Tool(
        name="agentmail_send",
        description=(
            "Send an email from Sunday's own AgentMail address. Compose a new "
            "email (give to/subject/text) or reply in-thread (pass "
            "reply_to_message_id; subject is then inherited). Auto-retries on "
            "transient AgentMail 5xx errors. Use this when Sunday is emailing "
            "from HER OWN address — NOT when acting on the user's Gmail (for "
            "that, use the Gmail tools).\n"
            "CRITICAL: `text` is the LITERAL email the recipient reads — write "
            "ONLY the email itself, signed off, ready to send. NEVER put your "
            "reasoning, recommendations, caveats, or notes to the user in it "
            "(no 'take the 3pm', no 'send this:', no 'don't gamble on…'). Those "
            "go in your chat with the user, never in the body. A stranger or a "
            "business reads this — it must contain nothing but the message to "
            "them."
        ),
        parameters=_SEND_PARAMS,
        run=_t_agentmail_send,
    ))


async def _init_seen_ids_then_poll(daemon: Any) -> None:
    """Background-task entry. Ensures daemon._agentmail_seen_ids exists (the
    webhook handler also writes to it) before the poll loop starts — same
    init-then-loop shape as Sendblue's `_init_seen_uids_then_poll`."""
    if not hasattr(daemon, "_agentmail_seen_ids"):
        daemon._agentmail_seen_ids = set()
    await start_poller(daemon)
