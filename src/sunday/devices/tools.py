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
import re
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


def _norm_device_id(s: str) -> str:
    """Fold a device id to its comparable core: lowercase, letters+digits only.
    'Myless-Mac mini-2', 'myless_mac_mini_2' and 'MylessMacMini2' all become
    'mylessmacmini2'."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _resolve_device(ctx: ToolContext, explicit: str | None = None, capability: str | None = None) -> tuple[str | None, str | None]:
    """Pick the device a tool should target. With an explicit id, validate
    it's connected. Otherwise auto-select the connected device (optionally
    one advertising `capability`). Returns (device_id, error). This is why
    "show me my screen" works without the model first calling device_list
    and threading an id through — there's almost always exactly one Mac.

    Explicit ids match loosely on purpose. Device ids come from hostnames, so
    they're full of hyphens and case the model reliably mis-remembers — asking
    for 'Myless-Mac mini-2' when the device registered as 'Myless-Mac-mini-2'
    used to hard-fail even though exactly one device obviously matched. We fold
    both sides to letters+digits and accept an unambiguous hit; a fold that
    matches several devices still errors rather than guessing between them."""
    mgr = _devices_manager(ctx)
    devices = mgr.list_devices()
    if explicit:
        if any(d["device_id"] == explicit for d in devices):
            return explicit, None
        ids = [d["device_id"] for d in devices]
        want = _norm_device_id(explicit)
        near = [d["device_id"] for d in devices if _norm_device_id(d["device_id"]) == want]
        if len(near) == 1:
            log.info("device id matched loosely", asked=explicit, matched=near[0])
            return near[0], None
        if len(near) > 1:
            return None, f"'{explicit}' matches more than one connected device: {near}. Use the exact id."
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


# Process-control verbs that, aimed at Sunday's own launchd label, would SIGTERM
# the daemon running THIS turn — so the reply never sends.
_TEARDOWN_VERBS = ("bootout", "unload", "remove", "kickstart", "stop", "disable")
# Known setup scripts whose whole job is to bootout + re-bootstrap the daemon.
_SELF_KILL_SCRIPTS = ("imessage-doctor.sh", "sunday-imessage-setup.sh")


def _would_self_destruct(command: str) -> bool:
    """True if `command` would tear down Sunday's own daemon — the process
    handling this very turn.

    The brain must never be able to kill itself mid-reply. A texted "restart
    yourself" or a pasted setup script once SIGTERM'd the daemon right after the
    `device_run_command` call, so "sunday down" landed before the answer could
    send and the text silently never arrived. Read-only inspection
    (`launchctl list | grep sunday`) stays allowed; only teardown verbs aimed at
    our own label/binary are refused."""
    c = " ".join(command.lower().split())  # collapse whitespace/newlines
    if any(s in c for s in _SELF_KILL_SCRIPTS):
        return True
    if ("pkill" in c or "killall" in c) and "sunday-daemon" in c:
        return True
    if "pkill" in c and "-f" in c and "sunday" in c:
        return True
    # launchctl teardown/restart of our own label (com.sunday.*)
    if re.search(r"com\.sunday|sunday\.daemon|sunday\.imessage", c):
        if any(v in c for v in _TEARDOWN_VERBS):
            return True
    return False


async def _t_device_run_command(args: dict[str, Any], ctx: ToolContext) -> Any:
    command = args.get("command")
    if not command:
        return {"error": "'command' is required"}
    if _would_self_destruct(str(command)):
        log.warning("refused self-destruct command", command=str(command)[:200])
        return {"error": (
            "Refused — that command would terminate Sunday's own daemon (the "
            "process handling this conversation), so your reply would never "
            "send. Do not try to restart or bootout Sunday from inside a turn. "
            "If a restart is genuinely needed, ask the user to do it. Answer in "
            "words instead."
        )}
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


def _parse_btm(dump: str) -> list[dict[str, str]]:
    """Parse `sfltool dumpbtm` into [{name, type, enabled}]. Each record is a
    Name/Type/Disposition triple; an item auto-starts when its Disposition is
    'enabled' and not 'disabled'."""
    items: list[dict[str, str]] = []
    name = typ = None
    for line in dump.splitlines():
        s = line.strip()
        if s.startswith("Name:"):
            name = s[len("Name:"):].strip()
        elif s.startswith("Type:"):
            typ = s[len("Type:"):].strip()
        elif s.startswith("Disposition:"):
            disp = s[len("Disposition:"):].strip()
            enabled = "enabled" in disp and "disabled" not in disp
            if name and name != "(null)":
                items.append({"name": name, "type": (typ or "").split(" (")[0], "enabled": enabled})
            name = typ = None
    return items


# Plumbing types that show in BTM but aren't "apps that launch at login" — the
# user almost never means these when asking what auto-starts.
_BTM_PLUMBING = {"quicklook", "spotlight", "dock tile"}


def _agent_names(listing: str) -> list[str]:
    """Active plist filenames → readable labels. Only real `.plist` files are
    loaded agents — skip Apple's own, plus `.disabled` files and any non-plist
    entries (e.g. backup folders) that aren't active."""
    out = []
    for line in listing.splitlines():
        f = line.strip()
        if not f or f.startswith("com.apple.") or not f.endswith(".plist"):
            continue
        out.append(f[:-6])
    return out


