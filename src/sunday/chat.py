"""The one chat.

Sunday's opinion: there is exactly one conversation between you and Sunday,
ever. Voice, iMessage, the desktop app, the CLI — all just modalities into
the same log. Every message lands here, in order, regardless of how it
arrived. The model sees the same context no matter which interface you used.

Backed by SQLite at ~/.sunday/sunday.db. Append-only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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

    def to_llm(self) -> dict[str, str]:
        """OpenAI-compatible chat message dict.

        Sunday's 'sunday' role maps to 'assistant'. 'user' and 'system' pass through.
        """
        role = "assistant" if self.role == "sunday" else self.role
        return {"role": role, "content": self.content}

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
    """The append-only chat log.

    One per Sunday instance. Open it at daemon start, append on every turn,
    read recent N for LLM context.
    """

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

    def close(self) -> None:
        self._conn.close()
