"""Chrome DevTools Protocol with a shadow profile.

Launches Chrome (or any Chromium-based Electron app you point us at) with
an isolated --user-data-dir so we don't pollute the user's real browser
profile, plus --remote-debugging-port so we can drive it.

Used by the satellite daemon to let Sunday automate browsers + Electron
apps on any machine she's installed on. Kept self-contained — no
playwright dep — to keep the satellite tiny.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx


@dataclass
class CdpSession:
    profile_id: str
    port: int
    process: asyncio.subprocess.Process
    user_data_dir: Path
    target_ws_cache: dict[str, str] = field(default_factory=dict)


_SESSIONS: dict[str, CdpSession] = {}
_DEFAULT_PORT = 19222


def _find_browser(custom_path: str | None = None) -> str:
    if custom_path and Path(custom_path).exists():
        return custom_path
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Arc.app/Contents/MacOS/Arc",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chrome"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise RuntimeError(
        "No Chromium-based browser found. Install Google Chrome, Chromium, "
        "Edge, or Arc — or pass `browser_path` explicitly when launching."
    )


async def launch(
    profile_id: str = "default",
    start_url: str | None = None,
    port: int | None = None,
    browser_path: str | None = None,
    extra_args: list[str] | None = None,
) -> dict:
    """Launch a Chromium-based browser in a shadow profile.

    Returns {profile_id, port, ws_url}. The browser stays alive after this
    coroutine returns; call close() to terminate.
    """
    if profile_id in _SESSIONS:
        # Idempotent — already launched. Optionally navigate the existing
        # session to start_url.
        session = _SESSIONS[profile_id]
        if start_url:
            await navigate(profile_id, start_url)
        return {"profile_id": profile_id, "port": session.port, "reused": True}

    port = port or (_DEFAULT_PORT + (abs(hash(profile_id)) % 1000))
    user_data_dir = Path(f"/tmp/sunday-shadow-{profile_id}")
    user_data_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        _find_browser(browser_path),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,InfiniteSessionRestore",
        *(extra_args or []),
        start_url or "about:blank",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Wait for the debug port to come up
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                res = await client.get(f"http://127.0.0.1:{port}/json/version")
                if res.status_code == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    else:
        proc.kill()
        raise RuntimeError(f"CDP port {port} never came up; browser failed to start")

    _SESSIONS[profile_id] = CdpSession(
        profile_id=profile_id,
        port=port,
        process=proc,
        user_data_dir=user_data_dir,
    )
    return {"profile_id": profile_id, "port": port}


async def close(profile_id: str = "default") -> dict:
    session = _SESSIONS.pop(profile_id, None)
    if session is None:
        return {"ok": True, "note": f"no session for {profile_id}"}
    try:
        session.process.terminate()
        try:
            await asyncio.wait_for(session.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            session.process.kill()
    except ProcessLookupError:
        pass
    return {"ok": True, "profile_id": profile_id}


async def _page_target_ws(session: CdpSession) -> str:
    async with httpx.AsyncClient(timeout=5) as client:
        res = await client.get(f"http://127.0.0.1:{session.port}/json")
        res.raise_for_status()
        targets = res.json()
    page = next((t for t in targets if t.get("type") == "page"), None)
    if not page:
        raise RuntimeError("no page target available")
    ws_url = page.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("page target has no debugger URL")
    return ws_url


async def _cdp_call(ws_url: str, method: str, params: dict | None = None) -> dict:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required for CDP. pip install websockets") from exc

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
        msg_id = 1
        await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(err.get("message", str(err)))
                return msg.get("result", {})
    raise RuntimeError("CDP connection closed without a response")


async def navigate(profile_id: str, url: str) -> dict:
    session = _SESSIONS.get(profile_id)
    if session is None:
        raise RuntimeError(f"no CDP session for profile: {profile_id}")
    ws_url = await _page_target_ws(session)
    return await _cdp_call(ws_url, "Page.navigate", {"url": url})


async def screenshot(profile_id: str) -> dict:
    session = _SESSIONS.get(profile_id)
    if session is None:
        raise RuntimeError(f"no CDP session for profile: {profile_id}")
    ws_url = await _page_target_ws(session)
    result = await _cdp_call(ws_url, "Page.captureScreenshot", {"format": "png"})
    # result['data'] is base64 PNG.
    return {"image_b64": result.get("data"), "mime": "image/png"}


async def evaluate(profile_id: str, expression: str) -> dict:
    session = _SESSIONS.get(profile_id)
    if session is None:
        raise RuntimeError(f"no CDP session for profile: {profile_id}")
    ws_url = await _page_target_ws(session)
    return await _cdp_call(
        ws_url,
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
    )
