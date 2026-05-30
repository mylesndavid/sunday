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
import glob
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote

CHAT_DB = Path("~/Library/Messages/chat.db").expanduser()
# Cocoa absolute time epoch = 2001-01-01 00:00:00 UTC.
MAC_EPOCH_OFFSET_SECONDS = 978_307_200


def is_available() -> bool:
    """True when this machine can serve iMessage tools (macOS + chat.db readable)."""
    return CHAT_DB.exists()


def _mac_ns_to_unix_seconds(mac_ns: int) -> float:
    return (mac_ns / 1_000_000_000.0) + MAC_EPOCH_OFFSET_SECONDS


# ── Contact name resolution ────────────────────────────────────────────────
# Phone numbers / emails → real names, from the macOS Contacts (AddressBook)
# SQLite DBs. Cached for the process. Needs Full Disk Access (same as chat.db).
_CONTACT_MAP: dict[str, str] | None = None


def _normalize_phone(s: str) -> str:
    """Last 10 digits — collapses +1 / formatting so numbers match."""
    digits = "".join(ch for ch in (s or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _build_contact_map() -> dict[str, str]:
    mapping: dict[str, str] = {}
    bases = [
        str(Path("~/Library/Application Support/AddressBook/AddressBook-v22.abcddb").expanduser()),
        *glob.glob(str(Path("~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb").expanduser())),
    ]
    for db in bases:
        if not Path(db).exists():
            continue
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            continue
        try:
            people: dict[int, str] = {}
            for rid, fn, ln, org in c.execute(
                "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION FROM ZABCDRECORD"
            ):
                name = " ".join(p for p in [fn, ln] if p) or (org or "")
                if name:
                    people[rid] = name.strip()
            for owner, num in c.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                if owner in people and num:
                    key = _normalize_phone(num)
                    if key:
                        mapping.setdefault(key, people[owner])
            for owner, addr in c.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                if owner in people and addr:
                    mapping.setdefault(addr.lower().strip(), people[owner])
        except sqlite3.OperationalError:
            pass
        finally:
            c.close()
    return mapping


def _contact_map() -> dict[str, str]:
    global _CONTACT_MAP
    if _CONTACT_MAP is None:
        try:
            _CONTACT_MAP = _build_contact_map()
        except Exception:  # noqa: BLE001
            _CONTACT_MAP = {}
    return _CONTACT_MAP


def _resolve_name(handle: str | None) -> str | None:
    """A phone/email handle → a contact name when we have one, else the handle."""
    if not handle:
        return handle
    m = _contact_map()
    if "@" in handle:
        return m.get(handle.lower().strip(), handle)
    key = _normalize_phone(handle)
    return m.get(key, handle) if key else handle


# ── Rich content (links, locations, events) ────────────────────────────────
# When a message carries a balloon (shared URL / location / event), the real
# content lives in payload_data, not the text. Decode it to a readable line so
# Sunday sees "Shared a Luma event: Bugs and Beer @ Transpose" — not "(attachment)".
_SKIP_URL_BITS = ("favicon", "apple-touch-icon", ".png", ".jpg", ".jpeg", ".gif",
                  ".ico", "/cdn-cgi/", "images.", "lumacdn", "og.luma", "/avatars/")


def _decode_balloon(bundle_id: str | None, payload: bytes | None) -> dict[str, Any] | None:
    if not payload:
        return None
    try:
        text = payload.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        text = str(payload)
    urls = re.findall(r'https?://[^\s"\'<>\\)￼�]+', text)
    primary = None
    for u in urls:
        if not any(b in u.lower() for b in _SKIP_URL_BITS):
            primary = u
            break
    primary = primary or (urls[0] if urls else None)

    # A title often rides in a ?name= param (link-preview cards) or as readable text.
    title = None
    mt = re.search(r'[?&]name=([^&"\s]+)', text)
    if mt:
        title = unquote(mt.group(1)).replace("+", " ").strip()

    bid = (bundle_id or "")
    if "Maps" in bid or "location" in bid.lower():
        return {"kind": "location",
                "summary": "📍 Shared location" + (f": {title}" if title else "") + (f" ({primary})" if primary else "")}
    if "PeerPayment" in bid or "Passbook" in bid:
        return {"kind": "payment", "summary": "💸 Apple Cash / payment"}
    if primary:
        host = re.sub(r"^https?://(www\.)?", "", primary).split("/")[0]
        label = title or host
        return {"kind": "link", "url": primary, "title": title,
                "summary": f"🔗 {label} — {primary}" if title else f"🔗 {primary}"}
    return None


# Tapback reactions: associated_message_type encodes which one (2000s add, 3000s remove).
_TAPBACKS = {
    2000: "❤️ Loved", 2001: "👍 Liked", 2002: "👎 Disliked",
    2003: "😂 Laughed at", 2004: "‼️ Emphasized", 2005: "❓ Questioned",
}


def _tapback_label(assoc_type: int | None) -> str | None:
    if not assoc_type:
        return None
    if assoc_type in _TAPBACKS:
        return f"{_TAPBACKS[assoc_type]} a message"
    if 3000 <= assoc_type < 4000:
        return "Removed a reaction"
    return None


def _describe_attachment(att: dict[str, Any]) -> str:
    mime = (att.get("mime_type") or "").lower()
    name = att.get("filename") or "file"
    if mime.startswith("image/"):
        return f"🖼 image ({name})"
    if mime.startswith("video/"):
        return f"🎬 video ({name})"
    if mime.startswith("audio/"):
        return "🎙 audio message"
    return f"📎 {name}"


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


def _clean_text(raw: str | None) -> str:
    # iMessage embeds U+FFFC (object replacement) where inline attachments sit, and
    # U+FFFD shows up from binary noise. Strip both so previews read clean.
    return (raw or "").replace("￼", "").replace("�", "").strip()


def _compose_text(raw: str | None, bundle_id: str | None,
                  payload: bytes | None, atts: list[dict[str, Any]] | None) -> str:
    """The most readable one-line rendering of a message: text + rich content + attachments."""
    base = _clean_text(raw)
    rich = _decode_balloon(bundle_id, payload)
    if rich:
        url = rich.get("url")
        if not base or (url and base == url.strip()):
            return rich["summary"]
        return f"{base}  ·  {rich['summary']}"
    if base:
        return base
    if atts:
        return "(" + ", ".join(_describe_attachment(a) for a in atts) + ")"
    return ""


def _thread_participants(conn: sqlite3.Connection, chat_rowid: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT h.id FROM handle h
        JOIN chat_handle_join chj ON chj.handle_id = h.ROWID
        WHERE chj.chat_id = ?
        """,
        (chat_rowid,),
    ).fetchall()
    return [_resolve_name(r[0]) or r[0] for r in rows if r[0]]


def _chat_label(conn: sqlite3.Connection, chat_rowid: int,
                display_name: str | None, chat_identifier: str) -> str:
    """Best human name for a chat: explicit group name, contact name, or built from members."""
    if display_name:
        return display_name
    parts = _thread_participants(conn, chat_rowid)
    if len(parts) == 1:
        return parts[0]
    if parts:
        first = ", ".join(p.split()[0] for p in parts[:2])
        extra = len(parts) - 2
        return first + (f" & {extra} more" if extra > 0 else "")
    return _resolve_name(chat_identifier) or chat_identifier


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
                last_msg.date,
                last_msg.balloon_bundle_id,
                last_msg.payload_data
            FROM chat c
            JOIN (
                SELECT cmj.chat_id, m.text, m.is_from_me, m.date,
                       m.balloon_bundle_id, m.payload_data,
                       ROW_NUMBER() OVER (PARTITION BY cmj.chat_id ORDER BY m.date DESC) AS rn
                FROM message m
                JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            ) last_msg ON last_msg.chat_id = c.ROWID AND last_msg.rn = 1
            ORDER BY last_msg.date DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            participants = _thread_participants(conn, row[0])
            is_group = len(participants) > 1 or bool(row[2])
            name = _chat_label(conn, row[0], row[2], row[1])
            preview = _compose_text(row[3], row[6], row[7], None)
            if not preview:
                last_id = conn.execute(
                    "SELECT MAX(m.ROWID) FROM message m "
                    "JOIN chat_message_join cmj ON cmj.message_id = m.ROWID WHERE cmj.chat_id = ?",
                    (row[0],),
                ).fetchone()
                atts = _attachments_for(conn, last_id[0]) if last_id and last_id[0] else []
                preview = "(" + ", ".join(_describe_attachment(a) for a in atts) + ")" if atts else "(no preview)"
            out.append({
                "chat_id": row[0],
                "chat_identifier": row[1],
                "display_name": name,
                "participants": participants,
                "is_group": is_group,
                "last_text": preview,
                "last_from_me": bool(row[4]),
                "last_date": _mac_ns_to_unix_seconds(row[5] or 0),
            })
    finally:
        conn.close()
    return out


def read_thread(chat_identifier: str, limit: int = 30) -> list[dict[str, Any]]:
    conn = _connect_read_only()
    try:
        rows = conn.execute(
            """
            SELECT m.ROWID, m.text, m.is_from_me, m.date, h.id AS handle,
                   m.balloon_bundle_id, m.payload_data, m.associated_message_type
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
            handle = row[4]
            text = _compose_text(row[1], row[5], row[6], atts)
            tap = _tapback_label(row[7])
            if tap:
                text = tap
            if not text:
                continue  # empty system rows (expired audio, edits) — skip the noise
            out.append({
                "rowid": row[0],
                "text": text,
                "is_from_me": bool(row[2]),
                "date": _mac_ns_to_unix_seconds(row[3] or 0),
                "handle": handle,
                "sender": "me" if row[2] else (_resolve_name(handle) or handle),
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
            SELECT m.ROWID, m.text, m.is_from_me, m.date, h.id, c.chat_identifier,
                   c.display_name, m.balloon_bundle_id, m.payload_data,
                   m.associated_message_type, c.ROWID
            FROM message m
            LEFT JOIN handle h ON h.ROWID = m.handle_id
            JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
            JOIN chat c ON c.ROWID = cmj.chat_id
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (limit * 3,),  # over-fetch; we drop empty/system rows then trim
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            atts = _attachments_for(conn, row[0])
            handle = row[4]
            text = _compose_text(row[1], row[7], row[8], atts)
            tap = _tapback_label(row[9])
            if tap:
                text = tap
            if not text:
                continue
            sender = "me" if row[2] else (_resolve_name(handle) or handle)
            chat_name = _chat_label(conn, row[10], row[6], row[5])
            out.append({
                "text": text,
                "is_from_me": bool(row[2]),
                "date": _mac_ns_to_unix_seconds(row[3] or 0),
                "handle": handle,
                "sender": sender,
                "chat_identifier": row[5],
                "display_name": chat_name,
            })
            if len(out) >= limit:
                break
    finally:
        conn.close()
    return out


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
