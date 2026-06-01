"""Sunday satellite — runs on a remote machine and connects back to main Sunday.

Install Sunday on any Mac (and later, Linux/Windows), run `sunday-satellite`,
and it shows up in Sunday's device list. The main brain can then take
screenshots, run commands, and drive a shadow-profile Chrome on that
machine via tools — same shape as everything else.

Usage:
    sunday-satellite --server ws://<main-host>:8765/v1/devices/ws \\
                     --device-id mac-studio \\
                     --token <daemon-auth-token>

The daemon authenticates the device connection with its bearer token (the
one at ~/.sunday/auth.token on the daemon host). Resolution order:

    --token  >  $SUNDAY_AUTH_TOKEN  >  local ~/.sunday/auth.token

so a satellite running on the SAME machine as the daemon needs no flag at
all, while a satellite on another box must be given the daemon's token
explicitly (copy it from the daemon host, or set SUNDAY_AUTH_TOKEN).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import platform
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog

from sunday.devices import cdp
from sunday.devices import control_macos
from sunday.devices import imessage_macos
from sunday.devices import rewind_macos
from sunday.devices.protocol import event_frame, register_frame, response_frame

log = structlog.get_logger("sunday.satellite")


def _capabilities() -> list[str]:
    """Capabilities this satellite advertises. Each is opt-in by environment
    so Linux/non-Mac satellites simply don't claim them."""
    caps = ["shell", "screen", "cdp"]
    if imessage_macos.is_available():
        caps.append("imessage")
    if rewind_macos.is_available():
        caps.append("rewind")
    if control_macos.is_available():
        caps.append("control")
    return caps


# ─── handlers (commands the satellite responds to) ──────────────────────


async def _h_run_command(params: dict[str, Any]) -> dict[str, Any]:
    command = params.get("command")
    if not command:
        raise ValueError("'command' is required")
    cwd = params.get("cwd")
    timeout = float(params.get("timeout") or 60)

    proc = await asyncio.create_subprocess_shell(
        str(command),
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"error": "timeout", "exit_code": -1}
    return {
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
        "exit_code": proc.returncode,
    }


