"""Local macOS iMessage integration.

Reads the user's actual Messages.app conversations via the SQLite database
at ~/Library/Messages/chat.db, and sends new iMessages through Messages.app
via AppleScript. This is "Sunday can see and respond to your iMessages,"
not "Sunday gets her own phone number" (that's channels/sendblue.py).

Requirements:
  - Full Disk Access for the process running the daemon
    (System Settings → Privacy & Security → Full Disk Access → add Terminal,
     or the Sunday daemon binary, or your IDE if running from there).
  - macOS Messages.app signed in with your iMessage account.

Read access is read-only (sqlite ?mode=ro). Sending goes through
Messages.app via osascript, so it appears in your own Messages history
exactly as if you typed it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.messages_local")

CHAT_DB = Path("~/Library/Messages/chat.db").expanduser()
# Cocoa absolute time epoch = 2001-01-01 00:00:00 UTC.
# message.date is nanoseconds since that epoch on modern macOS.
MAC_EPOCH_OFFSET_SECONDS = 978_307_200


def _mac_ns_to_unix_seconds(mac_ns: int) -> float:
    return (mac_ns / 1_000_000_000.0) + MAC_EPOCH_OFFSET_SECONDS


def _connect_read_only() -> sqlite3.Connection:
    if not CHAT_DB.exists():
        raise RuntimeError(
            f"Messages chat.db not found at {CHAT_DB}. "
            "Make sure Messages.app is signed in."
        )
    try:
        return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Could not open Messages chat.db: {exc}. "
            "This usually means the daemon doesn't have Full Disk Access. "
            "Open System Settings → Privacy & Security → Full Disk Access "
            "and add the process running Sunday (Terminal, IDE, or the daemon)."
        ) from exc


def list_threads(limit: int = 20) -> list[dict[str, Any]]:
    """Recent conversations, newest first, with last-message preview."""
    conn = _connect_read_only()
    try:
        rows = conn.execute(
            """
            SELECT
                c.ROWID,
                c.chat_identifier,
                c.display_name,
                last_msg.text,
                last_msg.is_from_me,
                last_msg.date
            FROM chat c
            JOIN (
                SELECT cmj.chat_id, m.text, m.is_from_me, m.date,
                       ROW_NUMBER() OVER (PARTITION BY cmj.chat_id ORDER BY m.date DESC) AS rn
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            ) last_msg ON last_msg.chat_id = c.ROWID AND last_msg.rn = 1
            ORDER BY last_msg.date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "chat_id": row[0],
            "chat_identifier": row[1],
            "display_name": row[2] or row[1],
            "last_text": row[3] or "(attachment)",
            "last_from_me": bool(row[4]),
            "last_date": _mac_ns_to_unix_seconds(row[5] or 0),
        }
        for row in rows
    ]


def read_thread(chat_identifier: str, limit: int = 30) -> list[dict[str, Any]]:
    """Messages in one conversation, oldest first."""
    conn = _connect_read_only()
    try:
        rows = conn.execute(
            """
            SELECT m.text, m.is_from_me, m.date, h.id AS handle
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            WHERE c.chat_identifier = ?
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (chat_identifier, limit),
        ).fetchall()
    finally:
        conn.close()
    rows.reverse()
    return [
        {
            "text": row[0] or "(attachment)",
            "is_from_me": bool(row[1]),
            "date": _mac_ns_to_unix_seconds(row[2] or 0),
            "handle": row[3],
        }
        for row in rows
    ]


def read_recent(limit: int = 20) -> list[dict[str, Any]]:
    """Most recent messages across all threads."""
    conn = _connect_read_only()
    try:
        rows = conn.execute(
            """
            SELECT m.text, m.is_from_me, m.date, h.id, c.chat_identifier, c.display_name
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "text": row[0] or "(attachment)",
            "is_from_me": bool(row[1]),
            "date": _mac_ns_to_unix_seconds(row[2] or 0),
            "handle": row[3],
            "chat_identifier": row[4],
            "display_name": row[5] or row[4],
        }
        for row in rows
    ]


async def send_imessage(to: str, body: str) -> dict[str, Any]:
    """Send an iMessage through the user's Messages.app via AppleScript.

    `to` should be a phone number (E.164: '+15551234567') or email used
    as an iMessage handle. For group chats, prefer chat GUIDs — see
    send_to_chat (not yet implemented in v0.1).
    """
    safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
    safe_to = to.replace('"', '\\"')
    script = (
        'tell application "Messages"\n'
        '    set targetService to 1st service whose service type = iMessage\n'
        f'    set targetBuddy to buddy "{safe_to}" of targetService\n'
        f'    send "{safe_body}" to targetBuddy\n'
        'end tell'
    )
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    if proc.returncode != 0:
        return {
            "error": err_b.decode("utf-8", errors="replace").strip()
            or out_b.decode("utf-8", errors="replace").strip()
            or f"osascript exited {proc.returncode}"
        }
    return {"ok": True, "to": to}


# ─── tool registrations ──────────────────────────────────────────────────


_LIST_THREADS_PARAMS = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "default": 20, "description": "Max threads to return."}
    },
}

_READ_THREAD_PARAMS = {
    "type": "object",
    "properties": {
        "chat_identifier": {
            "type": "string",
            "description": "The chat_identifier from imessage_list_threads (a phone number, email, or chat GUID).",
        },
        "limit": {"type": "integer", "default": 30, "description": "Max messages to return."},
    },
    "required": ["chat_identifier"],
}

_READ_RECENT_PARAMS = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "default": 20, "description": "Max messages to return across all threads."}
    },
}

_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {
            "type": "string",
            "description": "Recipient handle: phone number in E.164 ('+15551234567') or email tied to iMessage.",
        },
        "body": {"type": "string", "description": "Message body."},
    },
    "required": ["to", "body"],
}


async def _t_list_threads(args: dict[str, Any], ctx: ToolContext) -> Any:
    try:
        return {"threads": list_threads(int(args.get("limit") or 20))}
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_read_thread(args: dict[str, Any], ctx: ToolContext) -> Any:
    ident = args.get("chat_identifier")
    if not ident:
        return {"error": "'chat_identifier' is required"}
    try:
        return {"messages": read_thread(str(ident), int(args.get("limit") or 30))}
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_read_recent(args: dict[str, Any], ctx: ToolContext) -> Any:
    try:
        return {"messages": read_recent(int(args.get("limit") or 20))}
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    body = args.get("body")
    if not to or not body:
        return {"error": "'to' and 'body' are required"}
    return await send_imessage(str(to), str(body))


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="imessage_list_threads",
        description="List your recent iMessage conversations from Messages.app, newest first.",
        parameters=_LIST_THREADS_PARAMS,
        run=_t_list_threads,
    ))
    registry.register(Tool(
        name="imessage_read_thread",
        description="Read messages in a specific iMessage thread by chat_identifier.",
        parameters=_READ_THREAD_PARAMS,
        run=_t_read_thread,
    ))
    registry.register(Tool(
        name="imessage_read_recent",
        description="Read the most recent iMessages across all the user's threads.",
        parameters=_READ_RECENT_PARAMS,
        run=_t_read_recent,
    ))
    registry.register(Tool(
        name="imessage_send",
        description=(
            "Send an iMessage from the user's Messages.app to a specific handle "
            "(phone or email). The message appears in the user's iMessage history."
        ),
        parameters=_SEND_PARAMS,
        run=_t_send,
    ))
