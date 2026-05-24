"""Sendblue iMessage channel — Sunday's own phone number.

This is the "text Sunday from anywhere" path. Sendblue gives Sunday a
dedicated iMessage-capable phone number. Inbound messages hit our webhook;
Sunday writes them to the one chat with modality='imessage_sendblue',
runs the brain, and replies through Sendblue's API.

Distinct from channels/messages_local, which is "Sunday can read and
respond as you in your own iMessages."

Config (via credentials):
  SENDBLUE_API_KEY_ID
  SENDBLUE_API_SECRET_KEY
  SENDBLUE_NUMBER          your Sendblue-provisioned number for outbound
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from aiohttp import web

from sunday.brain import respond
from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.sendblue")

SENDBLUE_API_BASE = "https://api.sendblue.co/api"
SENDBLUE_SEND     = f"{SENDBLUE_API_BASE}/send-message"
SENDBLUE_TYPING   = f"{SENDBLUE_API_BASE}/send-typing-indicator"


def _sendblue_headers() -> dict[str, str] | None:
    api_key = get_credential("SENDBLUE_API_KEY_ID")
    api_secret = get_credential("SENDBLUE_API_SECRET_KEY")
    if not api_key or not api_secret:
        return None
    return {
        "sb-api-key-id": api_key,
        "sb-api-secret-key": api_secret,
        "Content-Type": "application/json",
    }


async def _send_typing(to: str) -> dict[str, Any]:
    """Make Sunday show up as 'typing…' in the user's Messages thread.

    Best-effort — if Sendblue returns a non-2xx (endpoint shape varies by
    plan), we log and continue rather than failing the inbound handler.
    """
    headers = _sendblue_headers()
    if headers is None:
        return {"error": "credentials missing"}
    async with httpx.AsyncClient(timeout=10) as client:
        res = await client.post(SENDBLUE_TYPING, headers=headers, json={"number": to})
    if res.status_code >= 400:
        log.warning("sendblue typing indicator failed", status=res.status_code, body=res.text[:200])
        return {"error": f"sendblue typing {res.status_code}"}
    return {"ok": True}


async def _send_sendblue(to: str, body: str, media_url: str | None = None) -> dict[str, Any]:
    headers = _sendblue_headers()
    if headers is None:
        return {
            "error": "SENDBLUE_API_KEY_ID / SENDBLUE_API_SECRET_KEY missing. "
                     "Run: sunday credential set SENDBLUE_API_KEY_ID <key>"
        }

    payload: dict[str, Any] = {"number": to, "content": body}
    if media_url:
        payload["media_url"] = media_url

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(SENDBLUE_SEND, headers=headers, json=payload)
    try:
        data = res.json()
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        return {"error": f"sendblue {res.status_code}: {data}"}
    return {"ok": True, "to": to, "data": data}


# ─── webhook ─────────────────────────────────────────────────────────────


async def _webhook_handler(request: web.Request, daemon: Any) -> web.Response:
    """Sendblue inbound webhook. Drops the message into Sunday's chat and runs the brain."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)

    # Skip outbound delivery receipts and status callbacks
    if body.get("is_outbound"):
        return web.json_response({"ok": True, "skipped": "outbound receipt"})
    status = (body.get("status") or "").upper()
    if status and status not in ("RECEIVED", "DELIVERED"):
        # echo, sent-callback, etc.
        return web.json_response({"ok": True, "skipped": f"status={status}"})

    sender = body.get("from_number") or body.get("number") or ""
    text = body.get("content") or ""
    media_url = body.get("media_url")

    if not text and media_url:
        text = f"(media: {media_url})"
    if not sender or not text:
        return web.json_response({"ok": True, "skipped": "missing sender/content"})

    # Fire-and-forget: show "Sunday is typing…" in the user's iMessage
    # thread the moment the inbound message lands, well before the brain
    # has finished generating a reply.
    import asyncio as _asyncio
    _asyncio.create_task(_send_typing(sender))

    try:
        reply = await respond(
            daemon.chat,
            text,
            "imessage_sendblue",
            daemon.config,
            daemon.registry,
            extras={"broadcast": daemon._broadcast, "devices": daemon.devices},
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("sendblue brain failed")
        return web.json_response({"ok": False, "error": str(exc)}, status=500)

    send_result = await _send_sendblue(sender, reply)
    return web.json_response({"ok": True, "reply": reply, "send": send_result})


# ─── tool ────────────────────────────────────────────────────────────────


_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient phone number in E.164 ('+15551234567')."},
        "body": {"type": "string", "description": "Message body."},
        "media_url": {"type": "string", "description": "Optional public URL of media to attach."},
    },
    "required": ["to", "body"],
}


async def _t_sendblue_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    body = args.get("body")
    if not to or not body:
        return {"error": "'to' and 'body' are required"}
    return await _send_sendblue(str(to), str(body), args.get("media_url"))


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    # Register the inbound webhook lazily — daemon.py owns the registry.
    from sunday.daemon import register_webhook
    register_webhook("/webhooks/sendblue", _webhook_handler)

    registry.register(Tool(
        name="sendblue_send",
        description=(
            "Send an iMessage from Sunday's own Sendblue number to any phone. "
            "Use for sending from the bot's number, not the user's personal iMessages "
            "(for that, use imessage_send)."
        ),
        parameters=_SEND_PARAMS,
        run=_t_sendblue_send,
    ))
