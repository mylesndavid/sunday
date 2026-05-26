"""Gmail + Google Calendar tools, called through Nango's proxy.

Endpoint paths are relative to the base URL Nango sets for each Google
integration (gmail.googleapis.com / www.googleapis.com). If your Nango
provider template uses different bases, only the path strings here change.
"""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.integrations import nango
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.integrations.google")


# ─── Gmail ────────────────────────────────────────────────────────────────


async def _gmail_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    q = (args.get("query") or "").strip()
    limit = max(1, min(int(args.get("limit") or 10), 25))
    res = await nango.proxy("GET", "gmail/v1/users/me/messages", "gmail",
                            params={"q": q, "maxResults": limit})
    if "error" in res:
        return res
    ids = [m["id"] for m in (res.get("messages") or [])]
    out = []
    for mid in ids[:limit]:
        msg = await nango.proxy("GET", f"gmail/v1/users/me/messages/{mid}", "gmail",
                                params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
        if "error" in msg:
            continue
        headers = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
        out.append({
            "id": mid,
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
        })
    return {"messages": out, "count": len(out)}


async def _gmail_read(args: dict[str, Any], ctx: ToolContext) -> Any:
    mid = (args.get("id") or "").strip()
    if not mid:
        return {"error": "'id' is required (from gmail_search)"}
    msg = await nango.proxy("GET", f"gmail/v1/users/me/messages/{mid}", "gmail", params={"format": "full"})
    if "error" in msg:
        return msg
    headers = {h["name"]: h["value"] for h in (msg.get("payload", {}).get("headers") or [])}
    body = _extract_body(msg.get("payload", {}))
    return {
        "from": headers.get("From", ""), "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""), "date": headers.get("Date", ""),
        "body": body[:6000],
    }


def _extract_body(payload: dict) -> str:
    # prefer text/plain, fall back to first part with data
    def decode(d):
        try:
            return base64.urlsafe_b64decode(d + "===").decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return ""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return decode(payload["body"]["data"])
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return decode(part["body"]["data"])
    for part in payload.get("parts", []) or []:
        got = _extract_body(part)
        if got:
            return got
    if payload.get("body", {}).get("data"):
        return decode(payload["body"]["data"])
    return ""


async def _gmail_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = (args.get("to") or "").strip()
    subject = args.get("subject") or ""
    body = args.get("body") or ""
    if not to or not body:
        return {"error": "'to' and 'body' are required"}
    em = EmailMessage()
    em["To"] = to
    em["Subject"] = subject
    em.set_content(body)
    raw = base64.urlsafe_b64encode(em.as_bytes()).decode("ascii")
    res = await nango.proxy("POST", "gmail/v1/users/me/messages/send", "gmail", json={"raw": raw})
    if "error" in res:
        return res
    return {"ok": True, "id": res.get("id")}


# ─── Calendar ─────────────────────────────────────────────────────────────


async def _cal_list(args: dict[str, Any], ctx: ToolContext) -> Any:
    params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": max(1, min(int(args.get("limit") or 10), 25))}
    if args.get("time_min"):
        params["timeMin"] = args["time_min"]
    if args.get("time_max"):
        params["timeMax"] = args["time_max"]
    res = await nango.proxy("GET", "calendar/v3/calendars/primary/events", "calendar", params=params)
    if "error" in res:
        return res
    events = [{
        "id": e.get("id"),
        "summary": e.get("summary", "(no title)"),
        "start": (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date"),
        "end": (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date"),
        "location": e.get("location", ""),
    } for e in (res.get("items") or [])]
    return {"events": events, "count": len(events)}


async def _cal_create(args: dict[str, Any], ctx: ToolContext) -> Any:
    summary = (args.get("summary") or "").strip()
    start = (args.get("start") or "").strip()
    end = (args.get("end") or "").strip()
    if not summary or not start or not end:
        return {"error": "'summary', 'start', and 'end' (ISO 8601) are required"}
    event = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if args.get("description"):
        event["description"] = args["description"]
    if args.get("location"):
        event["location"] = args["location"]
    res = await nango.proxy("POST", "calendar/v3/calendars/primary/events", "calendar", json=event)
    if "error" in res:
        return res
    return {"ok": True, "id": res.get("id"), "link": res.get("htmlLink")}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="gmail_search",
        description="Search the user's Gmail. Returns sender, subject, date, snippet, and id for each match. Use Gmail search operators (from:, subject:, after:, is:unread). Then gmail_read(id) for full content.",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail search query."},
            "limit": {"type": "integer", "description": "Max results (default 10)."},
        }, "required": ["query"]},
        run=_gmail_search,
    ))
    registry.register(Tool(
        name="gmail_read",
        description="Read a full Gmail message by id (from gmail_search).",
        parameters={"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        run=_gmail_read,
    ))
    registry.register(Tool(
        name="gmail_send",
        description="Send an email from the user's Gmail. Confirm the recipient + content with the user before sending anything non-trivial.",
        parameters={"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"},
        }, "required": ["to", "body"]},
        run=_gmail_send,
    ))
    registry.register(Tool(
        name="calendar_list",
        description="List upcoming Google Calendar events on the user's primary calendar. Pass ISO-8601 time_min/time_max to bound the window.",
        parameters={"type": "object", "properties": {
            "time_min": {"type": "string", "description": "ISO 8601 lower bound."},
            "time_max": {"type": "string", "description": "ISO 8601 upper bound."},
            "limit": {"type": "integer"},
        }},
        run=_cal_list,
    ))
    registry.register(Tool(
        name="calendar_create",
        description="Create a Google Calendar event on the primary calendar. start/end are ISO 8601 datetimes. Confirm details with the user first.",
        parameters={"type": "object", "properties": {
            "summary": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"},
            "description": {"type": "string"}, "location": {"type": "string"},
        }, "required": ["summary", "start", "end"]},
        run=_cal_create,
    ))
