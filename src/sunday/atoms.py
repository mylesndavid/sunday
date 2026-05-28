"""Atom store — structured working state + reference, observed and maintained
by the ambient agent.

V2 (the legitimate substrate):

  WORKING (live; decays; reinforced/closed/dropped/superseded; eligible for nudges)
    Kinds: commitment, thread, deadline.

  REFERENCE (immutable; never decays; never in the active count; surfaced on request)
    Kinds: decision, fact.

Key invariants:
  - Atom text is immutable. Changes happen via `superseded` (new atom + link).
  - Every atom has an `owner` (you / <name> / "unclear"). Unclear-owner atoms
    are HELD: never in atoms_open, no nudges, faster decay, promoted when a
    later observation clarifies ownership.
  - Every state transition writes an `atom_events` row with action, source,
    snippet, and confidence — the audit trail. You can't close what you can't
    audit.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.atoms")

ATOM_STATES = {"active", "completed", "stale", "dropped", "escalating", "superseded"}
ATOM_KINDS  = {"commitment", "thread", "deadline", "decision", "fact"}
WORKING_KINDS = ("commitment", "thread", "deadline")
WORKING_STATES = ("active", "escalating")

# Until the nudge layer (#5) exists, anything below this confidence on a
# close/drop/supersede must NOT silently complete an atom — coerce to
# reinforced. The "thing I didn't finish shows up done" trust-break is the
# fastest way to lose the user's belief in the substrate.
CONFIDENCE_CLOSE_THRESHOLD = 0.85


class AtomStore:
    def __init__(self, path: Path | None = None) -> None:
        sunday_home().mkdir(parents=True, exist_ok=True)
        self.path = path or (sunday_home() / "atoms.db")
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS atoms(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT NOT NULL,
            kind        TEXT,
            state       TEXT NOT NULL DEFAULT 'active',
            owner       TEXT,
            evidence    TEXT,
            source      TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_atoms_state ON atoms(state);
        CREATE INDEX IF NOT EXISTS idx_atoms_updated_at ON atoms(updated_at DESC);
        CREATE TABLE IF NOT EXISTS atom_events(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            atom_id     INTEGER NOT NULL,
            ts          REAL NOT NULL,
            source      TEXT,
            snippet     TEXT,
            action      TEXT NOT NULL,
            confidence  REAL
        );
        CREATE INDEX IF NOT EXISTS idx_atom_events_atom_id ON atom_events(atom_id, ts DESC);
        """)
        # v2 column adds — idempotent
        existing = {r[1] for r in self._conn.execute("PRAGMA table_info(atoms)").fetchall()}
        for col, ddl in [
            ("completion_signal", "ALTER TABLE atoms ADD COLUMN completion_signal TEXT"),
            ("superseded_by",     "ALTER TABLE atoms ADD COLUMN superseded_by INTEGER"),
            ("conversation_id",   "ALTER TABLE atoms ADD COLUMN conversation_id INTEGER"),
        ]:
            if col not in existing:
                self._conn.execute(ddl)
        self._conn.commit()

    def add(self, text: str, kind: str | None = None, state: str = "active",
            owner: str | None = None, evidence: str | None = None,
            source: str = "observer",
            completion_signal: str | None = None,
            confidence: float = 1.0) -> int:
        text = (text or "").strip()
        if not text:
            return 0
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO atoms(text, kind, state, owner, evidence, source, "
            "completion_signal, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (text, kind, state, owner, evidence, source, completion_signal, now, now),
        )
        aid = cur.lastrowid or 0
        self._conn.execute(
            "INSERT INTO atom_events(atom_id, ts, source, snippet, action, confidence) VALUES (?,?,?,?,?,?)",
            (aid, now, source, evidence, "created", confidence),
        )
        self._conn.commit()
        log.info("atom added", id=aid, kind=kind, owner=owner, preview=text[:60])
        return aid

    def apply_update(self, aid: int, action: str, *,
                     state: str | None = None, evidence: str | None = None,
                     confidence: float | None = None, source: str = "observer",
                     superseded_by: int | None = None) -> dict[str, Any]:
        """The single mutation entry. Enforces the confidence guard: any
        close/drop/supersede below threshold is coerced to `reinforced` (no
        state change, just bumps the clock and records the attempt). Always
        appends an atom_events row. Text is never mutated."""
        now = time.time()
        coerced = False
        if action in ("closed", "dropped", "superseded"):
            c = confidence if confidence is not None else 0.0
            if c < CONFIDENCE_CLOSE_THRESHOLD:
                # Until nudges exist, low-confidence terminations are reinforces.
                log.info("atom action coerced to reinforced (below threshold)",
                         id=aid, action=action, confidence=c,
                         threshold=CONFIDENCE_CLOSE_THRESHOLD)
                action = "reinforced"
                state = None
                superseded_by = None
                coerced = True

        if action == "reinforced":
            self._conn.execute("UPDATE atoms SET updated_at = ? WHERE id = ?", (now, aid))
        elif action in ("closed", "dropped", "superseded") and state:
            cols = ["state = ?", "updated_at = ?"]
            vals: list[Any] = [state, now]
            if superseded_by is not None:
                cols.append("superseded_by = ?"); vals.append(int(superseded_by))
            vals.append(int(aid))
            self._conn.execute(f"UPDATE atoms SET {', '.join(cols)} WHERE id = ?", vals)

        self._conn.execute(
            "INSERT INTO atom_events(atom_id, ts, source, snippet, action, confidence) VALUES (?,?,?,?,?,?)",
            (int(aid), now, source, evidence, action, confidence),
        )
        self._conn.commit()
        return {"ok": True, "action_applied": action, "coerced": coerced}

    def list(self, state: str | None = None, kind: str | None = None,
             limit: int = 200) -> list[dict[str, Any]]:
        cols = ("id, text, kind, state, owner, evidence, source, "
                "completion_signal, superseded_by, created_at, updated_at")
        where, params = [], []
        if state:
            where.append("state = ?"); params.append(state)
        if kind:
            where.append("kind = ?"); params.append(kind)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        rows = self._conn.execute(
            f"SELECT {cols} FROM atoms {clause} ORDER BY updated_at DESC LIMIT ?", params
        ).fetchall()
        keys = cols.split(", ")
        return [dict(zip(keys, r)) for r in rows]

    def count_working(self) -> int:
        """The number that belongs on /v1/status atoms_open. Strict filter:
        live state, working kind, owned (not held-as-unclear)."""
        placeholders_states = ",".join("?" * len(WORKING_STATES))
        placeholders_kinds = ",".join("?" * len(WORKING_KINDS))
        return self._conn.execute(
            f"SELECT COUNT(*) FROM atoms "
            f"WHERE state IN ({placeholders_states}) "
            f"AND kind IN ({placeholders_kinds}) "
            f"AND owner IS NOT NULL AND owner != 'unclear'",
            (*WORKING_STATES, *WORKING_KINDS),
        ).fetchone()[0]

    def count(self, state: str | None = "active") -> int:
        if state:
            return self._conn.execute("SELECT COUNT(*) FROM atoms WHERE state = ?", (state,)).fetchone()[0]
        return self._conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]

    def delete(self, aid: int) -> bool:
        cur = self._conn.execute("DELETE FROM atoms WHERE id = ?", (int(aid),))
        self._conn.execute("DELETE FROM atom_events WHERE atom_id = ?", (int(aid),))
        self._conn.commit()
        return cur.rowcount > 0

    def link_to_conversation(self, conversation_id: int, since: float, until: float | None = None) -> int:
        """Backfill conversation_id on every atom created in [since, until]
        that doesn't already belong to a conversation. Called when the
        observer closes a conversation, so atoms born during that window
        get retroactively linked to it."""
        upper = until if until is not None else time.time() + 1
        cur = self._conn.execute(
            "UPDATE atoms SET conversation_id = ? "
            "WHERE conversation_id IS NULL AND created_at >= ? AND created_at <= ?",
            (int(conversation_id), float(since), float(upper)),
        )
        self._conn.commit()
        return cur.rowcount

    def wipe(self) -> int:
        """Nuke the store. Used to clear pre-v2 spike data."""
        n = self._conn.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
        self._conn.execute("DELETE FROM atoms")
        self._conn.execute("DELETE FROM atom_events")
        self._conn.commit()
        log.info("atom store wiped", count=n)
        return n
