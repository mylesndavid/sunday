"""Memory graph — a navigable map of who/what Sunday knows about you.

Sunday's memory is a flat list of facts (see memory.py). This module turns
that list into a graph: it asks the model to pull the people, places,
pets, projects, and things each fact mentions, plus how they relate, then
stores those as nodes + edges in tables alongside the memories DB. The
desktop app renders it as an interactive graph you can click through.

Kept deliberately simple: with a personal memory the fact count is small,
so we rebuild the whole graph in one model call rather than maintaining
incremental diffs. A state row tracks the max fact id + count seen, so we
skip the call when nothing changed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.memory_graph")

# Friendly, user-facing categories. The model is told to use exactly these
# keys; the frontend maps each to a label + color. No "entity/edge" jargon
# ever reaches the UI — these are just buckets.
KINDS = ["person", "pet", "place", "organization", "project", "thing", "event", "topic"]

_EXTRACT_SYSTEM = (
    "You build a personal knowledge map from facts about a user. Given a "
    "numbered list of facts, identify the distinct things mentioned and how "
    "they connect.\n\n"
    "Return ONLY JSON of this exact shape:\n"
    "{\n"
    '  "nodes": [{"name": "Biscuit", "kind": "pet"}],\n'
    '  "links": [{"from": "You", "to": "Biscuit", "label": "has pet", "fact": 12}]\n'
    "}\n\n"
    f"Rules:\n"
    f"- kind must be one of: {', '.join(KINDS)}.\n"
    "- Always include a node named \"You\" (kind person) for the user; connect "
    "  facts about the user to it.\n"
    "- Merge duplicates: same real-world thing = one node, canonical name.\n"
    "- label is a short verb phrase (\"works at\", \"lives in\", \"friend of\", "
    "  \"working on\"). fact is the number of the fact the link came from.\n"
    "- Only things actually present in the facts. No invention.\n"
    "- Output JSON only, no prose, no code fence."
)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(sunday_home() / "memories.db", check_same_thread=False)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mem_nodes (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE,
            kind  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mem_links (
            a_id   INTEGER NOT NULL,
            b_id   INTEGER NOT NULL,
            label  TEXT,
            fact_id INTEGER,
            UNIQUE(a_id, b_id, label)
        );
        CREATE TABLE IF NOT EXISTS mem_node_facts (
            node_id INTEGER NOT NULL,
            fact_id INTEGER NOT NULL,
            UNIQUE(node_id, fact_id)
        );
        CREATE TABLE IF NOT EXISTS mem_graph_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            max_fact_id INTEGER NOT NULL DEFAULT 0,
            fact_count  INTEGER NOT NULL DEFAULT 0,
            built_at    REAL NOT NULL DEFAULT 0
        );
        """
    )
    conn.commit()
    return conn


