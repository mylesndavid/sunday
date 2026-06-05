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
"""


@dataclass(slots=True)
class Message:
    id: int
    role: str
    modality: str
    content: str
    created_at: float
    metadata: dict[str, Any] | None

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
        }


class Chat:
    """The append-only chat log."""

    def __init__(self, path: Path | None = None) -> None:
        ensure_home()
        self.path = path or db_path()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(
        self,
        role: str,
        content: str,
        modality: str,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        cursor = self._conn.execute(
            "INSERT INTO messages (role, modality, content, created_at, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (role, modality, content, time.time(), json.dumps(metadata) if metadata else None),
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

    def recent(self, limit: int = 50) -> list[Message]:
        rows = self._conn.execute(
            "SELECT id, role, modality, content, created_at, metadata "
            "FROM messages ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = [
            Message(
                id=row[0],
                role=row[1],
                modality=row[2],
                content=row[3],
                created_at=row[4],
                metadata=json.loads(row[5]) if row[5] else None,
            )
            for row in rows
        ]
        out.reverse()
        return out

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def max_id(self) -> int:
        row = self._conn.execute("SELECT MAX(id) FROM messages").fetchone()
        return row[0] or 0

    def range(self, after_id: int, before_id: int, limit: int = 400) -> list[Message]:
        """Messages with after_id < id <= before_id, oldest first — used by
        compaction to fold aged-out turns into the running summary."""
        rows = self._conn.execute(
            "SELECT id, role, modality, content, created_at, metadata "
            "FROM messages WHERE id > ? AND id <= ? ORDER BY id ASC LIMIT ?",
            (after_id, before_id, limit),
        ).fetchall()
        return [
            Message(id=r[0], role=r[1], modality=r[2], content=r[3],
                    created_at=r[4], metadata=json.loads(r[5]) if r[5] else None)
            for r in rows
        ]

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
            "SELECT id, role, modality, content, created_at, metadata "
            f"FROM messages WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            Message(id=r[0], role=r[1], modality=r[2], content=r[3],
                    created_at=r[4], metadata=json.loads(r[5]) if r[5] else None)
            for r in rows
        ]

    def around(self, message_id: int, span: int = 3) -> list[Message]:
        """The `span` messages on each side of `message_id` (inclusive),
        oldest first — lets the model pull the surrounding exchange after a
        search hit."""
        rows = self._conn.execute(
            "SELECT id, role, modality, content, created_at, metadata FROM messages "
            "WHERE id BETWEEN ? AND ? ORDER BY id ASC",
            (message_id - span, message_id + span),
        ).fetchall()
        return [
            Message(id=r[0], role=r[1], modality=r[2], content=r[3],
                    created_at=r[4], metadata=json.loads(r[5]) if r[5] else None)
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()
