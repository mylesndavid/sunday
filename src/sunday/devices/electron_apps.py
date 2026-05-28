"""Launch + drive Electron apps via CDP.

Every Electron app (Slack, Discord, VS Code, Cursor, Notion desktop, Linear,
Spotify, …) accepts `--remote-debugging-port=<port>` at launch and exposes
the full Chrome DevTools Protocol on that port — same protocol Sunday already
speaks for its Chromium browser sessions.

The flow:

  app_launch("slack")
    → quit the running Slack instance (CDP can't be enabled retroactively)
    → relaunch with --remote-debugging-port=N using the app's *native* data
      directory (so the user stays logged in)
    → register a CdpSession in cdp._SESSIONS keyed by `app:slack`
    → return that profile_id

  browser_read(profile_id="app:slack")    ← already works
  browser_click(ref=..., profile_id=...)  ← already works
  browser_type(text=..., profile_id=...)  ← already works

Nothing else needs to change — the existing CDP tool surface is identical
whether the target is Chromium or Slack.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from sunday.devices import cdp


# Curated registry: friendly name → app metadata. Add entries as needed; we
# don't auto-discover because (a) confidently launching arbitrary .app
# bundles with a CDP flag risks breaking unfamiliar apps and (b) we want
# stable, predictable names for the agent to call.
APP_REGISTRY: dict[str, dict[str, Any]] = {
    "slack":     {"label": "Slack",       "macos_bin": "/Applications/Slack.app/Contents/MacOS/Slack",            "bundle_id": "com.tinyspeck.slackmacgap"},
    "discord":   {"label": "Discord",     "macos_bin": "/Applications/Discord.app/Contents/MacOS/Discord",        "bundle_id": "com.hnc.Discord"},
    "vscode":    {"label": "VS Code",     "macos_bin": "/Applications/Visual Studio Code.app/Contents/MacOS/Electron", "bundle_id": "com.microsoft.VSCode"},
    "cursor":    {"label": "Cursor",      "macos_bin": "/Applications/Cursor.app/Contents/MacOS/Cursor",          "bundle_id": "com.todesktop.230313mzl4w4u92"},
    "notion":    {"label": "Notion",      "macos_bin": "/Applications/Notion.app/Contents/MacOS/Notion",          "bundle_id": "notion.id"},
    "linear":    {"label": "Linear",      "macos_bin": "/Applications/Linear.app/Contents/MacOS/Linear",          "bundle_id": "com.linear"},
    "spotify":   {"label": "Spotify",     "macos_bin": "/Applications/Spotify.app/Contents/MacOS/Spotify",        "bundle_id": "com.spotify.client"},
    "figma":     {"label": "Figma",       "macos_bin": "/Applications/Figma.app/Contents/MacOS/Figma",            "bundle_id": "com.figma.Desktop"},
    "obsidian":  {"label": "Obsidian",    "macos_bin": "/Applications/Obsidian.app/Contents/MacOS/Obsidian",      "bundle_id": "md.obsidian"},
    "claude":    {"label": "Claude",      "macos_bin": "/Applications/Claude.app/Contents/MacOS/Claude",          "bundle_id": "com.anthropic.claudefordesktop"},
}

# Ports above 19222 (Sunday's browser CDP default) so we don't collide.
_BASE_PORT = 21000


def list_known() -> list[dict[str, Any]]:
    """What's in the registry + which entries are actually installed."""
    out = []
    for key, meta in APP_REGISTRY.items():
        out.append({
            "name":      key,
            "label":     meta["label"],
            "installed": Path(meta["macos_bin"]).exists(),
        })
    return out


def _running_pids(bundle_id: str, app_name: str) -> list[int]:
    """Return PIDs of running processes for this app. We pgrep two ways
    because some Electron apps' main process name is the binary name and
    some carry the bundle id in the command line."""
    pids: set[int] = set()
    for query in (bundle_id, app_name):
        if not query:
            continue
        try:
            r = subprocess.run(
                ["pgrep", "-f", query],
                capture_output=True, text=True, timeout=3,
            )
            for line in r.stdout.split():
                try:
                    pids.add(int(line))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
    return sorted(pids)


