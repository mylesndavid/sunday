"""Sunday's local activity store — the unified Inbox's source of truth.

Today every Inbox-style read is LIVE retrieval: the Calls view hits VAPI on
every open (`daemon._http_vapi_calls`), and adding Sendblue + AgentMail naively
means three provider round-trips per tab open. The relay's proxy push makes a
better architecture available (spec §4b): inbound events land HERE first, the
Inbox reads HERE, and provider live-fetch demotes to backfill/reconcile.

This module is the one new local table that makes "replace live retrieval"
real. One normalized event shape across channels:

    channel    voice | text | email | webhook
    direction  in | out
    peer       the other party (number / address / name)
    ts         iso8601
    preview    a short snippet for the list row
    status     provider-specific status string
    thread_id  groups a conversation
    provider_id the upstream id (VAPI call id, Sendblue uuid, …)
    raw_json    the full original event, for detail views

Async-safe: every write/read runs the blocking sqlite call on a thread
(`asyncio.to_thread`) so it never stalls the event loop, and a lock serializes
writers. Idempotent on `id` — the same event arriving via push AND poll (the
belt-and-suspenders §1 pattern) is stored once.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.activity")

# The columns the Inbox list row carries (everything except the heavy raw_json).
# Kept as one string so the SELECT and the row→dict mapping can't drift apart.
_LIST_COLS = "id, channel, direction, peer, ts, preview, status, thread_id, provider_id, read"

# Channels we accept. Anything else is coerced to "webhook" — the catch-all,
# matching the relay's "blessed ≠ exclusive" stance (spec §0).
_CHANNELS = ("voice", "text", "email", "webhook")


class ActivityStore:
    """SQLite-backed normalized event log. One table, `activity`.

    The connection is opened with check_same_thread=False because the actual
    sqlite calls hop onto worker threads via asyncio.to_thread; a single
    asyncio.Lock serializes writers so the shared connection is never touched
    concurrently."""

    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "activity.db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS activity(
            id          TEXT PRIMARY KEY,   -- provider_id or a minted uuid
            channel     TEXT NOT NULL,      -- voice | text | email | webhook
            direction   TEXT,              -- in | out
            peer        TEXT,              -- number / address / name
            ts          TEXT,              -- iso8601
            preview     TEXT,              -- short list-row text
            status      TEXT,
            thread_id   TEXT,
            provider_id TEXT,
            raw_json    TEXT,              -- full original event
            read        INTEGER NOT NULL DEFAULT 0,  -- 1 once the USER opens it
            created_at  TEXT NOT NULL      -- when WE stored it
        );
        CREATE INDEX IF NOT EXISTS idx_activity_channel_ts ON activity(channel, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
        """)
        self._conn.commit()
        # Migrate DBs created before the read column existed. ALTER errors with
        # "duplicate column" once it's there — harmless, so swallow it.
        try:
            self._conn.execute("ALTER TABLE activity ADD COLUMN read INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()
        except sqlite3.OperationalError:
            pass
        # Serializes writers on the one shared connection (reads are quick
        # point/range queries that we still hop to a thread, but don't contend).
        self._write_lock = asyncio.Lock()

    # ── write side ────────────────────────────────────────────────────────

    async def append(self, event: dict[str, Any]) -> str:
        """Store one normalized event. Idempotent on `id`.

        `event` may carry: id, channel, direction, peer, ts, preview, status,
        thread_id, provider_id, and anything else (stashed in raw_json). When
        `id` is absent we fall back to provider_id, else mint a uuid. Re-storing
        the same id is a no-op (INSERT OR IGNORE) — so push + poll seeing the
        same message both call append() and it lands exactly once.

        Returns the row id that was used."""
        provider_id = event.get("provider_id")
        row_id = str(event.get("id") or provider_id or uuid.uuid4().hex)
        channel = str(event.get("channel") or "webhook")
        if channel not in _CHANNELS:
            channel = "webhook"
        ts = str(event.get("ts") or datetime.now(timezone.utc).isoformat())
        now = datetime.now(timezone.utc).isoformat()
        # raw_json is the whole event minus the broken-out columns, so detail
        # views get the original payload back without duplicating the indexed
        # fields. Tolerate non-serializable values rather than dropping the row.
        try:
            raw_json = event.get("raw_json")
            if raw_json is None:
                raw_json = json.dumps(event, default=str)
            elif not isinstance(raw_json, str):
                raw_json = json.dumps(raw_json, default=str)
        except (TypeError, ValueError):
            raw_json = "{}"
        preview = event.get("preview")
        if preview:
            preview = str(preview)[:280]

        def _insert() -> None:
            self._conn.execute(
                "INSERT OR IGNORE INTO activity("
                "id, channel, direction, peer, ts, preview, status, thread_id, "
                "provider_id, raw_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row_id, channel, event.get("direction"), event.get("peer"),
                    ts, preview, event.get("status"), event.get("thread_id"),
                    provider_id, raw_json, now,
                ),
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_insert)
        return row_id

    async def upsert(self, event: dict[str, Any]) -> str:
        """Like append(), but the provider is AUTHORITATIVE: if `id` already
        exists, refresh the mutable fields. Used by the VAPI sync — a call first
        synced mid-flight must flip to 'completed' on a later poll, which
        append()'s INSERT OR IGNORE would never do. `created_at` (our
        first-seen time) is preserved. Channels where first-seen wins (push+poll
        dedup) keep using append()."""
        provider_id = event.get("provider_id")
        row_id = str(event.get("id") or provider_id or uuid.uuid4().hex)
        channel = str(event.get("channel") or "webhook")
        if channel not in _CHANNELS:
            channel = "webhook"
        ts = str(event.get("ts") or datetime.now(timezone.utc).isoformat())
        now = datetime.now(timezone.utc).isoformat()
        try:
            raw_json = event.get("raw_json")
            if raw_json is None:
                raw_json = json.dumps(event, default=str)
            elif not isinstance(raw_json, str):
                raw_json = json.dumps(raw_json, default=str)
        except (TypeError, ValueError):
            raw_json = "{}"
        preview = event.get("preview")
        if preview:
            preview = str(preview)[:280]

        def _upsert() -> None:
            self._conn.execute(
                "INSERT INTO activity("
                "id, channel, direction, peer, ts, preview, status, thread_id, "
                "provider_id, raw_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "channel=excluded.channel, direction=excluded.direction, "
                "peer=excluded.peer, ts=excluded.ts, preview=excluded.preview, "
                "status=excluded.status, thread_id=excluded.thread_id, "
                "provider_id=excluded.provider_id, raw_json=excluded.raw_json",
                (
                    row_id, channel, event.get("direction"), event.get("peer"),
                    ts, preview, event.get("status"), event.get("thread_id"),
                    provider_id, raw_json, now,
                ),
            )
            self._conn.commit()

        async with self._write_lock:
            await asyncio.to_thread(_upsert)
        return row_id

    async def mark_read(self, id: str, read: bool = True) -> None:
        """Flip a row's read flag. Read state is the USER's, not the provider's —
        it's deliberately absent from append()/upsert()'s column lists, so the
        30s poller re-syncing a row never resets what you've already opened."""
        def _update() -> None:
            self._conn.execute("UPDATE activity SET read = ? WHERE id = ?", (1 if read else 0, str(id)))
            self._conn.commit()
        async with self._write_lock:
            await asyncio.to_thread(_update)

    # ── read side ─────────────────────────────────────────────────────────

    async def list(self, channel: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first list rows. `channel=None` (or 'all') returns every
        channel; otherwise filter to one. Excludes raw_json — list rows stay
        light; the detail view (`get`) carries the full payload."""
        chan = (channel or "").strip().lower()
        params: list[Any] = []
        clause = ""
        if chan and chan != "all":
            clause = "WHERE channel = ?"
            params.append(chan)
        params.append(int(limit))

        def _query() -> list[dict[str, Any]]:
            rows = self._conn.execute(
                f"SELECT {_LIST_COLS} FROM activity {clause} ORDER BY ts DESC LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

        return await asyncio.to_thread(_query)

    async def get(self, id: str) -> dict[str, Any] | None:
        """One row by id, including raw_json (parsed back to a dict when it
        decodes). Returns None when the id is unknown — the caller can then
        fall through to a provider live-fetch."""
        def _query() -> dict[str, Any] | None:
            r = self._conn.execute(
                f"SELECT {_LIST_COLS}, raw_json FROM activity WHERE id = ?",
                (str(id),),
            ).fetchone()
            if r is None:
                return None
            d = dict(r)
            raw = d.get("raw_json")
            if isinstance(raw, str):
                try:
                    d["raw_json"] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pass
            return d

        return await asyncio.to_thread(_query)
