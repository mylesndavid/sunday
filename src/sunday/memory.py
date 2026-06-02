"""Sunday's memory of you.

Architecture (matches what the best agents actually do — Hermes's MEMORY.md/
USER.md, Letta's core+archival, ChatGPT's saved-memories): a small
**always-in-context core** plus an **on-demand search**, NOT a per-turn
embedding round-trip.

  - core_block()  → all (or the most recent) durable facts, injected into
                    every turn's context. Local SQLite read, zero network,
                    sub-millisecond. The agent sees everything and picks what
                    fits — no semantic guessing, no API call in the hot path.
  - search()      → FTS5 keyword search over the facts, exposed as the
                    `recall` tool, for deliberate lookups once memory grows
                    past what's sensible to always inject.

Backing: SQLite at ~/.sunday/memories.db (+ an FTS5 index). No embeddings,
no OpenAI key required, no external calls. Facts are stored verbatim as
self-contained sentences and grown automatically by extract_facts() after
each turn.

(History: this used to embed every message with text-embedding-3-small and
do a vector KNN in the hot path — a network round-trip on every turn to
fetch 6 of ~90 short facts. That was slow and low-value; removed.)
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.memory")

# How many facts to always inject. At this size we inject all of them; the cap
# is headroom so the core block can't grow unbounded. Past it, the most recent
# CORE_LIMIT are injected and the rest are reachable via the recall (FTS) tool.
CORE_LIMIT = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    source      TEXT,
    created_at  REAL NOT NULL,
    metadata    TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
"""


@dataclass(slots=True)
class MemoryRow:
    id: int
    content: str
    source: str
    created_at: float
    distance: float = 0.0   # kept for API compat; unused (no vector distance now)


def _fts_query(query: str) -> str:
    """Turn a free-text query into a safe FTS5 MATCH expression: quote each
    term (so punctuation/operators can't break the parse) and OR them so we
    favour recall, letting bm25 rank the best matches first."""
    terms = [t for t in (query or "").replace('"', " ").split() if t.strip()]
    return " OR ".join(f'"{t}"' for t in terms)


