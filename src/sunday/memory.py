"""Sunday's memory of you.

Architecture (matches what the best agents actually do — Hermes's MEMORY.md/
USER.md, Letta's core+archival, ChatGPT's saved-memories): a small
**always-in-context core** plus an **on-demand search**, NOT a per-turn
embedding round-trip.

  - core_block()     → all (or the most recent) durable facts, injected into
                       every turn's context. Local SQLite read, zero network,
                       sub-millisecond. The agent sees everything and picks what
                       fits — no semantic guessing, no API call in the hot path.
  - search()         → FTS5 keyword search over the facts (always available).
  - search_hybrid()  → what the `recall` tool actually calls: FTS fused with a
                       LOCAL-embedding vector search (sqlite-vec + weighted RRF,
                       vec 0.7 / fts 0.3). Benchmarked on the real user corpus:
                       100% R@5 vs 76.7% for FTS alone — vectors catch paraphrase
                       ("laptop"→MacBook, "mom"→mother), FTS catches exact rare
                       names. Degrades to plain FTS when no local embedder is up.

Backing: SQLite at ~/.sunday/memories.db (+ FTS5 index + a sqlite-vec table fed
by embeddings.py — Ollama or Sunday's llama-server, strictly on-device). Facts
are stored verbatim as self-contained sentences and grown automatically by
extract_facts() after each turn. Vector indexing is background + best-effort.

(History: this used to embed every message with text-embedding-3-small and
do a vector KNN in the hot path — a NETWORK round-trip on every turn to fetch
6 of ~90 short facts. That was slow and low-value; removed. The hybrid recall
above is different on every axis: local-only, recall-path-only, background
indexing — measured, not vibes: see findings_agentmemory_spike.)
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
    """Local fact store. Always available wherever SQLite is (no API key, no
    network). FTS5 powers keyword search; when a LOCAL embedding endpoint is
    up (embeddings.py), recall upgrades to hybrid FTS+vector via sqlite-vec.
    No embedder → plain FTS, exactly the old behavior."""

    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "memories.db")
        self.available = False
        self._fts = False
        self._vec = False          # sqlite-vec extension loaded
        self._vec_dim = 0          # discovered from the first embedding batch
        self._indexing = False     # debounce flag for background vector indexing
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
        # sqlite-vec powers the hybrid recall path. Best-effort: without the
        # extension (or without a local embedder) everything runs on FTS alone.
        try:
            import sqlite_vec
            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec = True
        except Exception as exc:  # noqa: BLE001
            log.info("sqlite-vec unavailable — recall is FTS-only", error=str(exc)[:80])
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
        self._schedule_indexing()
        return mem_id

    async def store_many(self, contents: list[str], source: str = "auto") -> list[int]:
        out: list[int] = []
        for c in contents:
            mid = await self.store(c, source=source)
            if mid:
                out.append(mid)
        return out

    # ─── vectors (hybrid recall) ─────────────────────────────────────────

    def _schedule_indexing(self) -> None:
        """Fire-and-forget background vector indexing. Never blocks a store,
        never raises; without a running loop (sync scripts) it just skips —
        index_pending() catches up later."""
        if not self._vec:
            return
        try:
            import asyncio
            asyncio.get_running_loop().create_task(self.index_pending())
        except RuntimeError:
            pass

    def _vec_table(self, dim: int) -> bool:
        """Ensure the vec0 table exists for `dim` (and matches the current
        embed model — model change invalidates old vectors: drop + re-embed)."""
        if not self._conn or not self._vec:
            return False
        from sunday.embeddings import EMBED_MODEL
        try:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS memory_vec_meta (key TEXT PRIMARY KEY, value TEXT)")
            row = self._conn.execute(
                "SELECT value FROM memory_vec_meta WHERE key='model'").fetchone()
            current = f"{EMBED_MODEL}:{dim}"
            if row and row[0] != current:
                log.info("embed model changed — re-indexing vectors", was=row[0], now=current)
                self._conn.execute("DROP TABLE IF EXISTS memory_vecs_local")
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS memory_vecs_local "
                f"USING vec0(memory_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}])")
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_vec_meta(key, value) VALUES ('model', ?)",
                (current,))
            self._conn.commit()
            self._vec_dim = dim
            return True
        except sqlite3.Error as exc:
            log.warning("vec table setup failed", error=str(exc)[:120])
            return False

    async def index_pending(self, batch: int = 64) -> int:
        """Embed any facts that don't have vectors yet (new stores, migration,
        or an embedder that just came online). Background + best-effort."""
        if not self.available or not self._conn or not self._vec or self._indexing:
            return 0
        self._indexing = True
        total = 0
        try:
            from sunday.embeddings import get_embedder
            emb = get_embedder()
            while True:
                try:
                    rows = self._conn.execute(
                        "SELECT m.id, m.content FROM memories m "
                        "WHERE NOT EXISTS (SELECT 1 FROM memory_vecs_local v WHERE v.memory_id = m.id) "
                        "LIMIT ?", (batch,)).fetchall()
                except sqlite3.OperationalError:
                    rows = self._conn.execute(  # vec table doesn't exist yet — everything is pending
                        "SELECT id, content FROM memories LIMIT ?", (batch,)).fetchall()
                if not rows:
                    break
                vecs = await emb.embed([r[1] for r in rows], kind="document")
                if not vecs:
                    break               # no local embedder right now; try again later
                if not self._vec_dim and not self._vec_table(len(vecs[0])):
                    break
                import sqlite_vec
                for (mid, _), v in zip(rows, vecs):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO memory_vecs_local(memory_id, embedding) VALUES (?, ?)",
                        (mid, sqlite_vec.serialize_float32(v)))
                self._conn.commit()
                total += len(rows)
                if len(rows) < batch:
                    break
            if total:
                log.info("memory vectors indexed", count=total)
        except Exception as exc:  # noqa: BLE001
            log.warning("vector indexing failed", error=str(exc)[:120])
        finally:
            self._indexing = False
        return total

    async def search_hybrid(self, query: str, limit: int = 8) -> list[MemoryRow]:
        """Recall: FTS + local-vector search fused with weighted RRF
        (vec 0.7 / fts 0.3, k=10 — the benchmarked recipe). Falls back to
        plain FTS when sqlite-vec or a local embedder isn't available."""
        fts_rows = self.search(query, limit=20)
        if not self.available or not self._conn or not self._vec:
            return fts_rows[:limit]
        try:
            from sunday.embeddings import get_embedder
            qv = await get_embedder().embed([query], kind="query")
            if not qv:
                return fts_rows[:limit]
            if not self._vec_dim and not self._vec_table(len(qv[0])):
                return fts_rows[:limit]
            import sqlite_vec
            vec_ids = [r[0] for r in self._conn.execute(
                "SELECT memory_id, distance FROM memory_vecs_local "
                "WHERE embedding MATCH ? AND k = 20 ORDER BY distance",
                (sqlite_vec.serialize_float32(qv[0]),)).fetchall()]
        except Exception as exc:  # noqa: BLE001
            log.warning("hybrid recall fell back to FTS", error=str(exc)[:120])
            return fts_rows[:limit]
        # Weighted reciprocal-rank fusion. Vectors carry paraphrase; FTS
        # carries exact tokens (rare names). 0.7/0.3 @ k=10 measured best.
        rrf: dict[int, float] = {}
        for rank, mid in enumerate(vec_ids):
            rrf[mid] = rrf.get(mid, 0.0) + 0.7 / (10 + rank + 1)
        for rank, row in enumerate(fts_rows):
            rrf[row.id] = rrf.get(row.id, 0.0) + 0.3 / (10 + rank + 1)
        if not rrf:
            return []
        top = sorted(rrf, key=lambda d: -rrf[d])[:limit]
        by_id = {r.id: r for r in fts_rows}
        out: list[MemoryRow] = []
        for mid in top:
            if mid in by_id:
                out.append(by_id[mid])
            else:
                r = self._conn.execute(
                    "SELECT id, content, source, created_at FROM memories WHERE id = ?",
                    (mid,)).fetchone()
                if r:
                    out.append(MemoryRow(id=r[0], content=r[1], source=r[2] or "", created_at=r[3]))
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

    def update(self, mem_id: int, content: str) -> bool:
        """Edit a fact's text in place, keeping every index consistent:
          - rewrite the row's content,
          - rewrite the FTS row so keyword search reflects the new text,
          - drop the vector row so background index_pending() re-embeds it
            (a stale embedding for the old text would poison hybrid recall).
        Returns False if the id doesn't exist or content is empty."""
        if not self.available or not self._conn:
            return False
        content = (content or "").strip()
        if not content:
            return False
        cur = self._conn.execute(
            "UPDATE memories SET content = ? WHERE id = ?", (content, mem_id)
        )
        if cur.rowcount == 0:
            return False
        if self._fts:
            # FTS5 standalone table — keep it in sync (delete + reinsert is the
            # simplest correct path; mem_id is UNINDEXED so we target by it).
            self._conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (mem_id,))
            self._conn.execute(
                "INSERT INTO memories_fts(content, mem_id) VALUES (?, ?)", (content, mem_id)
            )
        if self._vec:
            # Drop the now-stale vector; index_pending() will re-embed the new text.
            try:
                self._conn.execute("DELETE FROM memory_vecs_local WHERE memory_id = ?", (mem_id,))
            except sqlite3.OperationalError:
                pass   # vec table may not exist yet — nothing to drop
        self._conn.commit()
        log.info("memory updated", id=mem_id, preview=content[:60])
        self._schedule_indexing()
        return True

    def forget(self, mem_id: int) -> bool:
        if not self.available or not self._conn:
            return False
        if self._fts:
            self._conn.execute("DELETE FROM memories_fts WHERE mem_id = ?", (mem_id,))
        if self._vec:
            try:
                self._conn.execute("DELETE FROM memory_vecs_local WHERE memory_id = ?", (mem_id,))
            except sqlite3.OperationalError:
                pass
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
