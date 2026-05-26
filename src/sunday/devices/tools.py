"""Tools the brain calls to drive remote devices.

Every tool routes through the main daemon's DeviceManager (available via
ToolContext.extras['devices']) and dispatches the right method to the
named satellite.

Screen / CDP screenshots return base64 PNGs; we stash them into
~/.sunday/attachments/ and return Attachment dicts so the next turn can
treat them as image content for vision-capable models.
"""

from __future__ import annotations

import base64
import time
import uuid
from typing import Any

import structlog

from sunday.attachments import Attachment, attachments_dir
from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.devices.tools")


def _devices_manager(ctx: ToolContext):
    mgr = ctx.extras.get("devices")
    if mgr is None:
        raise RuntimeError("DeviceManager not attached to this context (running outside daemon?)")
    return mgr


def _resolve_device(ctx: ToolContext, explicit: str | None = None, capability: str | None = None) -> tuple[str | None, str | None]:
    """Pick the device a tool should target. With an explicit id, validate
    it's connected. Otherwise auto-select the connected device (optionally
    one advertising `capability`). Returns (device_id, error). This is why
    "show me my screen" works without the model first calling device_list
    and threading an id through — there's almost always exactly one Mac."""
    mgr = _devices_manager(ctx)
    devices = mgr.list_devices()
    if explicit:
        if any(d["device_id"] == explicit for d in devices):
            return explicit, None
        ids = [d["device_id"] for d in devices]
        return None, f"no connected device '{explicit}'. Connected: {ids or 'none'}"
    if capability:
        devices = [d for d in devices if capability in (d.get("capabilities") or [])]
    if not devices:
        what = f" advertising '{capability}'" if capability else ""
        return None, (
            f"no connected device{what}. Open the Sunday app on your Mac "
            "(or start the satellite) so it shows up in device_list."
        )
    return devices[0]["device_id"], None


def _save_image_b64(image_b64: str, label: str) -> Attachment:
    data = base64.b64decode(image_b64)
    path = attachments_dir() / f"{label}-{uuid.uuid4().hex[:8]}.png"
    path.write_bytes(data)
    return Attachment.from_local_path(path)


# ─── tools ───────────────────────────────────────────────────────────────


async def _t_device_list(args: dict[str, Any], ctx: ToolContext) -> Any:
    return {"devices": _devices_manager(ctx).list_devices()}


_DEVICE_ID_PARAM = {
    "device_id": {
        "type": "string",
        "description": "Optional. Identifier from device_list. Omit to use the connected device automatically.",
    },
}


_RUN_COMMAND_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "command": {"type": "string", "description": "Shell command."},
        "cwd": {"type": "string", "description": "Working directory (optional)."},
        "timeout": {"type": "number", "description": "Per-command timeout in seconds (default 60)."},
    },
    "required": ["command"],
}


async def _t_device_run_command(args: dict[str, Any], ctx: ToolContext) -> Any:
    command = args.get("command")
    if not command:
        return {"error": "'command' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="shell")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id),
            "run_command",
            {
                "command": command,
                "cwd": args.get("cwd"),
                "timeout": args.get("timeout") or 60,
            },
            timeout=float(args.get("timeout") or 60) + 10,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


_SCREENSHOT_PARAMS = {
    "type": "object",
    "properties": {**_DEVICE_ID_PARAM},
}


async def _t_device_screenshot(args: dict[str, Any], ctx: ToolContext) -> Any:
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="screen")
    if err:
        return {"error": err}
    try:
        result = await _devices_manager(ctx).command(str(device_id), "screenshot", {})
    except RuntimeError as exc:
        return {"error": str(exc)}
    if isinstance(result, dict) and "error" in result:
        return result
    image_b64 = (result or {}).get("image_b64")
    if not image_b64:
        return {"error": "no image returned"}
    att = _save_image_b64(image_b64, f"screen-{device_id}")
    await ctx.broadcast({
        "type": "device_screen",
        "device_id": device_id,
        "screenshot_path": att.path,
        "ts": time.time(),
    })
    return {"attachment": att.to_dict()}