async def _quit_if_running(meta: dict, label: str) -> bool:
    """Best-effort quit of an already-running app. Tries graceful AppleScript
    first (so unsaved drafts get a chance), falls back to SIGTERM, then
    SIGKILL after a short wait."""
    bundle_id = meta.get("bundle_id", "")
    app_name = label.replace(" ", "")  # rough — "VS Code" → "VSCode"
    if not _running_pids(bundle_id, app_name):
        return False
    # Graceful path: osascript tell application "<label>" to quit
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{label}" to quit'],
            capture_output=True, timeout=4,
        )
    except Exception:  # noqa: BLE001
        pass
    # Wait for it to actually exit
    for _ in range(20):  # up to 5s
        if not _running_pids(bundle_id, app_name):
            return True
        await asyncio.sleep(0.25)
    # Hard kill if it's still around
    for pid in _running_pids(bundle_id, app_name):
        try:
            subprocess.run(["kill", "-15", str(pid)], capture_output=True, timeout=2)
        except Exception:  # noqa: BLE001
            pass
    for _ in range(8):  # up to 2s
        if not _running_pids(bundle_id, app_name):
            return True
        await asyncio.sleep(0.25)
    return False


async def launch_app(name: str, port: int | None = None) -> dict[str, Any]:
    """Launch an Electron app with --remote-debugging-port, killing any
    existing instance first so the flag actually applies. Registers the
    resulting CDP session in `cdp._SESSIONS` so the existing browser_*
    tools work against it transparently.

    Returns: {profile_id, port, label, ws_url, reused?}.
    """
    name = (name or "").strip().lower()
    meta = APP_REGISTRY.get(name)
    if not meta:
        return {"error": f"don't know how to launch '{name}'. Known: {', '.join(APP_REGISTRY)}."}
    bin_path = meta["macos_bin"]
    if not Path(bin_path).exists():
        return {"error": f"{meta['label']} isn't installed at {bin_path}"}

    profile_id = f"app:{name}"

    # Idempotent — if Sunday already has a session for this app, reuse it.
    existing = cdp._SESSIONS.get(profile_id)
    if existing:
        # Verify the port is still up
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(f"http://127.0.0.1:{existing.port}/json/version")
                if r.status_code == 200:
                    return {
                        "profile_id": profile_id,
                        "port": existing.port,
                        "label": meta["label"],
                        "reused": True,
                    }
        except Exception:  # noqa: BLE001
            pass
        # Stale session — drop it and relaunch
        cdp._SESSIONS.pop(profile_id, None)

    port = port or (_BASE_PORT + (abs(hash(name)) % 1000))

    quit_needed = await _quit_if_running(meta, meta["label"])

    # Launch the app's main binary with the debug flag. NOT passing
    # --user-data-dir on purpose: we want the app's *native* profile so the
    # user's existing login, workspaces, settings all come along.
    proc = await asyncio.create_subprocess_exec(
        bin_path,
        f"--remote-debugging-port={port}",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Wait for the debug port to come up (Slack takes a few seconds to load).
    deadline = time.monotonic() + 25
    ws_url = ""
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                r = await client.get(f"http://127.0.0.1:{port}/json/version")
                if r.status_code == 200:
                    ws_url = r.json().get("webSocketDebuggerUrl", "")
                    break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.4)
    else:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"{meta['label']} launched but CDP port {port} never came up"}

    # Stash a session so all the cdp.* / browser_* tools can find it by profile_id.
    cdp._SESSIONS[profile_id] = cdp.CdpSession(
        profile_id=profile_id,
        port=port,
        process=proc,
        user_data_dir=Path.home(),  # placeholder — we don't manage the data dir for apps
    )
    return {
        "profile_id": profile_id,
        "port": port,
        "label": meta["label"],
        "ws_url": ws_url,
        "relaunched": quit_needed,
    }


async def close_app(name: str) -> dict[str, Any]:
    """Quit an Electron app Sunday launched. Drops the CdpSession too."""
    name = (name or "").strip().lower()
    meta = APP_REGISTRY.get(name)
    if not meta:
        return {"error": f"unknown app '{name}'"}
    profile_id = f"app:{name}"
    session = cdp._SESSIONS.pop(profile_id, None)
    if session and session.process:
        try:
            session.process.terminate()
        except Exception:  # noqa: BLE001
            pass
    # Make sure the actual app process is gone too.
    await _quit_if_running(meta, meta["label"])
    return {"ok": True, "name": name, "label": meta["label"]}
