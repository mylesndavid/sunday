"""iMessage tools — central registration, satellite execution.

These tools live in the brain's tool catalog but the actual chat.db read
and AppleScript send happen on a Sunday satellite running on the user's
Mac. The central daemon (which usually lives in a datacenter) holds no
local Messages state — that data correctly stays on the user's machine.

A satellite advertises the 'imessage' capability when it can read
~/Library/Messages/chat.db. The first such satellite is auto-picked; the
brain can override by passing a `device_id`.
"""

from __future__ import annotations

from typing import Any

from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry


def _pick_imessage_device(ctx: ToolContext, explicit: str | None = None) -> tuple[Any, str | None]:
    """Return (manager, device_id) or (manager, None) when no imessage device is connected."""
    mgr = ctx.extras.get("devices")
    if mgr is None:
        return None, None
    devices = mgr.list_devices()
    if explicit:
        for d in devices:
            if d["device_id"] == explicit and "imessage" in (d.get("capabilities") or []):
                return mgr, explicit
        return mgr, None
    for d in devices:
        if "imessage" in (d.get("capabilities") or []):
            return mgr, d["device_id"]
    return mgr, None


_NO_DEVICE_ERROR = {
    "error": (
        "No connected satellite advertises the 'imessage' capability. "
        "Run `sunday-satellite` on a Mac with Messages.app signed in (and Full "
        "Disk Access granted) to enable reading + replying to your real iMessages."
    )
}


async def _proxy(ctx: ToolContext, method: str, params: dict[str, Any], explicit: str | None = None) -> Any:
    mgr, device_id = _pick_imessage_device(ctx, explicit)
    if mgr is None:
        return {"error": "DeviceManager not attached (running outside daemon?)"}
    if device_id is None:
        return _NO_DEVICE_ERROR
    try:
        return await mgr.command(device_id, method, params, timeout=30)
    except RuntimeError as exc:
        return {"error": str(exc)}


_DEVICE_PARAM = {
    "device_id": {
        "type": "string",
        "description": "Optional: pick a specific satellite by id. Defaults to the first connected satellite advertising 'imessage'.",
    },
}

_LIST_THREADS_PARAMS = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "default": 20, "description": "Max threads to return."},
        **_DEVICE_PARAM,
    },
}

_READ_THREAD_PARAMS = {
    "type": "object",
    "properties": {
        "chat_identifier": {
            "type": "string",
            "description": "From imessage_list_threads — phone number, email, or chat GUID.",
        },
        "limit": {"type": "integer", "default": 30, "description": "Max messages to return."},
        **_DEVICE_PARAM,
    },
    "required": ["chat_identifier"],
}

_READ_RECENT_PARAMS = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "default": 20, "description": "Max messages across all threads."},
        **_DEVICE_PARAM,
    },
}

_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Recipient handle (E.164 phone or email)."},
        "body": {"type": "string", "description": "Message body. Optional if attachments are provided."},
        "attachments": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Absolute paths (on the satellite's filesystem) to files/images to send.",
        },
        **_DEVICE_PARAM,
    },
    "required": ["to"],
}


async def _t_list_threads(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _proxy(ctx, "imessage_list_threads",
                        {"limit": int(args.get("limit") or 20)},
                        args.get("device_id"))


async def _t_read_thread(args: dict[str, Any], ctx: ToolContext) -> Any:
    chat_id = args.get("chat_identifier")
    if not chat_id:
        return {"error": "'chat_identifier' is required"}
    return await _proxy(ctx, "imessage_read_thread",
                        {"chat_identifier": str(chat_id), "limit": int(args.get("limit") or 30)},
                        args.get("device_id"))


async def _t_read_recent(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _proxy(ctx, "imessage_read_recent",
                        {"limit": int(args.get("limit") or 20)},
                        args.get("device_id"))


async def _t_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    body = args.get("body") or ""
    atts = args.get("attachments") or []
    if not to:
        return {"error": "'to' is required"}
    if not body and not atts:
        return {"error": "'body' or 'attachments' is required"}
    if not isinstance(atts, list):
        return {"error": "'attachments' must be a list of paths"}
    return await _proxy(ctx, "imessage_send",
                        {"to": str(to), "body": str(body), "attachments": [str(p) for p in atts]},
                        args.get("device_id"))


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="imessage_list_threads",
        description="List recent iMessage conversations from your Mac's Messages.app (via the connected satellite). Newest first.",
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
        description="Read the most recent iMessages across all threads on your Mac.",
        parameters=_READ_RECENT_PARAMS,
        run=_t_read_recent,
    ))
    registry.register(Tool(
        name="imessage_send",
        description=(
            "Send an iMessage from your Mac's Messages.app to a specific handle. "
            "The reply lands in your own Messages history exactly as if you typed it."
        ),
        parameters=_SEND_PARAMS,
        run=_t_send,
    ))
