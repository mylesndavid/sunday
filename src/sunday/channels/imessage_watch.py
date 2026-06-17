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
import time
from typing import Any

import structlog

from sunday.brain import respond
from sunday.devices import imessage_macos as im

log = structlog.get_logger("sunday.channel.imessage")

# How often to check chat.db for new rows. Local SQLite read is cheap, and this
# is the whole latency budget for "seen" — keep it tight.
WATCH_INTERVAL_SECONDS = 2.0


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


async def _process(daemon: Any, sender: str, text: str) -> None:
    """Drive the brain for one inbound text and send the reply via AppleScript."""
    t0 = time.perf_counter()
    timings: dict[str, Any] = {}
    try:
        reply = await respond(
            daemon.chat,
            text,
            "imessage_native",
            daemon.config,
            daemon.registry,
            runtime=getattr(daemon, "runtime", None),
            extras={
                "broadcast": daemon._broadcast,
                "devices": daemon.devices,
                "memory": daemon.memory,
                "runtime": getattr(daemon, "runtime", None),
                # same lean tiered-tools path the chat + sendblue channels use
                "registry": daemon.registry,
                "active_tools": daemon._active_tools,
            },
            timings=timings,
        )
    except Exception:  # noqa: BLE001
        log.exception("imessage inbound brain failed", sender=sender)
        return

    t_send = time.perf_counter()
    res = await im.send_imessage(sender, reply)
    send_ms = round((time.perf_counter() - t_send) * 1000)
    if "error" in res:
        log.warning("imessage send failed", sender=sender, error=res["error"])

    log.info(
        "imessage turn timing",
        total_ms=round((time.perf_counter() - t0) * 1000),
        send_ms=send_ms,
        llm_calls_ms=timings.get("llm_calls_ms"),
        tools=timings.get("tool_names", []),
        reply_chars=len(reply or ""),
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
                await _process(daemon, msg["sender"], msg["text"])
        except asyncio.CancelledError:
            log.info("imessage watcher cancelled")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("imessage watcher iteration failed", error=str(exc))
