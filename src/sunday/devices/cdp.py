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


def _provision_login(dest_user_data_dir: Path) -> None:
    """Best-effort: seed Sunday's browser profile from the user's real Chrome
    so it's logged into the same accounts. Cookies are encrypted with the
    machine's 'Chrome Safe Storage' keychain key, shared across profiles on
    the same user — so a copied cookie store decrypts fine here. If Chrome
    is open the DB may be locked; that's fine, the user can log in once and
    it persists in this profile."""
    src_root = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    src_default = src_root / "Default"
    if not src_default.exists():
        return
    dest_default = dest_user_data_dir / "Default"
    dest_default.mkdir(parents=True, exist_ok=True)
    # Local State (root) holds the encrypted-key reference.
    if (src_root / "Local State").exists():
        shutil.copy2(src_root / "Local State", dest_user_data_dir / "Local State")
    # Cookies live under Default/Network/ in current Chrome.
    for rel in ("Network/Cookies", "Network/Cookies-journal"):
        s = src_default / rel
        if s.exists():
            (dest_default / "Network").mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, dest_default / rel)
    # Local/Session storage carries token-based logins for many web apps.
    for d in ("Local Storage", "Session Storage"):
        s = src_default / d
        if s.exists():
            shutil.copytree(s, dest_default / d, dirs_exist_ok=True)


def cleanup_stale_profiles(max_age_days: float = 14.0, root: Path | None = None) -> dict:
    """Garbage-collect one-off task browser profiles under ~/.sunday/chrome that
    haven't been used in `max_age_days`. Each task (dentist-…, book-…, research…)
    gets its own profile with copied logins/cache, and they pile up — this is
    usually the biggest disposable chunk of ~/.sunday. NEVER removes:
      - "default" (the primary shared profile),
      - "app:*" (real Electron-app logins the user relies on),
      - any profile with a live session right now.
    "Last used" = newest mtime among the dir + its key session files, so an
    actively-browsed profile isn't misjudged by a stale top-level dir mtime.
    `root` is injectable for tests; defaults to ~/.sunday/chrome."""
    root = root or (Path.home() / ".sunday" / "chrome")
    if not root.exists():
        return {"removed": 0, "freed_bytes": 0, "kept": 0}
    cutoff = time.time() - max_age_days * 86400
    removed, freed, kept = 0, 0, 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        if name == "default" or name.startswith("app:") or name in _SESSIONS:
            kept += 1
            continue
        last_used = 0.0
        for cand in (d, d / "Local State", d / "Default" / "Cookies", d / "Default" / "History"):
            try:
                last_used = max(last_used, cand.stat().st_mtime)
            except OSError:
                pass
        if last_used >= cutoff:
            kept += 1
            continue
        size = 0
        for p in d.rglob("*"):
            try:
                if p.is_file():
                    size += p.stat().st_size
            except OSError:
                pass
        try:
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
            freed += size
        except Exception:  # noqa: BLE001
            pass
    return {"removed": removed, "freed_bytes": freed, "kept": kept}


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
    # Persistent profile so logins stick across launches (was a throwaway
    # /tmp dir — never logged in, which is why docs/Loom/Gmail bounced it).
    user_data_dir = Path.home() / ".sunday" / "chrome" / profile_id
    fresh = not (user_data_dir / "Default").exists()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    # On first creation, best-effort copy the user's real Chrome session so
    # Sunday's browser is logged into the same accounts the user is.
    if fresh:
        try:
            _provision_login(user_data_dir)
        except Exception:  # noqa: BLE001 — best effort; user can log in once
            pass

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


async def _eval(profile_id: str, expression: str):
    """Evaluate JS and return the unwrapped value (raises on JS error)."""
    res = await evaluate(profile_id, expression)
    if res.get("exceptionDetails"):
        raise RuntimeError(res["exceptionDetails"].get("text", "JS error"))
    return (res.get("result") or {}).get("value")


# JS that tags interactive elements with data-snd refs and returns a
# readable snapshot the model can act on (text + a list of click/type targets).
_SNAPSHOT_JS = r"""
(() => {
  const out = { title: document.title, url: location.href, elements: [] };
  const sel = 'a,button,input,textarea,select,[role=button],[role=link],[role=textbox],[contenteditable=true],[onclick]';
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;       // skip hidden
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    el.setAttribute('data-snd', String(i));
    let label = (el.getAttribute('aria-label') || el.placeholder || el.value || el.innerText || el.alt || el.title || '').trim().replace(/\s+/g,' ').slice(0,80);
    const tag = el.tagName.toLowerCase();
    const e = { ref: i, tag, label };
    if (el.type) e.type = el.type;
    if (el.href) e.href = el.href.slice(0,200);
    out.elements.push(e);
    i++;
    if (i > 200) break;
  }
  out.text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g,'\n\n').slice(0, 9000);
  return out;
})()
"""


async def read_page(profile_id: str) -> dict:
    """Return a readable, actionable snapshot of the current page: title, url,
    visible text, and the interactive elements (each with a `ref` to click/type)."""
    snap = await _eval(profile_id, _SNAPSHOT_JS)
    return snap if isinstance(snap, dict) else {"error": "could not read page"}


async def click(profile_id: str, ref: int | None = None, selector: str | None = None) -> dict:
    """Click an element by its snapshot `ref` (preferred) or a CSS selector."""
    if ref is None and not selector:
        return {"error": "pass ref (from read_page) or selector"}
    q = f'[data-snd="{int(ref)}"]' if ref is not None else selector.replace('"', '\\"')
    js = f"""
    (() => {{
      const el = document.querySelector("{q}");
      if (!el) return {{error: "no element for {q}"}};
      el.scrollIntoView({{block:'center'}});
      el.click();
      return {{ok: true, url: location.href}};
    }})()
    """
    return await _eval(profile_id, js) or {"ok": True}


async def type_text(profile_id: str, ref: int | None, text: str, submit: bool = False) -> dict:
    """Type into an element by snapshot `ref`: focus, set value, fire input
    events. Optionally press Enter to submit."""
    if ref is None:
        return {"error": "ref is required"}
    safe = json.dumps(text)
    js = f"""
    (() => {{
      const el = document.querySelector('[data-snd="{int(ref)}"]');
      if (!el) return {{error:"no element"}};
      el.focus();
      const v = {safe};
      if (el.isContentEditable) {{ el.textContent = v; }}
      else {{ el.value = v; }}
      el.dispatchEvent(new Event('input', {{bubbles:true}}));
      el.dispatchEvent(new Event('change', {{bubbles:true}}));
      return {{ok:true}};
    }})()
    """
    res = await _eval(profile_id, js) or {"ok": True}
    if submit and not res.get("error"):
        await press_key(profile_id, "Enter")
    return res


_KEYS = {
    "Enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13},
    "Tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "Escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "Backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "ArrowDown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "ArrowUp": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
}


async def press_key(profile_id: str, key: str) -> dict:
    """Press a key (Enter/Tab/Escape/…) as a real input event via CDP."""
    session = _SESSIONS.get(profile_id)
    if session is None:
        raise RuntimeError(f"no CDP session for profile: {profile_id}")
    spec = _KEYS.get(key)
    if not spec:
        return {"error": f"unsupported key {key}; supported: {list(_KEYS)}"}
    ws_url = await _page_target_ws(session)
    await _cdp_call(ws_url, "Input.dispatchKeyEvent", {"type": "keyDown", **spec})
    await _cdp_call(ws_url, "Input.dispatchKeyEvent", {"type": "keyUp", **spec})
    return {"ok": True, "key": key}
