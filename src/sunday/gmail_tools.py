"""Direct Gmail connector — IMAP + SMTP over a Google app password.

No OAuth, no Nango: the user turns on 2-step verification, creates an app
password at https://myaccount.google.com/apppasswords, and pastes it (plus
their Gmail address) in Settings → Tools. We read with imaplib (SSL,
imap.gmail.com) and send with smtplib (smtp.gmail.com:465, SSL) — both
stdlib, zero new dependencies.

These tools deliberately share the canonical names gmail_search / gmail_read
/ gmail_send. This module is registered AFTER sunday.integrations.google in
the default registry, so when an app password is configured the direct path
wins (registry.register overwrites by name); when it isn't, the tools still
exist and return a clear "connect Gmail in Settings" error.

All blocking socket I/O runs in asyncio.to_thread so the daemon event loop
never stalls on a slow mailbox.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from typing import Any

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

IMAP_HOST = "imap.gmail.com"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
_IMAP_TIMEOUT = 20
_SMTP_TIMEOUT = 20
_BODY_CAP = 8000

_NOT_CONNECTED = {
    "error": (
        "Gmail isn't connected. Connect Gmail in Settings → Tools (app "
        "password): turn on 2-step verification, create an app password at "
        "https://myaccount.google.com/apppasswords, then paste your Gmail "
        "address and that password."
    )
}


def _creds() -> tuple[str | None, str | None]:
    address = get_credential("GMAIL_ADDRESS")
    password = get_credential("GMAIL_APP_PASSWORD")
    # App passwords are shown with spaces ("abcd efgh ijkl mnop"); Google
    # accepts them with or without, but strip spaces to be safe.
    if password:
        password = password.replace(" ", "")
    return (address or None, password or None)


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — never let a malformed header crash a read
        return value


def _readable_text(msg: email.message.Message) -> str:
    """Pull the best plain-text body out of a parsed message."""
    def _part_text(part: email.message.Message) -> str:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except (LookupError, TypeError):
            return payload.decode("utf-8", errors="replace")

    if msg.is_multipart():
        # Prefer text/plain; fall back to stripped text/html.
        plain = None
        html = None
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain" and plain is None:
                plain = _part_text(part)
            elif ctype == "text/html" and html is None:
                html = _part_text(part)
        if plain:
            return plain
        if html:
            return _strip_html(html)
        return ""
    # single part
    text = _part_text(msg)
    if (msg.get_content_type() or "") == "text/html":
        return _strip_html(text)
    return text


def _strip_html(html: str) -> str:
    import re
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'")
            .replace("&quot;", '"'))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


# ─── blocking workers (run via asyncio.to_thread) ──────────────────────────


def _imap_login(address: str, password: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=_IMAP_TIMEOUT)
    imap.login(address, password)
    return imap


def _do_search(address: str, password: str, query: str, limit: int) -> dict[str, Any]:
    imap = _imap_login(address, password)
    try:
        imap.select("INBOX", readonly=True)
        # X-GM-RAW lets the model use Gmail's own search syntax verbatim
        # (from:amy newer_than:7d is:unread …). IMAP wants the criterion as a
        # quoted string literal.
        typ, data = imap.search(None, "X-GM-RAW", f'"{query}"')
        if typ != "OK":
            return {"error": f"IMAP search failed: {typ}"}
        ids = (data[0] or b"").split()
        ids = ids[::-1][:limit]  # newest first
        out: list[dict[str, Any]] = []
        for uid in ids:
            # Headers only — X-GM-SNIPPET is NOT a real Gmail IMAP extension
            # (only X-GM-MSGID/THRID/LABELS/RAW exist); asking for it made the
            # server reject the whole FETCH with BAD "Could not parse command".
            # gmail_read fetches the body for any hit worth opening.
            typ, msg_data = imap.fetch(
                uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"
            )
            if typ != "OK" or not msg_data:
                continue
            header_bytes = b""
            for chunk in msg_data:
                if isinstance(chunk, tuple) and len(chunk) == 2:
                    header_bytes += chunk[1] or b""
            parsed = email.message_from_bytes(header_bytes)
            out.append({
                "id": uid.decode("ascii", errors="replace"),
                "from": _decode(parsed.get("From")),
                "subject": _decode(parsed.get("Subject")) or "(no subject)",
                "date": _decode(parsed.get("Date")),
            })
        return {"messages": out, "count": len(out)}
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _do_read(address: str, password: str, mid: str) -> dict[str, Any]:
    imap = _imap_login(address, password)
    try:
        imap.select("INBOX", readonly=True)
        typ, msg_data = imap.fetch(mid.encode("ascii"), "(RFC822)")
        if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            return {"error": f"message {mid} not found"}
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        body = _readable_text(msg).strip()
        truncated = len(body) > _BODY_CAP
        return {
            "id": mid,
            "from": _decode(msg.get("From")),
            "to": _decode(msg.get("To")),
            "subject": _decode(msg.get("Subject")),
            "date": _decode(msg.get("Date")),
            "message_id": msg.get("Message-ID", ""),
            "body": body[:_BODY_CAP],
            "truncated": truncated,
        }
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _lookup_reply_headers(address: str, password: str, mid: str) -> dict[str, str]:
    """For a reply, fetch the original's Message-ID + Subject so we can thread."""
    imap = _imap_login(address, password)
    try:
        imap.select("INBOX", readonly=True)
        typ, msg_data = imap.fetch(
            mid.encode("ascii"),
            "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT FROM REFERENCES)])",
        )
        if typ != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
            return {}
        parsed = email.message_from_bytes(msg_data[0][1] or b"")
        return {
            "message_id": parsed.get("Message-ID", "") or "",
            "subject": _decode(parsed.get("Subject")),
            "from": _decode(parsed.get("From")),
            "references": parsed.get("References", "") or "",
        }
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass


