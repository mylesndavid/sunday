"""Native macOS UI control — Sunday's hands on any app.

Wraps the ax-macos Swift helper (built once, cached at ~/.sunday/bin):
read the frontmost app's UI as a labeled tree with on-screen positions,
then click/type/key like a human. Requires the Accessibility permission
(granted to Sunday); surfaces a clear AX_NOT_TRUSTED error otherwise.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger("sunday.devices.control")

_BIN = Path("~/.sunday/bin/ax-macos").expanduser()


def is_available() -> bool:
    """macOS + we can build/find the helper. (Actual Accessibility grant is
    checked at call time — like Screen Recording for rewind.)"""
    return sys.platform == "darwin" and Path("/usr/bin/swiftc").exists()


def _binary() -> Path:
    if _BIN.exists():
        return _BIN
    candidates = [
        Path(__file__).resolve().parents[3] / "bin" / "ax-macos.swift",
        Path("/opt/sunday/bin/ax-macos.swift"),
    ]
    src = next((c for c in candidates if c.exists()), None)
    if src is None:
        raise FileNotFoundError(f"ax-macos.swift not found in {candidates}")
    _BIN.parent.mkdir(parents=True, exist_ok=True)
    log.info("building ax-macos helper", src=str(src), target=str(_BIN))
    res = subprocess.run(["/usr/bin/swiftc", "-O", "-o", str(_BIN), str(src)],
                         capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        raise RuntimeError(f"swiftc failed: {res.stderr.strip() or res.stdout.strip()}")
    return _BIN


async def _run(*args: str, timeout: float = 20) -> dict[str, Any]:
    binp = _binary()
    proc = await asyncio.create_subprocess_exec(
        str(binp), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    text = out.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError:
        data = {"raw": text}
    if isinstance(data, dict) and data.get("error") == "AX_NOT_TRUSTED":
        return {"error": (
            "Accessibility permission not granted to Sunday. Open System "
            "Settings → Privacy & Security → Accessibility, enable Sunday, "
            "then try again."
        )}
    if proc.returncode != 0 and "error" not in data:
        return {"error": err.decode(errors="replace").strip() or "ax-macos failed"}
    return data


async def snapshot() -> dict[str, Any]:
    return await _run("snapshot")


async def click(x: float, y: float) -> dict[str, Any]:
    return await _run("click", str(int(x)), str(int(y)))


async def type_text(text: str) -> dict[str, Any]:
    return await _run("type", text)


async def key(combo: str) -> dict[str, Any]:
    return await _run("key", combo)
