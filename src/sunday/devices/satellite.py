"""Sunday satellite — runs on a remote machine and connects back to main Sunday.

Install Sunday on any Mac (and later, Linux/Windows), run `sunday-satellite`,
and it shows up in Sunday's device list. The main brain can then take
screenshots, run commands, and drive a shadow-profile Chrome on that
machine via tools — same shape as everything else.

Usage:
    sunday-satellite --server ws://<main-host>:8765/v1/devices/ws \\
                     --device-id mac-studio \\
                     --token <shared-secret>
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import platform
import sys
import uuid
from pathlib import Path
from typing import Any

import structlog

from sunday.devices import cdp
from sunday.devices import imessage_macos
from sunday.devices.protocol import event_frame, register_frame, response_frame

log = structlog.get_logger("sunday.satellite")


def _capabilities() -> list[str]:
    """Capabilities this satellite advertises. iMessage is opt-in based on
    chat.db being readable (so Linux/non-Mac satellites simply don't claim it)."""
    caps = ["shell", "screen", "cdp"]
    if imessage_macos.is_available():
        caps.append("imessage")
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
        "screencapture", "-x", "-t", "png", str(out),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _out, err = await proc.communicate()
    if proc.returncode != 0:
        return {"error": err.decode("utf-8", errors="replace").strip() or "screencapture failed"}
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


async def _h_imessage_send(params: dict[str, Any]) -> dict[str, Any]:
    to = params.get("to")
    if not to:
        return {"error": "'to' is required"}
    body = params.get("body") or ""
    atts = params.get("attachments") or []
    if not body and not atts:
        return {"error": "'body' or 'attachments' is required"}
    return await imessage_macos.send_imessage(str(to), str(body), [str(p) for p in atts])


HANDLERS = {
    "run_command":            _h_run_command,
    "screenshot":             _h_screenshot,
    "cdp_launch":             _h_cdp_launch,
    "cdp_navigate":           _h_cdp_navigate,
    "cdp_screenshot":         _h_cdp_screenshot,
    "cdp_evaluate":           _h_cdp_evaluate,
    "cdp_close":              _h_cdp_close,
    "imessage_list_threads":  _h_imessage_list_threads,
    "imessage_read_thread":   _h_imessage_read_thread,
    "imessage_read_recent":   _h_imessage_read_recent,
    "imessage_send":          _h_imessage_send,
}


# ─── main loop ──────────────────────────────────────────────────────────


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
                ping_interval=30,
            ) as ws:
                log.info("connected", server=server_url, device_id=device_id)
                caps = _capabilities()
                log.info("registering", device_id=device_id, capabilities=caps)
                await ws.send(_jsondumps(register_frame(
                    device_id=device_id,
                    capabilities=caps,
                    platform_name=platform.platform(),
                )))

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
    parser.add_argument("--token", default=None, help="Optional shared secret bearer token.")
    args = parser.parse_args()

    structlog.configure(processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ])

    try:
        asyncio.run(_serve(args.server, args.device_id, args.token))
    except KeyboardInterrupt:
        log.info("bye")


if __name__ == "__main__":
    main()