async def _t_device_screen_text(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Read what's on the screen as text (local Vision OCR on the device).
    Use this — not device_screenshot — to actually understand the screen
    with a text-only model; it returns readable text, not an image."""
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="screen")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(str(device_id), "screen_text", {}, timeout=30)
    except RuntimeError as exc:
        return {"error": str(exc)}


# ─── CDP tools ───────────────────────────────────────────────────────────


_CDP_LAUNCH_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "url": {"type": "string", "description": "Start URL (defaults to about:blank)."},
        "profile_id": {"type": "string", "description": "Isolated shadow-profile name (defaults to 'default')."},
        "browser_path": {"type": "string", "description": "Override path to a Chromium/Electron binary."},
    },
}

_CDP_NAVIGATE_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "url": {"type": "string"},
        "profile_id": {"type": "string"},
    },
    "required": ["url"],
}

_CDP_SCREENSHOT_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "profile_id": {"type": "string"},
    },
}

_CDP_EVAL_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "expression": {"type": "string", "description": "JavaScript expression to evaluate in the page."},
        "profile_id": {"type": "string"},
    },
    "required": ["expression"],
}


async def _t_cdp_launch(args: dict[str, Any], ctx: ToolContext) -> Any:
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id),
            "cdp_launch",
            {
                "url": args.get("url"),
                "profile_id": args.get("profile_id", "default"),
                "browser_path": args.get("browser_path"),
            },
            timeout=30,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_cdp_navigate(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = args.get("url")
    if not url:
        return {"error": "'url' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id),
            "cdp_navigate",
            {"url": url, "profile_id": args.get("profile_id", "default")},
            timeout=30,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_cdp_screenshot(args: dict[str, Any], ctx: ToolContext) -> Any:
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        result = await _devices_manager(ctx).command(
            str(device_id),
            "cdp_screenshot",
            {"profile_id": args.get("profile_id", "default")},
            timeout=30,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}
    image_b64 = (result or {}).get("image_b64")
    if not image_b64:
        return {"error": "no image returned"}
    att = _save_image_b64(image_b64, f"cdp-{device_id}")
    await ctx.broadcast({
        "type": "device_browser_frame",
        "device_id": device_id,
        "screenshot_path": att.path,
        "ts": time.time(),
    })
    return {"attachment": att.to_dict()}


async def _t_cdp_evaluate(args: dict[str, Any], ctx: ToolContext) -> Any:
    expression = args.get("expression")
    if not expression:
        return {"error": "'expression' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id),
            "cdp_evaluate",
            {"expression": expression, "profile_id": args.get("profile_id", "default")},
            timeout=30,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


_OPEN_URL_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "url": {"type": "string", "description": "URL to open (https://…, mailto:, …)."},
    },
    "required": ["url"],
}

_OPEN_APP_PARAMS = {
    "type": "object",
    "properties": {
        **_DEVICE_ID_PARAM,
        "app": {"type": "string", "description": "App name ('Safari', 'Messages') or bundle id ('com.apple.Safari')."},
        "args": {"type": "array", "items": {"type": "string"}, "description": "Optional CLI args passed to the app."},
    },
    "required": ["app"],
}


async def _t_device_open_url(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = args.get("url")
    if not url:
        return {"error": "'url' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="shell")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id), "open_url", {"url": str(url)}, timeout=15,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_device_open_app(args: dict[str, Any], ctx: ToolContext) -> Any:
    app = args.get("app")
    if not app:
        return {"error": "'app' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="shell")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id), "open_app",
            {"app": str(app), "args": args.get("args") or []},
            timeout=15,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="device_list",
        description="List satellite devices currently connected to Sunday.",
        parameters={"type": "object", "properties": {}},
        run=_t_device_list,
    ))
    registry.register(Tool(
        name="device_open_url",
        description="Open a URL on a connected device (launches the default browser or registered handler).",
        parameters=_OPEN_URL_PARAMS,
        run=_t_device_open_url,
    ))
    registry.register(Tool(
        name="device_open_app",
        description="Launch a macOS app by name or bundle id on a connected device.",
        parameters=_OPEN_APP_PARAMS,
        run=_t_device_open_app,
    ))

    # ─── Rewind tools — route through satellites that advertise 'rewind' ──

    def _pick_rewind_device(ctx, explicit=None):
        mgr = ctx.extras.get("devices")
        if mgr is None:
            return None, None
        for d in mgr.list_devices():
            if explicit and d["device_id"] != explicit:
                continue
            if "rewind" in (d.get("capabilities") or []):
                return mgr, d["device_id"]
        return mgr, None

    async def _rewind_proxy(method, params, ctx, explicit=None):
        mgr, did = _pick_rewind_device(ctx, explicit)
        if mgr is None:
            return {"error": "DeviceManager unavailable"}
        if did is None:
            return {"error": "no connected satellite advertises 'rewind' (needs macOS + Screen Recording permission)"}
        try:
            return await mgr.command(did, method, params, timeout=15)
        except RuntimeError as exc:
            return {"error": str(exc)}

    async def _t_rewind_search(args, ctx):
        q = (args.get("query") or "").strip()
        if not q: return {"error": "'query' is required"}
        return await _rewind_proxy("rewind_search", {"query": q, "limit": int(args.get("limit") or 10)}, ctx, args.get("device_id"))

    async def _t_rewind_recent(args, ctx):
        return await _rewind_proxy("rewind_recent", {"limit": int(args.get("limit") or 10)}, ctx, args.get("device_id"))

    async def _t_rewind_stats(args, ctx):
        return await _rewind_proxy("rewind_stats", {}, ctx, args.get("device_id"))

    async def _t_rewind_start(args, ctx):
        params = {"interval_seconds": int(args.get("interval_seconds") or 300)}
        return await _rewind_proxy("rewind_start", params, ctx, args.get("device_id"))

    async def _t_rewind_stop(args, ctx):
        return await _rewind_proxy("rewind_stop", {}, ctx, args.get("device_id"))

    registry.register(Tool(
        name="rewind_search",
        description=(
            "Search the user's screen history for frames whose OCR'd text matches the query. "
            "Use for 'what was on my screen earlier showing X' / 'find the slide that said Y'. "
            "Returns matching frames with timestamp + OCR text snippet."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to search for (FTS5 syntax supported)."},
                "limit": {"type": "integer", "description": "Max frames to return (default 10)."},
                "device_id": {"type": "string"},
            },
            "required": ["query"],
        },
        run=_t_rewind_search,
    ))
    registry.register(Tool(
        name="rewind_recent",
        description="Return the most recent N captured + OCR'd screen frames in reverse-chronological order.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max frames (default 10)."},
                "device_id": {"type": "string"},
            },
        },
        run=_t_rewind_recent,
    ))
    registry.register(Tool(
        name="rewind_stats",
        description="Stats on the user's Rewind index — total frames, oldest/newest timestamp, watcher state.",
        parameters={"type": "object", "properties": {"device_id": {"type": "string"}}},
        run=_t_rewind_stats,
    ))
    registry.register(Tool(
        name="rewind_start",
        description=(
            "Turn on continuous screen capture + OCR indexing on the connected Mac satellite. "
            "Frames captured every interval_seconds (default 300). Idempotent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "interval_seconds": {"type": "integer", "description": "Seconds between captures (default 300)."},
                "device_id": {"type": "string"},
            },
        },
        run=_t_rewind_start,
    ))
    registry.register(Tool(
        name="rewind_stop",
        description="Turn off the Rewind watcher. Stored history stays searchable.",
        parameters={"type": "object", "properties": {"device_id": {"type": "string"}}},
        run=_t_rewind_stop,
    ))
    registry.register(Tool(
        name="device_run_command",
        description="Run a shell command on a specific connected device. Returns stdout/stderr/exit_code.",
        parameters=_RUN_COMMAND_PARAMS,
        run=_t_device_run_command,
    ))
    registry.register(Tool(
        name="device_screenshot",
        description="Capture the full screen of a connected device as an image. For understanding what's on screen with a text model, prefer device_screen_text. Omit device_id to use the connected device.",
        parameters=_SCREENSHOT_PARAMS,
        run=_t_device_screenshot,
    ))
    registry.register(Tool(
        name="device_screen_text",
        description=(
            "Read what's currently on the user's screen as text. Captures the "
            "screen on the connected Mac and OCRs it locally (free, on-device "
            "Apple Vision) — returns readable text, not an image, so it works "
            "with a text-only model. Use this whenever the user asks what's on "
            "their screen / to read something they're looking at. Omit device_id "
            "to use the connected device."
        ),
        parameters=_SCREENSHOT_PARAMS,
        run=_t_device_screen_text,
    ))
    registry.register(Tool(
        name="device_cdp_launch",
        description=(
            "Launch a Chromium-based browser (Chrome / Edge / Arc / an Electron app) in an "
            "isolated shadow profile on a connected device, optionally navigating to a URL."
        ),
        parameters=_CDP_LAUNCH_PARAMS,
        run=_t_cdp_launch,
    ))
    registry.register(Tool(
        name="device_cdp_navigate",
        description="Navigate the shadow-profile browser on a device to a URL.",
        parameters=_CDP_NAVIGATE_PARAMS,
        run=_t_cdp_navigate,
    ))
    registry.register(Tool(
        name="device_cdp_screenshot",
        description="Screenshot the shadow-profile browser's current page on a device. Saves to attachments and broadcasts a live frame.",
        parameters=_CDP_SCREENSHOT_PARAMS,
        run=_t_cdp_screenshot,
    ))
    registry.register(Tool(
        name="device_cdp_evaluate",
        description="Evaluate a JavaScript expression in the shadow-profile browser on a device. Use for reading page state or driving UI.",
        parameters=_CDP_EVAL_PARAMS,
        run=_t_cdp_evaluate,
    ))
