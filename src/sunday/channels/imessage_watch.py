"""Native iMessage inbound watcher — the push side of the local channel.

The brain already has pull-style iMessage tools (read/search/send via
`devices.imessage_macos`, which reads ~/Library/Messages/chat.db and sends
through Messages.app over AppleScript). What was missing for a real *channel*
is the push: notice a new inbound message the instant it lands and drive the
brain — the local equivalent of the Sendblue poller, minus the third-party
send queue.

Intended topology (single-owner Mac mini): a dedicated macOS user signed into
a "Sunday" Apple ID runs this watcher against ITS OWN chat.db. Inbound texts to
that Apple ID fire the brain; replies go out via AppleScript instantly (no
Sendblue, no free-tier queue). The user's personal iMessages stay readable
separately through their own account's chat.db. Each account only ever touches
its own files — no cross-user reads (macOS blocks those anyway).

Enable with config flag `channels.imessage_native` (default off) so it never
double-answers alongside Sendblue until you cut over.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.devices import imessage_macos as im
from sunday.tools import ToolRegistry

log = structlog.get_logger("sunday.channel.imessage")


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    """Channel entry point (called by tools.py at daemon boot). Registers the
    inbound watcher as a background task only when the native channel is on —
    otherwise this module is inert, so it never double-answers with Sendblue."""
    if not getattr(config, "imessage_native", False):
        return
    from sunday.daemon import register_background_task
    register_background_task(start_imessage_watcher)
    log.info("native imessage channel enabled — watching local chat.db")

# How often to check chat.db for new rows. Local SQLite read is cheap, and this
# is the whole latency budget for "seen" — keep it tight.
WATCH_INTERVAL_SECONDS = 2.0

# Replies go out as a few natural texts, not one wall. The brain is told to
# separate distinct beats with a blank line (brain._TEXTING_STYLE); we split on
# those blank lines into separate bubbles, with a short human-cadence gap
# between sends. Cap the count so a runaway reply can't spray a dozen texts.
MAX_BUBBLES = 4
BUBBLE_GAP_SECONDS = 0.6

# A turn can finish with no text (the model did its tool work but produced an
# empty final message — seen on a long Slack/browser task). Silence reads as a
# crash to the user, so we send this instead. Never leave a text unanswered.
_EMPTY_REPLY_FALLBACK = (
    "hmm — I worked through that but didn't come back with anything to send. "
    "want me to keep going, or take a different angle?"
)


def _strip_markdown(text: str) -> str:
    """Belt-and-suspenders for when the model formats anyway: kill the markdown
    artifacts that look worst as literal characters in a bubble. Conservative —
    leaves single `*`, math, and ordinary punctuation alone so we never mangle
    real text like "2*3" or "the *only* one"."""
    out = []
    for line in text.split("\n"):
        s = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)   # # headers
        s = re.sub(r"^\s*[-*+]\s+", "", s)            # - bullet markers
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)        # **bold**
        s = re.sub(r"__(.+?)__", r"\1", s)            # __bold__
        s = re.sub(r"`([^`]+)`", r"\1", s)            # `code`
        out.append(s)
    return "\n".join(out)


def split_into_bubbles(reply: str) -> list[str]:
    """Split a reply into separate iMessage bubbles on blank lines. Returns at
    most MAX_BUBBLES; overflow is merged back into the last bubble so nothing is
    ever dropped. Empty/whitespace input → no bubbles (nothing to send)."""
    text = _strip_markdown(reply or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) > MAX_BUBBLES:
        head, tail = parts[: MAX_BUBBLES - 1], parts[MAX_BUBBLES - 1 :]
        parts = head + ["\n\n".join(tail)]
    return parts


def _new_inbound_since(since_rowid: int, limit: int = 25) -> list[dict[str, Any]]:
    """Inbound (is_from_me = 0) messages with ROWID > since_rowid, oldest first.

    ROWID is monotonic in chat.db, so it's a clean high-water mark — no clock
    skew, no replay. Returns dicts with rowid / sender handle / text."""
    conn = im._connect_read_only()
    try:
        cur = conn.execute(
            """
            SELECT m.ROWID, m.text, m.attributedBody, h.id, m.date
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            WHERE m.ROWID > ? AND m.is_from_me = 0
            ORDER BY m.ROWID ASC
            LIMIT ?
            """,
            (since_rowid, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for rowid, text, attributed, handle, date in rows:
        body = im._clean_text(text) or im._decode_attributed_body(attributed)
        out.append({
            "rowid": rowid,
            "sender": handle or "",
            "text": (body or "").strip(),
        })
    return out


def _max_rowid() -> int:
    conn = im._connect_read_only()
    try:
        row = conn.execute("SELECT MAX(ROWID) FROM message").fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


async def _dispatch(daemon: Any, sender: str, text: str) -> None:
    """Route one inbound text through the daemon's turn machinery, then send the
    reply as iMessage bubbles.

    Going through `daemon._say` (rather than calling the brain directly) means
    texting gets the same turn-lock, memory extraction, compaction, and app
    broadcast the chat surface does — AND "by the way" steering for free: if a
    turn is already running, `_say` folds this text into it as a mid-turn nudge
    and returns steered=True, so we send nothing here and the in-flight turn's
    reply (which now accounts for it) is what goes out. Single-owner assumption:
    one person texts this handle, so a follow-up is always a steer for the same
    conversation, never a different sender bleeding in."""
    t0 = time.perf_counter()
    # Real-app read receipt + 'typing…', flag-gated and best-effort. Fire it the
    # instant we see the text so the indicator shows while the brain works; it's
    # a no-op unless Sunday is the foreground session (see imessage_macos).
    indicators = bool(getattr(getattr(daemon, "config", None), "imessage_indicators", False))
    if indicators:
        asyncio.create_task(im.ack_inbound_gui(sender))
    try:
        result = await daemon._say(text, "imessage_native")
    except Exception:  # noqa: BLE001
        log.exception("imessage inbound turn failed", sender=sender)
        return

    if result.get("steered"):
        log.info("imessage folded in as 'by the way'", sender=sender)
        return

    reply = result.get("reply") or ""
    bubbles = split_into_bubbles(reply)
    empty = not bubbles
    if empty:
        log.warning("imessage empty reply — sending fallback", sender=sender)
        bubbles = [_EMPTY_REPLY_FALLBACK]
    if indicators:
        # wipe the typing placeholder before sending the real reply headless
        await im.clear_compose_gui(sender)
    t_send = time.perf_counter()
    for i, bubble in enumerate(bubbles):
        if i:
            await asyncio.sleep(BUBBLE_GAP_SECONDS)  # human cadence between texts
        res = await im.send_imessage(sender, bubble)
        if "error" in res:
            log.warning("imessage send failed", sender=sender, error=res["error"], bubble=i)

    log.info(
        "imessage reply sent",
        sender=sender,
        bubbles=len(bubbles),
        empty_fallback=empty,
        send_ms=round((time.perf_counter() - t_send) * 1000),
        total_ms=round((time.perf_counter() - t0) * 1000),
        reply_chars=len(reply),
    )


async def start_imessage_watcher(daemon: Any) -> None:
    """Poll the local chat.db for new inbound and drive the brain.

    Seeds the high-water mark at the current MAX(ROWID) so a restart never
    replays history."""
    if not im.is_available():
        log.info("imessage watcher disabled — chat.db not readable "
                 "(needs macOS + Full Disk Access)")
        return

    try:
        last_rowid = _max_rowid()
    except Exception as exc:  # noqa: BLE001
        log.warning("imessage watcher seed failed", error=str(exc))
        return
    log.info("imessage watcher started", seed_rowid=last_rowid)

    while True:
        try:
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            new = _new_inbound_since(last_rowid)
            for msg in new:
                last_rowid = max(last_rowid, msg["rowid"])
                if not msg["sender"] or not msg["text"]:
                    continue
                log.info("imessage inbound", sender=msg["sender"], rowid=msg["rowid"])
                # Dispatch as a task, don't await it: the poll loop has to keep
                # watching chat.db while a turn runs, so a follow-up text can be
                # seen and folded in as a "by the way" steer instead of queuing
                # behind the turn. _say serializes the turns via the daemon lock.
                asyncio.create_task(_dispatch(daemon, msg["sender"], msg["text"]))
        except asyncio.CancelledError:
            log.info("imessage watcher cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("imessage watcher iteration failed", error=str(exc))
