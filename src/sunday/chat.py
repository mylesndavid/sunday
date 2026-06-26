"""The one chat.

Sunday's opinion: there is exactly one conversation between you and Sunday,
ever. Voice, iMessage, the desktop app, the CLI — all just modalities into
the same log. Every message lands here, in order, regardless of how it
arrived. The model sees the same context no matter which interface you used.

Backed by SQLite at ~/.sunday/sunday.db. Append-only. Each message stores
its role (user/sunday/tool/system), its modality (cli/electron/voice/
imessage/vapi/system), and free-form metadata (tool calls, model name,
device id, etc.).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sunday.attachments import Attachment
from sunday.paths import db_path, ensure_home

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  role        TEXT NOT NULL,
  modality    TEXT NOT NULL,
  content     TEXT NOT NULL,
  created_at  REAL NOT NULL,
  metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);

-- Slack-style threads. A message with thread_id = NULL lives on the main
-- timeline (the one continuous chat). A message with thread_id set is a reply
-- branched off the threads.root_message_id, kept off the main timeline.
CREATE TABLE IF NOT EXISTS threads (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  root_message_id INTEGER NOT NULL,
  title           TEXT,
  created_at      REAL NOT NULL,
  last_active_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_threads_root ON threads(root_message_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migration. Safe to run on every startup and safe to
    run repeatedly — it only ever ADDs the thread_id column when absent and never
    rewrites or deletes an existing row. NULL thread_id is the main timeline, so
    every pre-existing message keeps its place untouched.

    Modelled on atoms.py's PRAGMA-guarded ALTER pattern. SQLite has no
    ``ADD COLUMN IF NOT EXISTS``, so we probe table_info first."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "thread_id" not in cols:
        # Nullable, no default rewrite of existing rows — SQLite ADD COLUMN with
        # no DEFAULT leaves every existing row's new cell as NULL in place.
        conn.execute("ALTER TABLE messages ADD COLUMN thread_id INTEGER")
    # Partial index: only threaded rows are indexed, so the main-timeline read
    # path (thread_id IS NULL) is unaffected and the index stays tiny.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_thread "
        "ON messages(thread_id, id) WHERE thread_id IS NOT NULL"
    )
    conn.commit()


@dataclass(slots=True)
class Message:
    id: int
    role: str
    modality: str
    content: str
    created_at: float
    metadata: dict[str, Any] | None
    # NULL = main timeline; set = a Slack-style thread reply. Default keeps every
    # existing construction site (and to_llm, which never emits it) working.
    thread_id: int | None = None

    def attachments(self) -> list[Attachment]:
        raw = (self.metadata or {}).get("attachments") or []
        return [Attachment.from_dict(a) for a in raw if isinstance(a, dict)]

    def to_llm(self) -> dict[str, Any]:
        """OpenAI-compatible chat message dict.

        Handles tool-call assistant messages, tool result messages, and
        vision multipart content when image attachments are present on a
        user message. Non-image attachments render as a short text
        descriptor at the end of content — the model can call a tool to
        actually consume them.
        """
        meta = self.metadata or {}

        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": meta.get("tool_call_id", ""),
                "content": self.content,
            }

        role = "assistant" if self.role == "sunday" else self.role
        attachments = self.attachments()
        images = [a for a in attachments if a.is_image()]
        others = [a for a in attachments if not a.is_image()]

        # Build descriptive suffix for non-image attachments so the model is
        # aware of them without needing vision capability.
        descriptor = ""
        if others:
            lines = [
                f"[attached {a.kind}: {a.filename}"
                + (f", {a.size} bytes" if a.size else "")
                + (f", {a.mime_type}" if a.mime_type else "")
                + (f", url={a.url}" if a.url else "")
                + (f", path={a.path}" if a.path else "")
                + "]"
                for a in others
            ]
            descriptor = "\n" + "\n".join(lines)

        text_part = (self.content or "") + descriptor

        # Origin tag for browser-side-panel messages. The model can't see
        # message modality otherwise, and "what's on this page" from the
        # Cockpit panel means THE ACTIVE CHROME TAB — without the tag the
        # model reaches for the wrong browser (live failure: it hunted down
        # the headless browser_read and died on "no CDP session"). The rule
        # for what to do with the tag lives in the static system prompt; the
        # tag itself is per-message data, not a per-message rule.
        if role == "user" and self.modality == "cockpit":
            text_part = "[via Cockpit side panel — the user is in their browser] " + text_part

        # Multipart vision content only makes sense on user-role messages.
        if images and role == "user":
            content_parts: list[dict[str, Any]] = []
            if text_part:
                content_parts.append({"type": "text", "text": text_part})
            for img in images:
                url = img.to_llm_image_url()
                if url:
                    content_parts.append({"type": "image_url", "image_url": {"url": url}})
            out: dict[str, Any] = {"role": role, "content": content_parts}
        else:
            out = {"role": role, "content": text_part}

        tcs = meta.get("tool_calls") if self.role == "sunday" else None
        if tcs:
            out["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in tcs
            ]
        # Pass reasoning content back to providers that consume it
        # (deepseek-reasoner, OpenAI o-series). Cheap on chat models — they
        # ignore unknown fields.
        if self.role == "sunday" and meta.get("reasoning_content"):
            out["reasoning_content"] = meta["reasoning_content"]
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "modality": self.modality,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "thread_id": self.thread_id,
        }


class Chat:
    """The append-only chat log."""

    def __init__(self, path: Path | None = None) -> None:
        ensure_home()
        self.path = path or db_path()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # Additive, idempotent — safe on every startup, never touches existing rows.
        _migrate(self._conn)

    def append(
        self,
        role: str,
        content: str,
        modality: str,
        metadata: dict[str, Any] | None = None,
        thread_id: int | None = None,
    ) -> int:
        now = time.time()
        cursor = self._conn.execute(
            "INSERT INTO messages (role, modality, content, created_at, metadata, thread_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (role, modality, content, now, json.dumps(metadata) if metadata else None, thread_id),
        )
        # A reply keeps its thread "warm" so thread lists can sort by recency.
        if thread_id is not None:
            self._conn.execute(
                "UPDATE threads SET last_active_at = ? WHERE id = ?", (now, thread_id)
            )
        self._conn.commit()
        return cursor.lastrowid or 0

    def clear(self) -> int:
        """Wipe the whole conversation log (a fresh start). Returns how many
        messages were removed. Does NOT touch the memory DB — durable facts
        survive a chat clear. Compaction state is reset separately by the caller."""
        n = self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self._conn.execute("DELETE FROM messages")
        self._conn.commit()
        return int(n or 0)

    # Every row read goes through this so the column list (incl. thread_id) lives
    # in exactly one place. thread_id is the last selected column.
    _COLS = "id, role, modality, content, created_at, metadata, thread_id"

    @staticmethod
    def _row(r) -> Message:
        return Message(
            id=r[0], role=r[1], modality=r[2], content=r[3], created_at=r[4],
            metadata=json.loads(r[5]) if r[5] else None,
            thread_id=r[6] if len(r) > 6 else None,
        )

    def get(self, message_id: int) -> Message | None:
        row = self._conn.execute(
            f"SELECT {self._COLS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        return self._row(row) if row else None

    def truncate_from(self, message_id: int) -> int:
        """Rewind: delete the message with this id and everything after it.
        Returns how many messages were removed. Used by edit-and-rewind — the
        edited message is re-appended fresh by the caller, then a turn re-runs."""
        n = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE id >= ?", (message_id,)
        ).fetchone()[0]
        self._conn.execute("DELETE FROM messages WHERE id >= ?", (message_id,))
        self._conn.commit()
        return int(n or 0)

    def recent(self, limit: int = 50) -> list[Message]:
        """The most recent messages on the MAIN timeline, oldest first.

        Scoped to ``thread_id IS NULL`` — thread replies are off the main
        timeline (Slack semantics) and must never bleed into the main-chat tail
        the brain sends each turn. Pre-thread DBs have no threaded rows, so this
        is identical to the old behaviour for them."""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM messages WHERE thread_id IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = [self._row(r) for r in rows]
        out.reverse()
        return out

    def thread_messages(self, thread_id: int, limit: int = 400) -> list[Message]:
        """All replies in a thread, oldest first. The root message is NOT
        included here (it lives on the main timeline); callers fetch it via
        get(threads.root_message_id) and prepend it."""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM messages WHERE thread_id = ? "
            "ORDER BY id ASC LIMIT ?",
            (thread_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def max_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    def previous_message_time(self) -> float | None:
        """created_at of the SECOND-most-recent message — i.e. the last contact
        BEFORE the current turn. respond() appends the new user message before
        building the per-turn context, so the most recent row is this turn and
        the one before it is "when we last talked." Returns None when there's
        no prior message (the very first message ever). Cheap: indexed, no
        content read. Main timeline only — a thread reply isn't "the last time
        you talked to Sunday" on the main chat."""
        row = self._conn.execute(
            "SELECT created_at FROM messages WHERE thread_id IS NULL "
            "ORDER BY id DESC LIMIT 1 OFFSET 1"
        ).fetchone()
        return row[0] if row else None

    def range(self, after_id: int, before_id: int, limit: int = 400) -> list[Message]:
        """Messages with after_id < id <= before_id, oldest first — used by
        compaction to fold aged-out turns into the running summary. Main timeline
        only: the rolling summary describes the main chat, not thread side-bars."""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM messages "
            "WHERE id > ? AND id <= ? AND thread_id IS NULL ORDER BY id ASC LIMIT ?",
            (after_id, before_id, limit),
        ).fetchall()
        return [self._row(r) for r in rows]

    def search(self, query: str, limit: int = 8, roles: tuple[str, ...] = ("user", "sunday")) -> list[Message]:
        """Keyword search over the raw message log — the verbatim history,
        not the extracted-facts memory DB. This is how the model reaches back
        past the compacted summary to the actual words that were said.

        Matches every whitespace-separated term (AND) case-insensitively in
        content. Defaults to user + Sunday turns (tool-result rows are huge
        JSON blobs and almost never what 'what did I say about X' wants).
        Returns most-recent-first."""
        terms = [t for t in (query or "").split() if t.strip()]
        if not terms:
            return []
        role_ph = ",".join("?" for _ in roles)
        where = [f"role IN ({role_ph})"] + ["content LIKE ? ESCAPE '\\'" for _ in terms]
        params: list[Any] = list(roles)
        for t in terms:
            esc = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{esc}%")
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT {self._COLS} "
            f"FROM messages WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row(r) for r in rows]

    def around(self, message_id: int, span: int = 3) -> list[Message]:
        """The `span` messages on each side of `message_id` (inclusive),
        oldest first — lets the model pull the surrounding exchange after a
        search hit."""
        rows = self._conn.execute(
            f"SELECT {self._COLS} FROM messages "
            "WHERE id BETWEEN ? AND ? ORDER BY id ASC",
            (message_id - span, message_id + span),
        ).fetchall()
        return [self._row(r) for r in rows]

    # ─── threads (Slack-style reply branches) ────────────────────────────

    def create_thread(self, root_message_id: int, title: str | None = None) -> int:
        """Open a thread off an existing main-timeline message (idempotent on the
        root: a message has at most one thread, so a second call returns the
        existing thread's id rather than forking). Returns the thread id."""
        existing = self._conn.execute(
            "SELECT id FROM threads WHERE root_message_id = ?", (root_message_id,)
        ).fetchone()
        if existing:
            return int(existing[0])
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO threads (root_message_id, title, created_at, last_active_at) "
            "VALUES (?, ?, ?, ?)",
            (root_message_id, title, now, now),
        )
        self._conn.commit()
        return cur.lastrowid or 0

    def get_thread(self, thread_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id, root_message_id, title, created_at, last_active_at "
            "FROM threads WHERE id = ?",
            (thread_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "root_message_id": row[1], "title": row[2],
            "created_at": row[3], "last_active_at": row[4],
            "reply_count": self._reply_count(row[0]),
        }

    def thread_for_message(self, message_id: int) -> int | None:
        """The thread rooted at this message, if one exists."""
        row = self._conn.execute(
            "SELECT id FROM threads WHERE root_message_id = ?", (message_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def _reply_count(self, thread_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id = ?", (thread_id,)
        ).fetchone()
        return int(row[0] or 0)

    def list_threads(self, limit: int = 100) -> list[dict[str, Any]]:
        """All threads, most-recently-active first, each with its root message
        preview + reply count — the data a thread list/index needs."""
        rows = self._conn.execute(
            "SELECT id, root_message_id, title, created_at, last_active_at "
            "FROM threads ORDER BY last_active_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            root = self.get(r[1])
            out.append({
                "id": r[0], "root_message_id": r[1], "title": r[2],
                "created_at": r[3], "last_active_at": r[4],
                "reply_count": self._reply_count(r[0]),
                "root_preview": (root.content[:140] if root else None),
                "root_role": (root.role if root else None),
            })
        return out

    def reply_counts(self) -> dict[int, int]:
        """{root_message_id: reply_count} for every thread — lets the main
        timeline render an "N replies" badge under each rooted message in one
        cheap query instead of per-message lookups."""
        rows = self._conn.execute(
            "SELECT t.root_message_id, COUNT(m.id) "
            "FROM threads t LEFT JOIN messages m ON m.thread_id = t.id "
            "GROUP BY t.root_message_id"
        ).fetchall()
        return {int(r[0]): int(r[1] or 0) for r in rows}

    def close(self) -> None:
        self._conn.close()