async def _h_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    if sys.platform != "darwin":
        return {"error": "screenshot not implemented for this platform yet"}
    out = Path(f"/tmp/sunday-cap-{uuid.uuid4().hex[:8]}.png")
    proc = await asyncio.create_subprocess_exec(
        "/usr/sbin/screencapture", "-x", "-t", "png", str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        raw = err.decode("utf-8", errors="replace").strip()
        if "could not create image" in raw.lower() or not raw:
            return {"error": (
                "SCREEN_RECORDING_DENIED: macOS hasn't granted Screen Recording "
                "permission to the Sunday satellite. Open System Settings → Privacy "
                "& Security → Screen Recording, enable Sunday (or the satellite), then "
                "restart it. This is not a problem you can retry around."
            )}
        return {"error": raw or "screencapture failed"}
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    return {"image_b64": base64.b64encode(data).decode("ascii"), "mime": "image/png"}


async def _h_cdp_launch(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.launch(
        profile_id=params.get("profile_id", "default"),
        start_url=params.get("url"),
        port=params.get("port"),
        browser_path=params.get("browser_path"),
        extra_args=params.get("extra_args") or [],
    )


async def _h_cdp_navigate(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.navigate(
        profile_id=params.get("profile_id", "default"),
        url=params.get("url", ""),
    )


async def _h_cdp_screenshot(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.screenshot(profile_id=params.get("profile_id", "default"))


async def _h_cdp_evaluate(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.evaluate(
        profile_id=params.get("profile_id", "default"),
        expression=params.get("expression", ""),
    )


async def _h_cdp_close(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.close(profile_id=params.get("profile_id", "default"))


# ─── Electron app control via CDP ─────────────────────────────────────────
# These piggyback on the cdp.py session model. Launching an Electron app
# registers a CdpSession under `app:<name>`, so the existing _h_cdp_read /
# _h_cdp_click / _h_cdp_type handlers work against it with no changes — the
# agent just passes profile_id="app:slack" (or whatever) and drives it like
# any other Chromium target.

async def _h_app_launch(params: dict[str, Any]) -> dict[str, Any]:
    from sunday.devices import electron_apps
    return await electron_apps.launch_app(
        name=params.get("name", ""),
        port=params.get("port"),
    )


async def _h_app_close(params: dict[str, Any]) -> dict[str, Any]:
    from sunday.devices import electron_apps
    return await electron_apps.close_app(name=params.get("name", ""))


async def _h_app_list_known(params: dict[str, Any]) -> dict[str, Any]:
    from sunday.devices import electron_apps
    return {"apps": electron_apps.list_known()}


async def _h_cdp_read(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.read_page(profile_id=params.get("profile_id", "default"))


async def _h_cdp_click(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.click(
        profile_id=params.get("profile_id", "default"),
        ref=params.get("ref"),
        selector=params.get("selector"),
    )


async def _h_cdp_type(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.type_text(
        profile_id=params.get("profile_id", "default"),
        ref=params.get("ref"),
        text=params.get("text", ""),
        submit=bool(params.get("submit")),
    )


async def _h_cdp_key(params: dict[str, Any]) -> dict[str, Any]:
    return await cdp.press_key(
        profile_id=params.get("profile_id", "default"),
        key=params.get("key", "Enter"),
    )


# ─── desktop control (macOS `open`) ─────────────────────────────────────


async def _h_open_url(params: dict[str, Any]) -> dict[str, Any]:
    url = params.get("url")
    if not url:
        return {"error": "'url' is required"}
    proc = await asyncio.create_subprocess_exec(
        "open", str(url),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        return {"error": err.decode("utf-8", errors="replace").strip()}
    return {"ok": True, "url": url}


async def _h_open_app(params: dict[str, Any]) -> dict[str, Any]:
    app = params.get("app")
    if not app:
        return {"error": "'app' is required (name like 'Safari' or bundle id like 'com.apple.Safari')"}
    args = params.get("args") or []
    cmd = ["open", "-a", str(app)]
    if args:
        cmd.append("--args")
        cmd.extend(str(a) for a in args)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        return {"error": err.decode("utf-8", errors="replace").strip()}
    return {"ok": True, "app": app}


# ─── iMessage handlers (macOS only) ──────────────────────────────────────


async def _h_imessage_list_threads(params: dict[str, Any]) -> dict[str, Any]:
    return {"threads": imessage_macos.list_threads(int(params.get("limit") or 20))}


async def _h_imessage_read_thread(params: dict[str, Any]) -> dict[str, Any]:
    chat_id = params.get("chat_identifier")
    if not chat_id:
        return {"error": "'chat_identifier' is required"}
    return {"messages": imessage_macos.read_thread(str(chat_id), int(params.get("limit") or 30))}


async def _h_imessage_read_recent(params: dict[str, Any]) -> dict[str, Any]:
    return {"messages": imessage_macos.read_recent(int(params.get("limit") or 20))}


async def _h_imessage_search(params: dict[str, Any]) -> dict[str, Any]:
    query = params.get("query")
    if not query:
        return {"error": "'query' is required"}
    return {"matches": imessage_macos.search_messages(str(query), int(params.get("limit") or 20))}


async def _h_imessage_send(params: dict[str, Any]) -> dict[str, Any]:
    to = params.get("to")
    if not to:
        return {"error": "'to' is required"}
    body = params.get("body") or ""
    atts = params.get("attachments") or []
    if not body and not atts:
        return {"error": "'body' or 'attachments' is required"}
    return await imessage_macos.send_imessage(str(to), str(body), [str(p) for p in atts])


# ─── Rewind handlers (macOS only) ────────────────────────────────────────


async def _h_rewind_start(params: dict[str, Any]) -> dict[str, Any]:
    interval = float(params.get("interval_seconds") or rewind_macos.DEFAULT_INTERVAL_SECONDS)
    return rewind_macos.start(interval=interval)


async def _h_rewind_stop(params: dict[str, Any]) -> dict[str, Any]:
    return rewind_macos.stop()


async def _h_rewind_search(params: dict[str, Any]) -> dict[str, Any]:
    q = (params.get("query") or "").strip()
    if not q:
        return {"error": "'query' is required"}
    return {"frames": rewind_macos.search(q, int(params.get("limit") or 10))}


async def _h_rewind_recent(params: dict[str, Any]) -> dict[str, Any]:
    return {"frames": rewind_macos.recent(int(params.get("limit") or 10))}


async def _h_rewind_stats(params: dict[str, Any]) -> dict[str, Any]:
    return rewind_macos.stats()


async def _h_screen_text(params: dict[str, Any]) -> dict[str, Any]:
    """Capture the screen and return its text via local Vision OCR — lets a
    text-only brain read what's on screen without an image round-trip."""
    return await rewind_macos.capture_text()


# ─── native UI control (macOS Accessibility) ─────────────────────────────


async def _h_ax_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    return await control_macos.snapshot()


async def _h_ax_click(params: dict[str, Any]) -> dict[str, Any]:
    return await control_macos.click(float(params.get("x", 0)), float(params.get("y", 0)))


async def _h_ax_type(params: dict[str, Any]) -> dict[str, Any]:
    return await control_macos.type_text(str(params.get("text", "")))


async def _h_ax_key(params: dict[str, Any]) -> dict[str, Any]:
    return await control_macos.key(str(params.get("combo", "")))


HANDLERS = {
    "run_command":            _h_run_command,
    "screenshot":             _h_screenshot,
    "open_url":               _h_open_url,
    "open_app":               _h_open_app,
    "cdp_launch":             _h_cdp_launch,
    "cdp_navigate":           _h_cdp_navigate,
    "cdp_screenshot":         _h_cdp_screenshot,
    "cdp_evaluate":           _h_cdp_evaluate,
    "cdp_close":              _h_cdp_close,
    "cdp_read":               _h_cdp_read,
    "cdp_click":              _h_cdp_click,
    "cdp_type":               _h_cdp_type,
    "cdp_key":                _h_cdp_key,
    "app_launch":             _h_app_launch,
    "app_close":              _h_app_close,
    "app_list_known":         _h_app_list_known,
    "imessage_list_threads":  _h_imessage_list_threads,
    "imessage_read_thread":   _h_imessage_read_thread,
    "imessage_read_recent":   _h_imessage_read_recent,
    "imessage_search":        _h_imessage_search,
    "imessage_send":          _h_imessage_send,
    "rewind_start":           _h_rewind_start,
    "rewind_stop":            _h_rewind_stop,
    "rewind_search":          _h_rewind_search,
    "rewind_recent":          _h_rewind_recent,
    "rewind_stats":           _h_rewind_stats,
    "screen_text":            _h_screen_text,
    "ax_snapshot":            _h_ax_snapshot,
    "ax_click":               _h_ax_click,
    "ax_type":                _h_ax_type,
    "ax_key":                 _h_ax_key,
}


# ─── main loop ──────────────────────────────────────────────────────────


def _resolve_token(cli_token: str | None) -> str | None:
    """Find the daemon's bearer token: explicit flag, then env, then the
    local token file (covers a satellite co-located with the daemon)."""
    for candidate in (cli_token, os.environ.get("SUNDAY_AUTH_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    try:
        from sunday.paths import auth_token_path
        p = auth_token_path()
        if p.exists():
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
    except Exception:  # noqa: BLE001
        pass
    return None


def _handshake_status(exc: Exception) -> int | None:
    """Pull the HTTP status out of a websockets handshake rejection, across
    library versions (v13's InvalidStatus.response.status_code and v12's
    InvalidStatusCode.status_code)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None) if resp is not None else None
    return code if code is not None else getattr(exc, "status_code", None)


async def _serve(server_url: str, device_id: str, token: str | None) -> None:
    try:
        import websockets
    except ImportError:
        log.error(
            "websockets package not installed. Install Sunday with the devices extra: "
            "pip install -e '.[devices]'"
        )
        sys.exit(1)

    headers = [("Authorization", f"Bearer {token}")] if token else []

    while True:
        try:
            async with websockets.connect(
                server_url,
                additional_headers=headers,
                max_size=32 * 1024 * 1024,
                # 20s ping cadence — well under any proxy idle timeout
                # (Cloudflare's is 100s for unproxied, Caddy default 60s).
                # ping_timeout high enough that a slow pong doesn't kill us.
                ping_interval=20,
                ping_timeout=60,
                close_timeout=10,
            ) as ws:
                log.info("connected", server=server_url, device_id=device_id)
                caps = _capabilities()
                log.info("registering", device_id=device_id, capabilities=caps)
                await ws.send(_jsondumps(register_frame(
                    device_id=device_id,
                    capabilities=caps,
                    platform_name=platform.platform(),
                )))
                # Resume Rewind on registration if the user previously opted in.
                if "rewind" in caps:
                    try:
                        result = rewind_macos.auto_start_if_enabled()
                        if result.get("ok") and result.get("enabled") is not False:
                            log.info("rewind watcher resumed", interval_s=result.get("interval_s"))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("rewind auto-start failed", error=str(exc))

                async for raw in ws:
                    try:
                        msg = _jsonloads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if msg.get("type") == "command":
                        asyncio.create_task(_dispatch(ws, msg))
        except KeyboardInterrupt:
            log.info("shutting down")
            return
        except Exception as exc:  # noqa: BLE001
            status = _handshake_status(exc)
            if status in (401, 403):
                log.error(
                    "daemon rejected the device connection — bad or missing auth token. "
                    "Pass the daemon's token via --token or SUNDAY_AUTH_TOKEN "
                    "(it's the value in ~/.sunday/auth.token on the daemon host). "
                    "Retrying in 30s.",
                    status=status,
                )
                await asyncio.sleep(30)
            else:
                log.warning("connection lost, reconnecting in 5s", error=str(exc))
                await asyncio.sleep(5)


async def _dispatch(ws: Any, cmd: dict[str, Any]) -> None:
    req_id = cmd.get("id") or ""
    method = cmd.get("method") or ""
    params = cmd.get("params") or {}
    handler = HANDLERS.get(method)
    if handler is None:
        await ws.send(_jsondumps(response_frame(req_id, error=f"unknown method: {method}")))
        return
    try:
        result = await handler(params)
        await ws.send(_jsondumps(response_frame(req_id, result=result)))
    except Exception as exc:  # noqa: BLE001
        log.exception("handler failed", method=method)
        await ws.send(_jsondumps(response_frame(req_id, error=f"{type(exc).__name__}: {exc}")))


def _jsondumps(obj: Any) -> str:
    import json
    return json.dumps(obj, separators=(",", ":"))


def _jsonloads(raw: Any) -> dict:
    import json
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    return json.loads(raw)


# ─── entrypoint ─────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(prog="sunday-satellite", description=__doc__)
    parser.add_argument("--server", required=True, help="ws://host:port/v1/devices/ws of the main Sunday daemon.")
    parser.add_argument("--device-id", default=platform.node(), help="Identifier for this machine.")
    parser.add_argument(
        "--token",
        default=None,
        help="Daemon bearer token. Falls back to $SUNDAY_AUTH_TOKEN, then the "
             "local ~/.sunday/auth.token (so a co-located satellite needs none).",
    )
    args = parser.parse_args()

    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])

    token = _resolve_token(args.token)
    if not token:
        log.warning(
            "no auth token found (looked at --token, $SUNDAY_AUTH_TOKEN, and "
            "~/.sunday/auth.token) — the daemon will reject this connection. "
            "Copy the daemon's token from ~/.sunday/auth.token on its host."
        )

    try:
        asyncio.run(_serve(args.server, args.device_id, token))
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
