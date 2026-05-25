"""Sunday's memory of you.

Every turn, Sunday recalls a handful of relevant facts about the user and
injects them into the system prompt. Every turn, she also kicks off a
background task that asks an LLM to extract any new durable facts from
the latest exchange and writes them in. The memory grows automatically;
you don't have to ask her to remember things, though you can.

Backing: SQLite + sqlite-vec at ~/.sunday/memories.db. Embeddings via
OpenAI's text-embedding-3-small (1536-dim, ~$0.02/1M tokens). The
OPENAI_API_KEY credential is required — without it, memory is disabled
and Sunday logs a one-time warning at boot.

Facts are stored verbatim as text plus a vector. Cosine similarity at
recall time. No graph, no entity extraction — just embeddings of
self-contained sentences. Simple and works.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.memory")

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS  = 1536

DEFAULT_TOP_K = 6
# Cosine distance gate. text-embedding-3-small in practice produces distances
# of 0.4-0.9 even for semantically related text; 1.5 effectively means "trust
# top-K." If you want stricter relevance, tighten this in config later.
DEFAULT_RECALL_FLOOR = 1.5

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
    distance: float = 0.0


def _embed_client():
    """Lazy import of openai so memory module loads without OpenAI installed."""
    from openai import AsyncOpenAI
    key = get_credential("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY is required for Sunday's memory (used for "
            "embeddings). Run: sunday credential set OPENAI_API_KEY <key>"
        )
    return AsyncOpenAI(api_key=key)


def _emb_bytes(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


class Memory:
    """sqlite-vec backed semantic memory. Append-only, async write/read.

    Construction is sync (opens the db). Embedding calls are async and
    require OPENAI_API_KEY. If sqlite-vec or the OpenAI key is missing,
    `available` flips to False and all writes/reads return cleanly with a
    `disabled` flag — the rest of Sunday keeps working.
    """

    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "memories.db")
        self.available = False
        try:
            import sqlite_vec  # noqa: F401
        except ImportError:
            log.warning("sqlite_vec not installed — memory disabled (pip install sqlite-vec)")
            self._conn: sqlite3.Connection | None = None
            return

        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self._conn.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
        except (sqlite3.OperationalError, AttributeError) as exc:
            log.warning("could not load sqlite-vec extension — memory disabled", error=str(exc))
            self._conn.close()
            self._conn = None
            return

        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs USING vec0("
            f"memory_id INTEGER PRIMARY KEY, embedding FLOAT[{EMBED_DIMS}])"
        )
        self._conn.commit()
        self.available = True

    # ─── writes ──────────────────────────────────────────────────────────

    async def store(self, content: str, source: str = "auto", metadata: dict | None = None) -> int | None:
        if not self.available or not self._conn:
            return None
        content = (content or "").strip()
        if not content:
            return None
        client = _embed_client()
        try:
            res = await client.embeddings.create(model=EMBED_MODEL, input=content)
        except Exception as exc:  # noqa: BLE001
            log.warning("embedding failed", error=str(exc))
            return None
        vec = res.data[0].embedding

        cur = self._conn.execute(
            "INSERT INTO memories (content, source, created_at, metadata) VALUES (?, ?, ?, ?)",
            (content, source, time.time(), json.dumps(metadata) if metadata else None),
        )
        mem_id = cur.lastrowid or 0
        self._conn.execute(
            "INSERT INTO memory_vecs(memory_id, embedding) VALUES (?, ?)",
            (mem_id, _emb_bytes(vec)),
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

    async def recall(self, query: str, top_k: int = DEFAULT_TOP_K, floor: float = DEFAULT_RECALL_FLOOR) -> list[MemoryRow]:
        if not self.available or not self._conn:
            return []
        query = (query or "").strip()
        if not query or self.count() == 0:
            return []
        client = _embed_client()
        try:
            res = await client.embeddings.create(model=EMBED_MODEL, input=query)
        except Exception as exc:  # noqa: BLE001
            log.warning("recall embedding failed", error=str(exc))
            return []
        vec = res.data[0].embedding

        # vec0 KNN requires `k = ?` as a constraint, not a tail LIMIT, when
        # joined with other tables. We do the KNN as a CTE first, then join.
        rows = self._conn.execute(
            """
            WITH knn AS (
                SELECT memory_id, distance
                FROM memory_vecs
                WHERE embedding MATCH ? AND k = ?
            )
            SELECT m.id, m.content, m.source, m.created_at, knn.distance
            FROM knn
            JOIN memories m ON m.id = knn.memory_id
            ORDER BY knn.distance
            """,
            (_emb_bytes(vec), int(top_k)),
        ).fetchall()
        out = [MemoryRow(id=r[0], content=r[1], source=r[2] or "", created_at=r[3], distance=r[4]) for r in rows]
        # Apply relevance floor so we don't inject barely-related stuff.
        return [r for r in out if r.distance <= floor]

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
        self._conn.execute("DELETE FROM memory_vecs WHERE memory_id = ?", (mem_id,))
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


async def extract_facts(user_text: str, sunday_reply: str, config: SundayConfig) -> list[str]:
    """Call an LLM to distill durable facts from a single user+sunday exchange.

    Uses OpenRouter (default runtime) so it's cheap and routed through the
    same gateway as the main brain.
    """
    from sunday.runtime_openai import OpenAIRuntime

    prompt = f"User said:\n{user_text.strip()}\n\nSunday replied:\n{sunday_reply.strip()}\n\nJSON array of durable facts about the user:"
    try:
        rt = OpenAIRuntime(config)
        result = await rt.complete(
            system_prompt=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools_schema=None,
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


def recall_block(memories: list[MemoryRow]) -> str:
    """Format recalled memories as a system-prompt block. Empty when no hits."""
    if not memories:
        return ""
    lines = ["", "# What you know about them", ""]
    for m in memories:
        lines.append(f"- {m.content}")
    return "\n".join(lines)