async def _t_mac_startup_items(args: dict[str, Any], ctx: ToolContext) -> Any:
    """Report what actually auto-starts on the Mac, from the authoritative
    sources — NOT a guess from /Applications, and NOT hand-written AppleScript."""
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="shell")
    if err:
        return {"error": err}
    # One shell round-trip, delimited sections. Login items + LaunchAgents/
    # Daemons need no admin and always work; sfltool dumpbtm (the full
    # Background Task Management registry) is richer but needs admin auth, so
    # it's a bonus when available, never relied on.
    script = (
        'echo "===LOGIN==="; osascript -e '
        "'tell application \"System Events\" to get the name of every login item' 2>/dev/null; "
        'echo "===UAGENTS==="; ls -1 "$HOME/Library/LaunchAgents" 2>/dev/null; '
        'echo "===SAGENTS==="; ls -1 /Library/LaunchAgents 2>/dev/null; '
        'echo "===SDAEMONS==="; ls -1 /Library/LaunchDaemons 2>/dev/null; '
        'echo "===BTM==="; sfltool dumpbtm 2>/dev/null'
    )
    try:
        res = await _devices_manager(ctx).command(
            str(device_id), "run_command", {"command": script, "timeout": 30}, timeout=40,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}
    out = (res or {}).get("stdout") or ""
    if not out and (res or {}).get("error"):
        return {"error": res["error"]}

    sections: dict[str, str] = {}
    cur = None
    for line in out.splitlines():
        if line.startswith("===") and line.endswith("==="):
            cur = line.strip("= ")
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"

    login_items = [n.strip() for n in (sections.get("LOGIN", "").replace("\n", ",")).split(",") if n.strip()]
    user_agents = _agent_names(sections.get("UAGENTS", ""))
    sys_agents = _agent_names(sections.get("SAGENTS", ""))
    sys_daemons = _agent_names(sections.get("SDAEMONS", ""))

    btm = _parse_btm(sections.get("BTM", ""))
    btm_available = bool(btm)
    btm_launches = sorted({b["name"] for b in btm if b["enabled"] and b["type"] not in _BTM_PLUMBING})

    result = {
        "login_items": login_items,
        "user_launch_agents": user_agents,
        "system_launch_agents": sys_agents,
        "system_launch_daemons": sys_daemons,
    }
    if btm_available:
        result["background_task_manager"] = btm_launches
        result["note"] = (
            "login_items = apps set to open at login. The launch_agents/daemons "
            "are background services that start automatically (user agents need "
            "no sudo to disable — move the plist out of ~/Library/LaunchAgents; "
            "system ones need sudo). background_task_manager is the full macOS "
            "BTM registry (admin was available this time). To disable login "
            "items: System Settings → General → Login Items."
        )
    else:
        result["note"] = (
            "This is the no-admin view: login_items (apps set to open at login) "
            "plus the launch agents/daemons that start background services. The "
            "full Background Task Management registry (every app-registered "
            "background item) needs admin via `sudo sfltool dumpbtm` — offer "
            "that to the user if they want the exhaustive list. Disable a user "
            "agent by moving its plist out of ~/Library/LaunchAgents (no sudo); "
            "system agents/daemons need sudo; login items toggle in System "
            "Settings → General → Login Items."
        )
    return result


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


