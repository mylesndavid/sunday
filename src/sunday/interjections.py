"""Interjections — Sunday surfaces something unprompted.

Substrate shared by two consumers: proactive (knowledge gaps the observer
hears in real time) and nudges (atom decay surfacing stale work). Same
pipeline: gate → formulate → flare → user engages? → fold or dismiss.

Storage: ~/.sunday/interjections.db. Cheap, append-only.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.interjections")

# Cooldowns (seconds). After any interjection: 5 min hard hold. After a
# dismissed one: 15 min — the user said no, so be quieter. Both reset to
# 0 if the user actually engaged.
COOLDOWN_AFTER_FIRE = 5 * 60
COOLDOWN_AFTER_DISMISS = 15 * 60
CONFIDENCE_FLOOR = 0.85
AUTO_DISMISS_AFTER_SECONDS = 90   # if the flare goes unengaged this long, drop it


@dataclass(slots=True)
class Interjection:
    id: int
    ts: float
    kind: str               # "proac" | "nudge"
    trigger: str            # "knowledge_gap" | "deadline_due" | ...
    text: str               # the formulated one-liner shown to the user
    evidence: str           # why (quoted phrase / atom id)
    confidence: float
    source_atom_id: int | None
    engaged_at: float | None
    dismissed_at: float | None
    feedback: str | None    # "up" | "down" | None
    user_reply: str | None  # text the user typed in the notch input


class InterjectionStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (sunday_home() / "interjections.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS interjections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                trigger TEXT NOT NULL,
                text TEXT NOT NULL,
                evidence TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                source_atom_id INTEGER,
                engaged_at REAL,
                dismissed_at REAL,
                feedback TEXT,
                user_reply TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_inter_ts ON interjections(ts);
        """)
        self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────────

    def add(self, kind: str, trigger: str, text: str, evidence: str,
            confidence: float, source_atom_id: int | None = None) -> int:
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO interjections (ts, kind, trigger, text, evidence,
               confidence, source_atom_id) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now, kind, trigger, text, evidence, confidence, source_atom_id),
        )
        self._conn.commit()
        iid = cur.lastrowid or 0
        log.info("interjection added", id=iid, kind=kind, trigger=trigger, preview=text[:60])
        return iid

    def mark_engaged(self, iid: int, feedback: str | None = None, reply: str | None = None) -> bool:
        now = time.time()
        self._conn.execute(
            """UPDATE interjections SET engaged_at = ?, feedback = COALESCE(?, feedback),
               user_reply = COALESCE(?, user_reply) WHERE id = ? AND engaged_at IS NULL""",
            (now, feedback, reply, iid),
        )
        self._conn.commit()
        log.info("interjection engaged", id=iid, feedback=feedback, has_reply=bool(reply))
        return True

    def mark_dismissed(self, iid: int) -> None:
        now = time.time()
        self._conn.execute(
            "UPDATE interjections SET dismissed_at = ? WHERE id = ? AND engaged_at IS NULL AND dismissed_at IS NULL",
            (now, iid),
        )
        self._conn.commit()

    def sweep_auto_dismiss(self) -> int:
        """Drop interjections the user never engaged with after a while. Returns
        how many it dismissed."""
        cutoff = time.time() - AUTO_DISMISS_AFTER_SECONDS
        cur = self._conn.execute(
            "UPDATE interjections SET dismissed_at = ? WHERE engaged_at IS NULL AND dismissed_at IS NULL AND ts < ?",
            (time.time(), cutoff),
        )
        self._conn.commit()
        return cur.rowcount or 0

    # ── reads ─────────────────────────────────────────────────────────────

    def latest(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            """SELECT id, ts, kind, trigger, text, evidence, confidence,
                      source_atom_id, engaged_at, dismissed_at, feedback, user_reply
               FROM interjections ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        cols = ["id", "ts", "kind", "trigger", "text", "evidence", "confidence",
                "source_atom_id", "engaged_at", "dismissed_at", "feedback", "user_reply"]
        return [dict(zip(cols, r)) for r in rows]

    def engaged_since(self, since_ts: float, limit: int = 20) -> list[dict]:
        """Engaged interjections the main-chat agent should see in context."""
        rows = self._conn.execute(
            """SELECT id, ts, kind, trigger, text, feedback, user_reply
               FROM interjections WHERE engaged_at IS NOT NULL AND ts >= ?
               ORDER BY ts DESC LIMIT ?""",
            (since_ts, limit),
        ).fetchall()
        cols = ["id", "ts", "kind", "trigger", "text", "feedback", "user_reply"]
        return [dict(zip(cols, r)) for r in rows]

    def last_fired_at(self) -> float:
        row = self._conn.execute(
            "SELECT MAX(ts) FROM interjections"
        ).fetchone()
        return float(row[0] or 0)

    def last_dismissed_at(self) -> float:
        row = self._conn.execute(
            "SELECT MAX(dismissed_at) FROM interjections WHERE dismissed_at IS NOT NULL"
        ).fetchone()
        return float(row[0] or 0)


def cooldown_ok(store: InterjectionStore) -> tuple[bool, str]:
    """Is it OK to fire a new interjection right now? Returns (ok, why_not)."""
    now = time.time()
    if now - store.last_fired_at() < COOLDOWN_AFTER_FIRE:
        return False, f"hard cooldown ({int(now - store.last_fired_at())}s since last)"
    if now - store.last_dismissed_at() < COOLDOWN_AFTER_DISMISS:
        return False, f"dismiss cooldown ({int(now - store.last_dismissed_at())}s since dismiss)"
    return True, ""