class Memory:
    """Local, embedding-free fact store. Always available wherever SQLite is
    (no API key, no extensions required). FTS5 powers search; if the SQLite
    build lacks FTS5 we fall back to LIKE."""

    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "memories.db")
        self.available = False
        self._fts = False
        try:
            self._conn: sqlite3.Connection | None = sqlite3.connect(self.path, check_same_thread=False)
        except sqlite3.Error as exc:
            log.warning("could not open memory db — memory disabled", error=str(exc))
            self._conn = None
            return

        self._conn.executescript(_SCHEMA)
        # FTS5 index over the fact text (standalone table we keep in sync on
        # store/forget). Best-effort: if this SQLite lacks FTS5, search uses LIKE.
        try:
            # porter stemming so "walnut allergy" matches "allergic to walnuts"
            # — closes most of the gap between keyword search and semantic.
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(content, mem_id UNINDEXED, tokenize='porter unicode61')"
            )
            self._fts = True
            self._backfill_fts()
        except sqlite3.OperationalError as exc:
            log.info("FTS5 unavailable — memory search will use LIKE", error=str(exc))
        self._conn.commit()
        self.available = True

    def _backfill_fts(self) -> None:
        """Populate the FTS index from any facts that predate it (migration)."""
        if not self._conn:
            return
        n = self._conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
        if n == 0:
            self._conn.execute(
                "INSERT INTO memories_fts(content, mem_id) SELECT content, id FROM memories"
            )
            moved = self._conn.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0]
            if moved:
                log.info("memory FTS index backfilled", facts=moved)

    # ─── writes ──────────────────────────────────────────────────────────

    async def store(self, content: str, source: str = "auto", metadata: dict | None = None) -> int | None:
        # async kept for call-site compatibility; the work is all local now.
        if not self.available or not self._conn:
            return None
        content = (content or "").strip()
        if not content:
            return None
        cur = self._conn.execute(
            "INSERT INTO memories (content, source, created_at, metadata) VALUES (?, ?, ?, ?)",
            (content, source, time.time(), json.dumps(metadata) if metadata else None),
        )
        mem_id = cur.lastrowid or 0
        if self._fts:
            self._conn.execute(
                "INSERT INTO memories_fts(content, mem_id) VALUES (?, ?)", (content, mem_id)
            )
        self._conn.commit()
        log.info("memory stored", id=mem_id, source=source, preview=content[:60])
        return mem_id

    async def store_many(self, contents: list[str], source: str = "auto") -> list[int]:
        out: list[int] = []
        for c in contents:
            mid = await self.store(c, source=source)
            if mid:
                out.append(mid)
        return out

    # ─── reads ───────────────────────────────────────────────────────────

    def core_block(self, limit: int = CORE_LIMIT) -> str:
        """The always-injected memory: every durable fact (capped), formatted
        as soft background. Local read, no network. This is what makes Sunday
        'know you' — the agent sees all of it and uses what fits."""
        if not self.available or not self._conn:
            return ""
        rows = self._conn.execute(
            "SELECT content FROM memories ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        if not rows:
            return ""
        lines = [
            "(What you know about them — background to draw on when it fits what "
            "they're asking. Don't recite these or bring them up out of nowhere; "
            "just let them inform you.)",
            "",
        ]
        # oldest-first reads more naturally as a profile
        lines += [f"- {r[0]}" for r in reversed(rows)]
        return "\n".join(lines)

    def search(self, query: str, limit: int = 8) -> list[MemoryRow]:
        """Keyword search over the facts (FTS5 bm25-ranked, or LIKE fallback).
        Backs the `recall` tool — a deliberate lookup, not the hot path."""
        if not self.available or not self._conn:
            return []
        q = (query or "").strip()
        if not q:
            return []
        if self._fts:
            match = _fts_query(q)
            if not match:
                return []
            try:
                rows = self._conn.execute(
                    "SELECT m.id, m.content, m.source, m.created_at "
                    "FROM memories_fts f JOIN memories m ON m.id = f.mem_id "
                    "WHERE memories_fts MATCH ? ORDER BY bm25(memories_fts) LIMIT ?",
                    (match, int(limit)),
                ).fetchall()
                return [MemoryRow(id=r[0], content=r[1], source=r[2] or "", created_at=r[3]) for r in rows]
            except sqlite3.OperationalError as exc:
                log.warning("FTS search failed, falling back to LIKE", error=str(exc))
        # LIKE fallback — any term appears
        like_terms = [t for t in q.split() if t]
        where = " OR ".join("content LIKE ?" for _ in like_terms) or "1=0"
        params = [f"%{t}%" for t in like_terms] + [int(limit)]
        rows = self._conn.execute(
            f"SELECT id, content, source, created_at FROM memories WHERE {where} "
            f"ORDER BY created_at DESC LIMIT ?", params,
        ).fetchall()
        return [MemoryRow(id=r[0], content=r[1], source=r[2] or "", created_at=r[3]) for r in rows]

    def count(self) -> int:
        if not self.available or not self._conn:
            return 0
        return self._conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def all(self, limit: int = 100) -> list[MemoryRow]:
        if not self.available or not self._conn:
            return []
        rows = self._conn.execute(
            "SELECT id, content, source, created_at FROM memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [MemoryRow(id=r[0], content=r[1], source=r[2] or "", created_at=r[3]) for r in rows]

    def forget(self, mem_id: int) -> bool:
        if not self.available or not self._conn:
            return False
        if self._fts:
            self._conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (mem_id,))
        cur = self._conn.execute("DELETE FROM memories WHERE id = ?", (mem_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


# ─── extraction (auto-grow) ──────────────────────────────────────────────


EXTRACT_SYSTEM = (
    "You're a memory extractor. Read a short exchange between a user and their "
    "personal AI, and pull out durable facts about the user worth remembering "
    "for future conversations.\n\n"
    "Durable = preferences, relationships, ongoing projects, things they own, "
    "places they live/work, recurring habits, values, decisions they've made.\n"
    "NOT durable = today's mood, the current task, time-bound state, things "
    "they're asking Sunday to do right now.\n\n"
    "Each fact is a complete self-contained sentence with the subject explicit "
    "('User lives in Phoenix' not 'lives in Phoenix'). Skip anything the AI's "
    "reply alone tells you — facts come from what the user said or implied.\n\n"
    "Respond with a JSON array of strings. Empty array if nothing durable. "
    "Only the JSON, no prose."
)


async def extract_facts(exchanges: list[tuple[str, str]], config: SundayConfig) -> list[str]:
    """Distill durable facts from a batch of user+sunday exchanges in one call.

    Runs on the cheap utility model (small, reasoning off) — this is a
    distillation task, not the chat brain. Batched so it fires less often than
    once per turn.
    """
    from sunday.runtime import build_utility_runtime

    convo = "\n\n".join(
        f"User said:\n{(u or '').strip()}\n\nSunday replied:\n{(r or '').strip()}"
        for u, r in exchanges if (u or r)
    )
    if not convo.strip():
        return []
    prompt = f"{convo}\n\nJSON array of durable facts about the user:"
    try:
        rt = build_utility_runtime(config)
        result = await rt.complete(
            system_prompt=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools_schema=None,
            purpose="extract_facts",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("memory extraction failed", error=str(exc))
        return []

    content = (result.content or "").strip()
    # Tolerate code-fenced JSON
    if content.startswith("```"):
        first = content.find("\n")
        last  = content.rfind("```")
        if first > 0 and last > first:
            content = content[first + 1:last].strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        log.debug("extraction returned non-JSON", preview=content[:120])
        return []
    if not isinstance(parsed, list):
        return []
    return [str(f).strip() for f in parsed if isinstance(f, str) and f.strip()]
