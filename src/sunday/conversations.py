"""Conversation store — real-world conversations captured by the observer.

Distinct from `chat` (text exchanges with Sunday) and `atoms` (working state).
Conversations are audio-originated, segmented by silence (OMI's pattern), and
each carries a verbatim transcript + a structured summary the agent can query.

Atoms born during a conversation point back to it via atoms.conversation_id,
so you can pivot from a commitment to the moment it was made.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.conversations")


class ConversationStore:
    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "conversations.db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._fts = False
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations(
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at   REAL NOT NULL,
            ended_at     REAL,
            title        TEXT,
            summary      TEXT,
            category     TEXT,
            participants TEXT,
            transcript   TEXT,
            source       TEXT,
            created_at   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_started_at ON conversations(started_at DESC);
        """)
        try:
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts USING fts5("
                "title, summary, transcript, conv_id UNINDEXED, tokenize='porter unicode61')"
            )
            self._fts = True
        except sqlite3.OperationalError as exc:
            log.info("FTS5 unavailable — conv search falls back to LIKE", error=str(exc))
        self._conn.commit()

    def add(self, started_at: float, ended_at: float | None = None,
            title: str | None = None, summary: str | None = None,
            category: str | None = None, participants: list[str] | None = None,
            transcript: str | None = None, source: str = "observer") -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO conversations(started_at, ended_at, title, summary, category, "
            "participants, transcript, source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (started_at, ended_at or now, title, summary, category,
             json.dumps(participants or []), transcript, source, now),
        )
        cid = cur.lastrowid or 0
        if self._fts and (title or summary or transcript):
            self._conn.execute(
                "INSERT INTO conversations_fts(rowid, title, summary, transcript, conv_id) "
                "VALUES (?,?,?,?,?)",
                (cid, title or "", summary or "", transcript or "", cid),
            )
        self._conn.commit()
        log.info("conversation added", id=cid, title=(title or "")[:60])
        return cid

    def list(self, limit: int = 50, since: float | None = None,
             category: str | None = None) -> list[dict[str, Any]]:
        cols = "id, started_at, ended_at, title, summary, category, participants, length(transcript) AS transcript_chars"
        where, params = [], []
        if since:
            where.append("started_at >= ?"); params.append(float(since))
        if category:
            where.append("category = ?"); params.append(category)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT {cols} FROM conversations {clause} ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_dict(r, cols) for r in rows]

    @staticmethod
    def value_tier(category: str | None, transcript_chars: int, summary: str | None) -> str:
        """high / medium / low. Used to hide noise (ambient sound, TikTok scroll,
        TV bleed, dialogue fragments) without throwing the data away.

        Rules — derived from looking at a real day's captures:
          • category 'meeting' or 'call' → high (always worth seeing)
          • category 'personal' and >250 chars → medium
          • category 'media' is media: only if >3000 chars AND not flagged
            "not a real conversation" in the summary
          • anything <200 chars → low (fragment)
          • summary that explicitly says it's noise → low
        """
        cat = (category or "").lower()
        s = (summary or "").lower()
        noise_phrases = ("not a real conversation", "no conversation detected",
                         "ambient", "disjointed compilation", "background noise")
        if transcript_chars < 200:
            return "low"
        if any(p in s for p in noise_phrases):
            return "low"
        if cat in ("meeting", "call"):
            return "high"
        if cat == "personal" and transcript_chars >= 250:
            return "medium"
        if cat == "media" and transcript_chars >= 3000:
            return "medium"
        if cat == "unclear" and transcript_chars >= 500:
            return "medium"
        return "low"

    def get(self, cid: int) -> dict[str, Any] | None:
        cols = "id, started_at, ended_at, title, summary, category, participants, transcript"
        r = self._conn.execute(
            f"SELECT {cols} FROM conversations WHERE id = ?", (int(cid),),
        ).fetchone()
        return self._row_to_dict(r, cols) if r else None

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        if not self._fts:
            # LIKE fallback
            like = f"%{query}%"
            rows = self._conn.execute(
                "SELECT id, started_at, ended_at, title, summary, category, participants "
                "FROM conversations WHERE title LIKE ? OR summary LIKE ? OR transcript LIKE ? "
                "ORDER BY started_at DESC LIMIT ?",
                (like, like, like, int(limit)),
            ).fetchall()
            return [self._row_to_dict(r, "id, started_at, ended_at, title, summary, category, participants") for r in rows]
        terms = [t for t in (query or "").replace('"', " ").split() if t.strip()]
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        try:
            rows = self._conn.execute(
                "SELECT c.id, c.started_at, c.ended_at, c.title, c.summary, c.category, c.participants "
                "FROM conversations_fts f JOIN conversations c ON c.id = f.conv_id "
                "WHERE conversations_fts MATCH ? ORDER BY bm25(conversations_fts) LIMIT ?",
                (match, int(limit)),
            ).fetchall()
            return [self._row_to_dict(r, "id, started_at, ended_at, title, summary, category, participants") for r in rows]
        except sqlite3.OperationalError:
            return []

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    @staticmethod
    def _row_to_dict(row, cols: str) -> dict[str, Any]:
        # Tolerate "expr AS alias" — use the alias as the dict key, not the
        # whole SQL expression. Falls back to the bare column name otherwise.
        keys = []
        for piece in cols.split(", "):
            keys.append(piece.split(" AS ")[-1].strip() if " AS " in piece else piece.strip())
        d = dict(zip(keys, row))
        try:
            d["participants"] = json.loads(d.get("participants") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["participants"] = []
        return d
