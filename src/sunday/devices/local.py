"""In-process device handlers for the brain's own host.

The brain runs *on* the user's Mac, so shell commands should execute right
here — no satellite WebSocket required. `DeviceManager.register_local()`
installs these as a "local" device so `device_run_command` always has a
target, even when no satellite is connected (the satellite's WS link can
silently drop on a restart and leave the brain unable to run a command on the
very machine it lives on).

Kept deliberately minimal: shell only, the universal escape hatch. Screen /
CDP / control stay satellite-served for now — a satellite advertising those
capabilities still wins for them (capability-filtered routing in
devices/tools.py:_resolve_device). The subprocess logic mirrors the
satellite's `_h_run_command` so behaviour is identical wherever a command runs.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def _run_command(params: dict[str, Any]) -> dict[str, Any]:
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


# Capabilities this in-process device advertises, and the method→handler table
# DeviceManager.command() dispatches to for it.
CAPABILITIES = ["shell"]
HANDLERS = {"run_command": _run_command}
