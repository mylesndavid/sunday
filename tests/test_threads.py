"""Slack-style threads — the safety-critical bits.

This feature touches the user's real chat DB, so the migration is the part that
matters most: it must be additive, idempotent, and never rewrite or delete an
existing message. The rest verifies the Slack semantic — a thread is a scoped
side-bar that doesn't leak into the main chat and doesn't drag the main chat
into itself — and the CRUD/scoping the endpoints lean on.
"""

from __future__ import annotations

import sqlite3

import pytest

from sunday.brain import _context_messages, _thread_tail
from sunday.chat import Chat, _migrate


# ─── migration safety: additive, idempotent, non-destructive ────────────────

def _legacy_db(path) -> list[tuple]:
    """A pre-threads DB: the OLD messages schema (no thread_id) with real rows.
    Returns the exact rows we wrote so we can prove they're untouched later."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          role        TEXT NOT NULL,
          modality    TEXT NOT NULL,
          content     TEXT NOT NULL,
          created_at  REAL NOT NULL,
          metadata    TEXT
        );
        CREATE INDEX idx_messages_created_at ON messages(created_at);
        """
    )
    rows = [
        ("user", "cli", "first thing i said", 100.0, None),
        ("sunday", "cli", "first reply", 101.0, '{"runtime": "x"}'),
        ("user", "electron", "second thing", 200.0, None),
        ("sunday", "electron", "second reply", 201.0, None),
    ]
    conn.executemany(
        "INSERT INTO messages (role, modality, content, created_at, metadata) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    conn.commit()
    snapshot = conn.execute(
        "SELECT id, role, modality, content, created_at, metadata FROM messages ORDER BY id"
    ).fetchall()
    conn.close()
    return snapshot


def _snapshot(path) -> list[tuple]:
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT id, role, modality, content, created_at, metadata FROM messages ORDER BY id"
    ).fetchall()
    conn.close()
    return rows