async def _cdp_cmd(ctx: ToolContext, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
    device_id, err = _resolve_device(ctx, params.pop("device_id", None), capability="cdp")
    if err:
        return {"error": err}
    params.setdefault("profile_id", "default")
    try:
        return await _devices_manager(ctx).command(str(device_id), method, params, timeout=timeout)
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_browser_read(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _cdp_cmd(ctx, "cdp_read", {"device_id": args.get("device_id"), "profile_id": args.get("profile_id", "default")})


async def _t_browser_click(args: dict[str, Any], ctx: ToolContext) -> Any:
    if args.get("ref") is None and not args.get("selector"):
        return {"error": "pass 'ref' (from browser_read) or 'selector'"}
    return await _cdp_cmd(ctx, "cdp_click", {
        "device_id": args.get("device_id"), "profile_id": args.get("profile_id", "default"),
        "ref": args.get("ref"), "selector": args.get("selector"),
    })


async def _t_browser_type(args: dict[str, Any], ctx: ToolContext) -> Any:
    if args.get("ref") is None or not args.get("text"):
        return {"error": "'ref' and 'text' are required"}
    return await _cdp_cmd(ctx, "cdp_type", {
        "device_id": args.get("device_id"), "profile_id": args.get("profile_id", "default"),
        "ref": args.get("ref"), "text": args.get("text"), "submit": bool(args.get("submit")),
    })


async def _t_browser_key(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _cdp_cmd(ctx, "cdp_key", {
        "device_id": args.get("device_id"), "profile_id": args.get("profile_id", "default"),
        "key": args.get("key", "Enter"),
    })


# ─── native UI control (any app, via macOS Accessibility) ────────────────


async def _ctl_cmd(ctx: ToolContext, method: str, params: dict[str, Any], timeout: float = 25) -> Any:
    device_id, err = _resolve_device(ctx, params.pop("device_id", None), capability="control")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(str(device_id), method, params, timeout=timeout)
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_app_snapshot(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _ctl_cmd(ctx, "ax_snapshot", {"device_id": args.get("device_id")})


async def _t_app_click(args: dict[str, Any], ctx: ToolContext) -> Any:
    if args.get("x") is None or args.get("y") is None:
        return {"error": "'x' and 'y' are required (from app_snapshot)"}
    return await _ctl_cmd(ctx, "ax_click", {"device_id": args.get("device_id"), "x": args["x"], "y": args["y"]})


async def _t_app_type(args: dict[str, Any], ctx: ToolContext) -> Any:
    if not args.get("text"):
        return {"error": "'text' is required"}
    return await _ctl_cmd(ctx, "ax_type", {"device_id": args.get("device_id"), "text": args["text"]})


async def _t_app_key(args: dict[str, Any], ctx: ToolContext) -> Any:
    if not args.get("combo"):
        return {"error": "'combo' is required (e.g. cmd+t, enter, cmd+shift+4)"}
    return await _ctl_cmd(ctx, "ax_key", {"device_id": args.get("device_id"), "combo": args["combo"]})


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


# ─── Electron app CDP dispatchers (daemon-side, routes to the satellite) ──

async def _t_electron_launch(args: dict[str, Any], ctx: ToolContext) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "'name' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        # 30s timeout because launching Slack-class apps + CDP handshake can
        # take 10-20s on first launch.
        return await _devices_manager(ctx).command(
            str(device_id), "app_launch", {"name": name}, timeout=45,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_electron_close(args: dict[str, Any], ctx: ToolContext) -> Any:
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "'name' is required"}
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id), "app_close", {"name": name}, timeout=15,
        )
    except RuntimeError as exc:
        return {"error": str(exc)}


async def _t_electron_list_known(args: dict[str, Any], ctx: ToolContext) -> Any:
    device_id, err = _resolve_device(ctx, args.get("device_id"), capability="cdp")
    if err:
        return {"error": err}
    try:
        return await _devices_manager(ctx).command(
            str(device_id), "app_list_known", {}, timeout=10,
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
        description="Open a URL on a connected device (launches the default browser or registered handler). For showing the user a specific page or kicking off an on-screen flow — NOT for research. To look something up, use web_search instead of opening pages to read.",
        parameters=_OPEN_URL_PARAMS,
        run=_t_device_open_url,
    ))
    registry.register(Tool(
        name="device_open_app",
        description="Launch a macOS app by name or bundle id on a connected device.",
        parameters=_OPEN_APP_PARAMS,
        run=_t_device_open_app,
    ))

    # ─── Timeline tools — route through satellites that advertise 'timeline' ──

    # Timeline: the user's SUMMARIZED activity record (activity cards +
    # play-by-play observations). This is what Sunday reads to answer "what did I
    # do" — NOT raw screenshots. Replaces the old rewind_* tools, which exposed
    # raw OCR'd frames; the brain should reason over the summarized timeline.
    def _pick_timeline_device(ctx, explicit=None):
        mgr = ctx.extras.get("devices")
        if mgr is None:
            return None, None
        for d in mgr.list_devices():
            if explicit and d["device_id"] != explicit:
                continue
            if "timeline" in (d.get("capabilities") or []):
                return mgr, d["device_id"]
        return mgr, None

    async def _timeline_proxy(method, params, ctx, explicit=None):
        mgr, did = _pick_timeline_device(ctx, explicit)
        if mgr is None:
            return {"error": "DeviceManager unavailable"}
        if did is None:
            return {"error": "no connected Mac has an activity timeline (needs macOS + Screen Recording permission)"}
        try:
            return await mgr.command(did, method, params, timeout=20)
        except RuntimeError as exc:
            return {"error": str(exc)}

    async def _t_timeline_search(args, ctx):
        q = (args.get("query") or "").strip()
        if not q:
            return {"error": "'query' is required"}
        return await _timeline_proxy("timeline_search", {"q": q, "limit": int(args.get("limit") or 20)}, ctx, args.get("device_id"))

    async def _t_timeline_activity(args, ctx):
        import time as _time
        if (args.get("date") or "").strip():
            return await _timeline_proxy("timeline_day", {"date": args["date"].strip()}, ctx, args.get("device_id"))
        hours = float(args.get("hours") or 24)
        now = _time.time()
        return await _timeline_proxy(
            "timeline_events",
            {"from_ts": now - hours * 3600, "to_ts": now, "limit": int(args.get("limit") or 100)},
            ctx, args.get("device_id"),
        )

    async def _t_timeline_moments(args, ctx):
        import time as _time
        to_ts = float(args.get("to_ts") or _time.time())
        from_ts = float(args.get("from_ts") or (to_ts - 3600))
        return await _timeline_proxy("timeline_observations", {"from_ts": from_ts, "to_ts": to_ts}, ctx, args.get("device_id"))

    async def _t_timeline_stats(args, ctx):
        return await _timeline_proxy("timeline_state", {}, ctx, args.get("device_id"))

    async def _t_timeline_capture(args, ctx):
        on = args.get("on")
        on = True if on is None else bool(on)
        return await _timeline_proxy("timeline_start" if on else "timeline_stop", {}, ctx, args.get("device_id"))

    def _resolve_block_time(val):
        """Accept a clock time ('14:00', '2pm', '2:30pm') → today's unix seconds, or
        a raw unix value passed straight through. None if unparseable."""
        import re
        import time as _time
        if val is None:
            return None
        try:
            f = float(val)
            if f > 1_000_000:      # already unix seconds
                return f
        except (TypeError, ValueError):
            pass
        s = str(val).strip().lower().replace(" ", "")
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", s)
        if not m:
            return None
        hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and hh != 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if hh > 23 or mm > 59:
            return None
        lt = _time.localtime()
        return _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))

    async def _t_timeline_block_set(args, ctx):
        label = (args.get("label") or "").strip()
        if not label:
            return {"error": "'label' is required"}
        start = _resolve_block_time(args.get("start"))
        end = _resolve_block_time(args.get("end"))
        if start is None or end is None:
            return {"error": "start/end must be a clock time like '14:00' or '2pm', or unix seconds"}
        if end <= start:
            end += 24 * 3600      # crosses midnight, e.g. '11pm to 1am'
        params = {"start_ts": start, "end_ts": end, "label": label,
                  "intent": args.get("intent"), "gcal_mode": (args.get("gcal_mode") or "none")}
        if args.get("block_id"):
            params["block_id"] = int(args["block_id"])
        return await _timeline_proxy("timeline_block_set", params, ctx, args.get("device_id"))

    async def _t_timeline_blocks(args, ctx):
        import time as _time
        if (args.get("date") or "").strip():
            try:
                y, mo, d = (int(x) for x in args["date"].strip().split("-"))
                start = _time.mktime((y, mo, d, 0, 0, 0, 0, 0, -1))
                end = start + 86400
            except Exception:  # noqa: BLE001
                return {"error": "date must be YYYY-MM-DD"}
        else:
            now = _time.time()
            start = now - 3600
            end = now + float(args.get("hours") or 24) * 3600
        return await _timeline_proxy("timeline_blocks", {"from_ts": start, "to_ts": end}, ctx, args.get("device_id"))

    async def _t_timeline_block_clear(args, ctx):
        bid = args.get("block_id")
        if not bid:
            return {"error": "'block_id' is required"}
        return await _timeline_proxy("timeline_block_clear", {"block_id": int(bid)}, ctx, args.get("device_id"))

    async def _t_timeline_now(args, ctx):
        return await _timeline_proxy("timeline_current_block", {}, ctx, args.get("device_id"))

    registry.register(Tool(
        name="timeline_search",
        description=(
            "Search the user's activity TIMELINE — the summarized record of what "
            "they actually worked on, as activity cards (title, one-line summary, "
            "apps/sites, time range). Use for 'when did I work on X', 'find when I "
            "did Y', 'that thing about Z'. Reads SUMMARIZED data — prefer this over "
            "screen-reading (device_screen_text/screenshot) for anything in the past."
        ),
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for (matches card titles/summaries)."},
            "limit": {"type": "integer", "description": "Max cards (default 20)."},
            "device_id": {"type": "string"},
        }, "required": ["query"]},
        run=_t_timeline_search,
    ))
    registry.register(Tool(
        name="timeline_activity",
        description=(
            "Get the user's activity cards for a day or a recent window — what they "
            "did, already summarized (each card: title, summary, apps, time range). "
            "USE THIS for 'what did I do today / yesterday / this morning / earlier / "
            "this week' — read the timeline, do NOT screenshot the screen for PAST "
            "activity. Pass `date` (YYYY-MM-DD) for a day, or `hours` for the last N "
            "hours (default 24)."
        ),
        parameters={"type": "object", "properties": {
            "date": {"type": "string", "description": "A local day (YYYY-MM-DD). Omit to use `hours`."},
            "hours": {"type": "number", "description": "Look back this many hours (default 24). Ignored if `date` set."},
            "limit": {"type": "integer", "description": "Max cards (default 100)."},
            "device_id": {"type": "string"},
        }},
        run=_t_timeline_activity,
    ))
    registry.register(Tool(
        name="timeline_moments",
        description=(
            "Zoom into a time range for the minute-by-minute play-by-play — the "
            "detailed observations behind the cards ('searched X, watched Y…'). Use "
            "after timeline_activity/timeline_search when the user wants the granular "
            "detail of a session. Pass from_ts/to_ts (unix seconds)."
        ),
        parameters={"type": "object", "properties": {
            "from_ts": {"type": "number", "description": "Start (unix seconds)."},
            "to_ts": {"type": "number", "description": "End (unix seconds)."},
            "device_id": {"type": "string"},
        }},
        run=_t_timeline_moments,
    ))
    registry.register(Tool(
        name="timeline_stats",
        description=(
            "Status of the user's activity timeline — capture on/off, how many "
            "activity cards + frames, how many still being summarized, and which "
            "summarizer is running. Use for 'is my timeline on / working'."
        ),
        parameters={"type": "object", "properties": {"device_id": {"type": "string"}}},
        run=_t_timeline_stats,
    ))
    registry.register(Tool(
        name="timeline_capture",
        description=(
            "Turn the user's activity-timeline screen capture on or off on the "
            "connected Mac. `on: true` starts it, `on: false` stops it."
        ),
        parameters={"type": "object", "properties": {
            "on": {"type": "boolean", "description": "true to start capture, false to stop."},
            "device_id": {"type": "string"},
        }},
        run=_t_timeline_capture,
    ))
    registry.register(Tool(
        name="timeline_block_set",
        description=(
            "Create (or update) a TIMEBLOCK — the user's intention for a stretch of "
            "time, like 'now until 2: Gravity deep work' or 'tonight: off computer'. "
            "Blocks are private/local by default (not on their calendar) and show in "
            "the menu bar as a live contract. USE THIS when the user wants to plan or "
            "protect time without making a calendar event. Set gcal_mode to mirror it "
            "to Google Calendar: 'none' (private, default), 'busy' (opaque busy block), "
            "or 'event' (full titled event)."
        ),
        parameters={"type": "object", "properties": {
            "label": {"type": "string", "description": "Short name, e.g. 'Gravity deep work'."},
            "start": {"type": "string", "description": "Clock time like '14:00' or '2pm' (today), or unix seconds."},
            "end": {"type": "string", "description": "Clock time like '16:00' or '4pm', or unix seconds."},
            "intent": {"type": "string", "description": "Optional: what/why — used later to check if actual activity matches."},
            "gcal_mode": {"type": "string", "enum": ["none", "busy", "event"], "description": "Calendar mirroring (default none = private)."},
            "block_id": {"type": "integer", "description": "Pass to edit an existing block instead of creating one."},
            "device_id": {"type": "string"},
        }, "required": ["label", "start", "end"]},
        run=_t_timeline_block_set,
    ))
    registry.register(Tool(
        name="timeline_blocks",
        description=(
            "List the user's timeblocks (their planned intentions) for today or a "
            "window. Use for 'what's my plan', 'what am I supposed to be doing', "
            "'what's my next block'. Pass `date` (YYYY-MM-DD) for a day, else it "
            "returns from an hour ago through the next `hours` (default 24)."
        ),
        parameters={"type": "object", "properties": {
            "date": {"type": "string", "description": "A local day (YYYY-MM-DD). Omit for the upcoming window."},
            "hours": {"type": "number", "description": "Look ahead this many hours (default 24). Ignored if `date` set."},
            "device_id": {"type": "string"},
        }},
        run=_t_timeline_blocks,
    ))
    registry.register(Tool(
        name="timeline_block_clear",
        description="Delete a timeblock by its id (get ids from timeline_blocks).",
        parameters={"type": "object", "properties": {
            "block_id": {"type": "integer", "description": "The block's id."},
            "device_id": {"type": "string"},
        }, "required": ["block_id"]},
        run=_t_timeline_block_clear,
    ))
    registry.register(Tool(
        name="timeline_now",
        description=(
            "What the user is supposed to be doing RIGHT NOW: the current timeblock "
            "(+ minutes left), the next block, and a drift verdict — whether their "
            "actual recent activity matches the block's intention (on_track + a short "
            "note). Use for 'what am I meant to be doing', 'am I on track', 'am I "
            "focused'. The drift read compares intention against the observed timeline."
        ),
        parameters={"type": "object", "properties": {"device_id": {"type": "string"}}},
        run=_t_timeline_now,
    ))
    registry.register(Tool(
        name="device_run_command",
        description="Run a shell command on a specific connected device. Returns stdout/stderr/exit_code.",
        parameters=_RUN_COMMAND_PARAMS,
        run=_t_device_run_command,
    ))
    registry.register(Tool(
        name="mac_startup_items",
        description=(
            "List what actually auto-starts / launches at login on the Mac — "
            "login items, startup apps, background agents and daemons. Reads the "
            "authoritative macOS Background Task Management database (sfltool "
            "dumpbtm) plus LaunchAgents/Daemons, and returns a clean structured "
            "list. USE THIS for any 'what starts when I boot / what's set to "
            "auto-start / login items / startup programs' question — do NOT list "
            "/Applications (that's just installed apps) or hand-write AppleScript."
        ),
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM}},
        run=_t_mac_startup_items,
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
            "isolated shadow profile on a connected device, optionally navigating to a URL. "
            "For background INTERACTION with a site (logging in, filling forms, automating a "
            "flow) — not for research. To look something up, use web_search, not a browser."
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
        description="Evaluate a JavaScript expression in the browser on a device. Low-level escape hatch; prefer browser_read/click/type.",
        parameters=_CDP_EVAL_PARAMS,
        run=_t_cdp_evaluate,
    ))
    registry.register(Tool(
        name="browser_read",
        description=(
            "Read the current page in Sunday's OWN headless shadow browser — "
            "NOT the user's Chrome. It cannot see the user's open tabs and has "
            "its own separate logins. For anything in the user's real browser "
            "('this page', 'this tab', signed-in sites) use cockpit_read_page "
            "instead. This one is for background jobs and Electron apps "
            "(electron_launch). Returns title, url, visible text, and "
            "interactive elements each with a numeric `ref` for "
            "browser_click/browser_type. Launch one first with "
            "device_cdp_launch if none is open. For background INTERACTION with a "
            "site, not research — to look something up, use web_search instead."
        ),
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "profile_id": {"type": "string"}}},
        run=_t_browser_read,
    ))
    registry.register(Tool(
        name="browser_click",
        description="Click an element in Sunday's own shadow browser (NOT the user's Chrome — use cockpit_click for that) by its `ref` from browser_read (or a CSS `selector`). Re-read the page after to see what changed.",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "profile_id": {"type": "string"}, "ref": {"type": "integer"}, "selector": {"type": "string"}}},
        run=_t_browser_click,
    ))
    registry.register(Tool(
        name="browser_type",
        description="Type text into a field in Sunday's own shadow browser (NOT the user's Chrome — use cockpit_fill for that) by its `ref` from browser_read. Set submit=true to press Enter after (e.g. search boxes).",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "profile_id": {"type": "string"}, "ref": {"type": "integer"}, "text": {"type": "string"}, "submit": {"type": "boolean"}}, "required": ["ref", "text"]},
        run=_t_browser_type,
    ))
    registry.register(Tool(
        name="browser_key",
        description="Press a key in the browser (Enter, Tab, Escape, Backspace, ArrowDown, ArrowUp).",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "profile_id": {"type": "string"}, "key": {"type": "string"}}},
        run=_t_browser_key,
    ))
    registry.register(Tool(
        name="app_snapshot",
        description=(
            "Read the UI of whatever app is in front on the Mac — every button, "
            "field, menu, link with its label and on-screen position (x,y). This "
            "is your eyes on ANY native app (Messages, Finder, Notes, anything), "
            "not just the browser. Call it, then app_click(x,y) / app_type / "
            "app_key to operate. Bring an app to the front first with device_open_app."
        ),
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM}},
        run=_t_app_snapshot,
    ))
    registry.register(Tool(
        name="app_click",
        description="Click at a screen point on the Mac (use the x,y of an element from app_snapshot). Then app_snapshot again to see the result.",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "x": {"type": "number"}, "y": {"type": "number"}}, "required": ["x", "y"]},
        run=_t_app_click,
    ))
    registry.register(Tool(
        name="app_type",
        description="Type text into the focused field of the frontmost app (click the field first with app_click).",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "text": {"type": "string"}}, "required": ["text"]},
        run=_t_app_type,
    ))
    registry.register(Tool(
        name="app_key",
        description="Press a key or combo on the Mac: 'enter', 'tab', 'escape', 'cmd+t', 'cmd+shift+4', 'cmd+c', etc. Drives menus and shortcuts in any app.",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM, "combo": {"type": "string"}}, "required": ["combo"]},
        run=_t_app_key,
    ))

    # ─── Electron app control via CDP ────────────────────────────────────
    # Slack, Discord, VS Code, Cursor, Notion desktop, Linear, Spotify, …
    # Every Electron app accepts --remote-debugging-port at launch; we
    # restart the app with the flag, then drive it through the SAME
    # browser_read / browser_click / browser_type tools above using
    # profile_id="app:<name>".
    registry.register(Tool(
        name="electron_launch",
        description=(
            "Launch a desktop app (Slack, Discord, VS Code, Cursor, Notion, "
            "Linear, Spotify, Figma, Obsidian, Claude) with Chrome DevTools "
            "enabled, then drive it like a browser: pass the returned "
            "profile_id to browser_read / browser_click / browser_type. "
            "If the app is already running, Sunday quits and relaunches it "
            "so the debug flag applies — give the user a heads-up first if "
            "they might lose drafts. Uses the app's NATIVE data directory, "
            "so all the user's logins and workspaces come along."
        ),
        parameters={"type": "object", "properties": {
            **_DEVICE_ID_PARAM,
            "name": {"type": "string", "description": "App key: slack, discord, vscode, cursor, notion, linear, spotify, figma, obsidian, claude."},
        }, "required": ["name"]},
        run=_t_electron_launch,
    ))
    registry.register(Tool(
        name="electron_close",
        description="Quit an electron app Sunday launched via electron_launch.",
        parameters={"type": "object", "properties": {
            **_DEVICE_ID_PARAM,
            "name": {"type": "string"},
        }, "required": ["name"]},
        run=_t_electron_close,
    ))
    registry.register(Tool(
        name="electron_list_known",
        description="List the desktop apps Sunday knows how to launch + drive via CDP, and which are actually installed on this Mac.",
        parameters={"type": "object", "properties": {**_DEVICE_ID_PARAM}},
        run=_t_electron_list_known,
    ))