def _do_send(address: str, password: str, em: EmailMessage) -> dict[str, Any]:
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=_SMTP_TIMEOUT, context=context) as smtp:
        smtp.login(address, password)
        smtp.send_message(em)
    return {"ok": True, "message_id": em.get("Message-ID", "")}


# ─── tool implementations ──────────────────────────────────────────────────


async def _t_gmail_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    address, password = _creds()
    if not address or not password:
        return _NOT_CONNECTED
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "'query' is required (Gmail search syntax, e.g. 'from:amy newer_than:7d')"}
    limit = max(1, min(int(args.get("limit") or 10), 50))
    return await asyncio.to_thread(_do_search, address, password, query, limit)


async def _t_gmail_read(args: dict[str, Any], ctx: ToolContext) -> Any:
    address, password = _creds()
    if not address or not password:
        return _NOT_CONNECTED
    mid = str(args.get("id") or "").strip()
    if not mid:
        return {"error": "'id' is required (from gmail_search)"}
    return await asyncio.to_thread(_do_read, address, password, mid)


async def _t_gmail_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    address, password = _creds()
    if not address or not password:
        return _NOT_CONNECTED
    to = (args.get("to") or "").strip()
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    in_reply_to = str(args.get("in_reply_to") or "").strip()
    if not to:
        return {"error": "'to' is required"}
    if not body:
        return {"error": "'body' is required"}

    em = EmailMessage()
    from_name = parseaddr(address)[0] or ""
    em["From"] = address
    em["To"] = to
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=address.split("@")[-1] if "@" in address else None)

    if in_reply_to:
        ref = await asyncio.to_thread(_lookup_reply_headers, address, password, in_reply_to)
        orig_id = ref.get("message_id") or ""
        if orig_id:
            em["In-Reply-To"] = orig_id
            existing_refs = ref.get("references") or ""
            em["References"] = (existing_refs + " " + orig_id).strip()
        orig_subject = ref.get("subject") or subject
        if orig_subject and not orig_subject.lower().startswith("re:"):
            subject = f"Re: {orig_subject}"
        elif orig_subject:
            subject = orig_subject

    em["Subject"] = subject
    em.set_content(body)

    return await asyncio.to_thread(_do_send, address, password, em)


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="gmail_search",
        description=(
            "Search the user's Gmail directly (IMAP, app password). Uses Gmail's "
            "own search syntax via X-GM-RAW, so operators work verbatim: "
            "'from:amy newer_than:7d', 'subject:invoice is:unread', "
            "'has:attachment after:2026/01/01'. Returns id, from, subject, date, "
            "and snippet for each match (newest first). Then call gmail_read(id) "
            "for the full message."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query (Gmail operator syntax)."},
            "limit": {"type": "integer", "description": "Max results (default 10, max 50)."},
        }, "required": ["query"]},
        run=_t_gmail_search,
    ))
    registry.register(Tool(
        name="gmail_read",
        description=(
            "Read a full Gmail message by id (the id comes from gmail_search). "
            "Returns to/from/subject/date and the readable plain-text body "
            "(capped ~8k chars; 'truncated' flags when cut)."
        ),
        parameters={"type": "object", "properties": {
            "id": {"type": "string", "description": "Message id from gmail_search."},
        }, "required": ["id"]},
        run=_t_gmail_read,
    ))
    registry.register(Tool(
        name="gmail_send",
        description=(
            "Send a plain-text email from the user's Gmail (SMTP, app password). "
            "SENDING IS IRREVERSIBLE — a real email leaves the user's account "
            "immediately. Always confirm the recipient, subject, and body with "
            "the user before calling this, unless they've explicitly told you to "
            "send it. To reply in-thread, pass in_reply_to with the original "
            "message id (from gmail_search/gmail_read); it sets the threading "
            "headers and prefixes the subject with 'Re:'."
        ),
        parameters={"type": "object", "properties": {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Subject (ignored/overridden when replying)."},
            "body": {"type": "string", "description": "Plain-text body."},
            "in_reply_to": {"type": "string", "description": "Optional: message id to reply to (threads the message + 'Re:' subject)."},
        }, "required": ["to", "body"]},
        run=_t_gmail_send,
    ))