def test_migration_adds_column_and_table(tmp_path):
    p = tmp_path / "sunday.db"
    _legacy_db(p)
    conn = sqlite3.connect(p)
    _migrate(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "thread_id" in cols
    # The chat table opener also creates the threads table via SCHEMA; the
    # migration itself only guarantees thread_id + its index, so create the
    # table the way Chat() does to confirm the whole shape is reachable.
    conn.close()
    chat = Chat(path=p)
    tables = {r[0] for r in chat._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "threads" in tables


def test_migration_is_idempotent_and_nondestructive(tmp_path):
    p = tmp_path / "sunday.db"
    before = _legacy_db(p)

    # Run the migration TWICE — it must not raise and must not change any row.
    conn = sqlite3.connect(p)
    _migrate(conn)
    _migrate(conn)
    conn.close()

    after = _snapshot(p)
    assert after == before, "existing messages were altered by the migration"

    # Opening Chat() (which runs SCHEMA + migrate again) is also a no-op on rows.
    chat = Chat(path=p)
    rows = chat._conn.execute(
        "SELECT id, role, modality, content, created_at, metadata FROM messages ORDER BY id"
    ).fetchall()
    assert rows == before
    # And every pre-existing row is on the main timeline (NULL thread_id).
    tids = [r[0] for r in chat._conn.execute(
        "SELECT thread_id FROM messages ORDER BY id").fetchall()]
    assert tids == [None, None, None, None]


def test_existing_messages_read_unchanged_after_migration(tmp_path):
    p = tmp_path / "sunday.db"
    _legacy_db(p)
    chat = Chat(path=p)
    recent = chat.recent(limit=50)
    assert [m.content for m in recent] == [
        "first thing i said", "first reply", "second thing", "second reply",
    ]
    assert all(m.thread_id is None for m in recent)


def test_fresh_db_has_threads_support(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    cols = {r[1] for r in chat._conn.execute(
        "PRAGMA table_info(messages)").fetchall()}
    assert "thread_id" in cols


# ─── Slack semantics: scoping in both directions ────────────────────────────

def _seed(chat: Chat) -> int:
    """A short main chat with one threaded side-bar. Returns the thread id."""
    chat.append("user", "main question", "electron")
    root = chat.append("sunday", "main answer", "electron")
    chat.append("user", "next main thing", "electron")
    tid = chat.create_thread(root, title="side discussion")
    chat.append("user", "thread question", "electron", thread_id=tid)
    chat.append("sunday", "thread answer", "electron", thread_id=tid)
    return tid


def test_main_timeline_excludes_thread_replies(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    _seed(chat)
    contents = [m.content for m in chat.recent(limit=50)]
    assert contents == ["main question", "main answer", "next main thing"]
    assert "thread question" not in contents
    assert "thread answer" not in contents


def test_thread_messages_are_scoped(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    tid = _seed(chat)
    replies = chat.thread_messages(tid)
    assert [m.content for m in replies] == ["thread question", "thread answer"]
    assert all(m.thread_id == tid for m in replies)


def test_main_turn_context_is_main_only(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    _seed(chat)
    msgs = _context_messages(chat, memory_block="", thread_id=None)
    blob = " ".join(str(m.get("content")) for m in msgs)
    assert "main question" in blob
    assert "thread question" not in blob
    assert "thread answer" not in blob


def test_thread_turn_context_sees_thread_and_root(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    tid = _seed(chat)
    msgs = _context_messages(chat, memory_block="", thread_id=tid)
    blob = " ".join(str(m.get("content")) for m in msgs)
    # The thread's replies AND its root anchor are present...
    assert "thread question" in blob
    assert "thread answer" in blob
    assert "main answer" in blob  # the root message
    # ...but unrelated main-timeline turns are NOT dragged in.
    assert "next main thing" not in blob
    assert "main question" not in blob


def test_thread_tail_keeps_root_first(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    tid = _seed(chat)
    tail = _thread_tail(chat, tid)
    assert tail[0].content == "main answer"  # the root, first
    assert tail[-1].content == "thread answer"


# ─── thread CRUD + counts (what the endpoints lean on) ───────────────────────

def test_create_thread_is_idempotent_per_root(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    root = chat.append("sunday", "rootable", "electron")
    a = chat.create_thread(root)
    b = chat.create_thread(root)  # second call must not fork
    assert a == b
    assert len(chat.list_threads()) == 1


def test_list_threads_orders_by_recency_with_previews(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    r1 = chat.append("sunday", "root one", "electron")
    r2 = chat.append("sunday", "root two", "electron")
    t1 = chat.create_thread(r1)
    t2 = chat.create_thread(r2)
    chat.append("user", "ping t1", "electron", thread_id=t1)  # t1 most recent now
    threads = chat.list_threads()
    assert threads[0]["id"] == t1
    assert threads[0]["reply_count"] == 1
    assert threads[0]["root_preview"] == "root one"
    assert threads[1]["id"] == t2
    assert threads[1]["reply_count"] == 0


def test_reply_counts_keyed_by_root(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    root = chat.append("sunday", "answer", "electron")
    tid = chat.create_thread(root)
    chat.append("user", "q1", "electron", thread_id=tid)
    chat.append("sunday", "a1", "electron", thread_id=tid)
    counts = chat.reply_counts()
    assert counts == {root: 2}


def test_thread_for_message(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    root = chat.append("sunday", "answer", "electron")
    assert chat.thread_for_message(root) is None
    tid = chat.create_thread(root)
    assert chat.thread_for_message(root) == tid


def test_get_thread_reports_reply_count(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    root = chat.append("sunday", "answer", "electron")
    tid = chat.create_thread(root)
    chat.append("user", "q", "electron", thread_id=tid)
    th = chat.get_thread(tid)
    assert th["root_message_id"] == root
    assert th["reply_count"] == 1


def test_appending_reply_warms_thread_last_active(tmp_path):
    chat = Chat(path=tmp_path / "sunday.db")
    root = chat.append("sunday", "answer", "electron")
    tid = chat.create_thread(root)
    before = chat.get_thread(tid)["last_active_at"]
    chat.append("user", "later", "electron", thread_id=tid)
    after = chat.get_thread(tid)["last_active_at"]
    assert after >= before