def _facts(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    try:
        return conn.execute("SELECT id, content FROM memories ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []


def _state(conn: sqlite3.Connection) -> tuple[int, int]:
    row = conn.execute("SELECT max_fact_id, fact_count FROM mem_graph_state WHERE id = 1").fetchone()
    return (row[0], row[1]) if row else (0, 0)


def needs_rebuild(conn: sqlite3.Connection | None = None) -> bool:
    own = conn is None
    conn = conn or _db()
    try:
        facts = _facts(conn)
        max_id = facts[-1][0] if facts else 0
        seen_max, seen_count = _state(conn)
        return (max_id != seen_max) or (len(facts) != seen_count)
    finally:
        if own:
            conn.close()


async def rebuild(config: SundayConfig, force: bool = False) -> dict[str, Any]:
    """(Re)extract the whole graph from the current facts. Cheap: one model
    call. Skips when nothing changed unless force=True."""
    conn = _db()
    try:
        facts = _facts(conn)
        if not facts:
            return {"nodes": [], "links": [], "empty": True}
        if not force and not needs_rebuild(conn):
            return _read_graph(conn)

        numbered = "\n".join(f"{fid}. {text}" for fid, text in facts)
        from sunday.runtime import build_runtime
        rt = build_runtime(config)
        result = await rt.complete(
            system_prompt=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": f"Facts:\n{numbered}\n\nJSON map:"}],
            tools_schema=None,
        )
        parsed = _parse(result.content or "")
        _write(conn, facts, parsed)
        max_id = facts[-1][0]
        conn.execute(
            "INSERT INTO mem_graph_state (id, max_fact_id, fact_count, built_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET max_fact_id=excluded.max_fact_id, "
            "fact_count=excluded.fact_count, built_at=excluded.built_at",
            (max_id, len(facts), time.time()),
        )
        conn.commit()
        log.info("memory graph rebuilt", nodes=len(parsed.get("nodes", [])), links=len(parsed.get("links", [])))
        return _read_graph(conn)
    finally:
        conn.close()


def _parse(raw: str) -> dict[str, Any]:
    s = raw.strip()
    if s.startswith("```"):
        s = s[s.find("\n") + 1: s.rfind("```")].strip()
    a, b = s.find("{"), s.rfind("}")
    if a >= 0 and b > a:
        s = s[a:b + 1]
    try:
        d = json.loads(s)
    except json.JSONDecodeError:
        log.warning("graph extraction returned non-JSON", preview=s[:120])
        return {"nodes": [], "links": []}
    return {"nodes": d.get("nodes") or [], "links": d.get("links") or []}


def _write(conn: sqlite3.Connection, facts: list[tuple[int, str]], parsed: dict[str, Any]) -> None:
    conn.execute("DELETE FROM mem_nodes")
    conn.execute("DELETE FROM mem_links")
    conn.execute("DELETE FROM mem_node_facts")
    name_to_id: dict[str, int] = {}

    def node_id(name: str, kind: str) -> int:
        key = name.strip()
        if not key:
            return -1
        low = key.lower()
        if low in name_to_id:
            return name_to_id[low]
        kind = kind if kind in KINDS else "thing"
        cur = conn.execute("INSERT OR IGNORE INTO mem_nodes (name, kind) VALUES (?, ?)", (key, kind))
        nid = cur.lastrowid or conn.execute("SELECT id FROM mem_nodes WHERE name = ?", (key,)).fetchone()[0]
        name_to_id[low] = nid
        return nid

    for n in parsed.get("nodes", []):
        if isinstance(n, dict) and n.get("name"):
            node_id(str(n["name"]), str(n.get("kind", "thing")))

    fact_text = {fid: text for fid, text in facts}
    for ln in parsed.get("links", []):
        if not isinstance(ln, dict):
            continue
        a = node_id(str(ln.get("from", "")), "thing")
        b = node_id(str(ln.get("to", "")), "thing")
        if a < 0 or b < 0 or a == b:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO mem_links (a_id, b_id, label, fact_id) VALUES (?, ?, ?, ?)",
            (a, b, str(ln.get("label", "")), ln.get("fact")),
        )
        fid = ln.get("fact")
        if isinstance(fid, int) and fid in fact_text:
            conn.execute("INSERT OR IGNORE INTO mem_node_facts (node_id, fact_id) VALUES (?, ?)", (a, fid))
            conn.execute("INSERT OR IGNORE INTO mem_node_facts (node_id, fact_id) VALUES (?, ?)", (b, fid))


def _read_graph(conn: sqlite3.Connection) -> dict[str, Any]:
    fact_text = {fid: text for fid, text in _facts(conn)}
    nodes = []
    for nid, name, kind in conn.execute("SELECT id, name, kind FROM mem_nodes ORDER BY id"):
        fids = [r[0] for r in conn.execute("SELECT fact_id FROM mem_node_facts WHERE node_id = ?", (nid,))]
        nodes.append({
            "id": nid,
            "name": name,
            "kind": kind,
            "facts": [fact_text[f] for f in fids if f in fact_text],
            "degree": 0,
        })
    by_id = {n["id"]: n for n in nodes}
    links = []
    for a, b, label in conn.execute("SELECT a_id, b_id, label FROM mem_links"):
        if a in by_id and b in by_id:
            links.append({"source": a, "target": b, "label": label or ""})
            by_id[a]["degree"] += 1
            by_id[b]["degree"] += 1
    return {"nodes": nodes, "links": links, "empty": not nodes}


def graph() -> dict[str, Any]:
    conn = _db()
    try:
        return _read_graph(conn)
    finally:
        conn.close()
