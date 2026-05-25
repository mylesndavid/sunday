"""Local macOS iMessage access — runs ON the satellite, NOT the central brain.

A Sunday satellite running on a Mac with Messages.app signed in advertises
the `imessage` capability. The central brain proxies imessage_* tool calls
through that satellite via the device protocol — so a Sunday server in a
datacenter can still read + reply to your real iMessages, because the
actual chat.db read and the AppleScript send both happen on YOUR Mac.

Requirements:
  - macOS with Messages.app signed in
  - Full Disk Access for the process running `sunday-satellite`
    (System Settings → Privacy & Security → Full Disk Access)
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

CHAT_DB = Path("~/Library/Messages/chat.db").expanduser()
# Cocoa absolute time epoch = 2001-01-01 00:00:00 UTC.
MAC_EPOCH_OFFSET_SECONDS = 978_307_200


def is_available() -> bool:
    """True when this machine can serve iMessage tools (macOS + chat.db readable)."""
    return CHAT_DB.exists()


def _mac_ns_to_unix_seconds(mac_ns: int) -> float:
    return (mac_ns / 1_000_000_000.0) + MAC_EPOCH_OFFSET_SECONDS


def _connect_read_only() -> sqlite3.Connection:
    if not CHAT_DB.exists():
        raise RuntimeError(
            f"Messages chat.db not found at {CHAT_DB}. "
            "Make sure this satellite runs on a Mac with Messages.app signed in."
        )
    try:
        return sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Could not open Messages chat.db: {exc}. "
            "This usually means the satellite process doesn't have Full Disk Access."
        ) from exc


def _attachments_for(conn: sqlite3.Connection, message_rowid: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.filename, a.mime_type, a.transfer_name, a.uti, a.total_bytes
        FROM attachment a
        JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
        WHERE maj.message_id = ?
        """,
        (message_rowid,),
    ).fetchall()
    return [
        {
            "path": (row[0] or "").replace("~", str(Path.home())) if row[0] else "",
            "mime_type": row[1] or "application/octet-stream",
            "filename": row[2] or (Path(row[0]).name if row[0] else "attachment"),
            "uti": row[3],
            "size": int(row[4] or 0),
        }
        for row in rows
    ]


def list_threads(limit: int = 20) -> list[dict[str, Any]]:
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
    conn = _connect_read_only()
    try:
        rows = conn.execute(
            """
            SELECT m.ROWID, m.text, m.is_from_me, m.date, h.id AS handle
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
        out: list[dict[str, Any]] = []
        for row in rows:
            atts = _attachments_for(conn, row[0])
            out.append({
                "rowid": row[0],
                "text": row[1] or ("(attachment)" if atts else ""),
                "is_from_me": bool(row[2]),
                "date": _mac_ns_to_unix_seconds(row[3] or 0),
                "handle": row[4],
                "attachments": atts,
            })
    finally:
        conn.close()
    out.reverse()
    return out


def read_recent(limit: int = 20) -> list[dict[str, Any]]:
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


async def _run_osascript(script: str) -> dict[str, Any]:
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
    return {"ok": True}


async def send_imessage(to: str, body: str, attachments: list[str] | None = None) -> dict[str, Any]:
    """Send through Messages.app via AppleScript so it lands in the user's own history."""
    safe_to = to.replace('"', '\\"')

    if body:
        safe_body = body.replace("\\", "\\\\").replace('"', '\\"')
        body_script = (
            'tell application "Messages"\n'
            '    set targetService to 1st service whose service type = iMessage\n'
            f'    set targetBuddy to buddy "{safe_to}" of targetService\n'
            f'    send "{safe_body}" to targetBuddy\n'
            'end tell'
        )
        result = await _run_osascript(body_script)
        if "error" in result:
            return result

    for att_path in attachments or []:
        p = Path(att_path).expanduser().resolve()
        if not p.exists():
            return {"error": f"attachment missing: {p}"}
        safe_path = str(p).replace('"', '\\"')
        att_script = (
            'tell application "Messages"\n'
            '    set targetService to 1st service whose service type = iMessage\n'
            f'    set targetBuddy to buddy "{safe_to}" of targetService\n'
            f'    send POSIX file "{safe_path}" to targetBuddy\n'
            'end tell'
        )
        result = await _run_osascript(att_script)
        if "error" in result:
            return result

    return {"ok": True, "to": to, "attached": len(attachments or [])}
