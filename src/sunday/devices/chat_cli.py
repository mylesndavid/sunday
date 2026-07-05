"""Drive a locally-installed `codex` or `claude` CLI as a vision model.

This is how Timeline gets Dayflow-grade play-by-play summaries "practically for
free": instead of paying a per-token vision API, we shell out to the coding-
agent CLI the user is already logged into (their ChatGPT/Codex or Claude
subscription) and hand it the screenshots. The images are passed as **local file
paths** — `codex --image /path`, or listed in the prompt for `claude`, which
reads them off disk. Nothing leaves the Mac except through the user's own CLI.

Mirrors Dayflow's ChatCLIProcessRunner: run the tool through the user's *login*
shell (`-l -i -c`) so it inherits the PATH and auth the user set up in Terminal,
with a generous timeout. We only need the simple non-streaming form.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

import structlog

log = structlog.get_logger("sunday.devices.chat_cli")

TIMEOUT_SECONDS = 240.0
_detected: dict[str, Any] = {}   # tiny cache: {"tool": "codex"|"claude"|None}


def _login_shell() -> str:
    return os.environ.get("SHELL") or "/bin/zsh"


async def _shell(cmd: str, timeout: float) -> tuple[int, str, str]:
    """Run one command string through the user's login shell."""
    proc = await asyncio.create_subprocess_exec(
        _login_shell(), "-l", "-i", "-c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def detect(prefer: str | None = None, refresh: bool = False) -> str | None:
    """Which CLI is usable, if any — `codex`, `claude`, or None. Cached. Checks
    with `command -v` through the login shell so it sees the same PATH the user
    installed the tool on. `prefer` is tried first."""
    if not refresh and "tool" in _detected:
        return _detected["tool"]
    order = [prefer] if prefer in ("codex", "claude") else []
    order += [t for t in ("codex", "claude") if t not in order]
    found: str | None = None
    for tool in order:
        try:
            rc, out, _ = await _shell(f"command -v {tool}", timeout=15)
            if rc == 0 and out.strip():
                found = tool
                break
        except Exception:  # noqa: BLE001
            continue
    _detected["tool"] = found
    if found:
        log.info("chat cli detected", tool=found)
    return found


def _parse_codex_stdout(raw: str) -> str:
    """`codex exec` prints headers, then the assistant reply after a lone
    `codex` line, then a `tokens used` footer. Slice out the reply."""
    text = raw.strip()
    marker = "\ncodex\n"
    idx = text.rfind(marker)
    if idx >= 0:
        text = text[idx + len(marker):]
    cut = text.lower().find("\ntokens used")
    if cut >= 0:
        text = text[:cut]
    return text.strip()


async def run(
    prompt: str,
    image_paths: list[str] | None = None,
    tool: str | None = None,
    model: str | None = None,
    effort: str | None = "low",
    workdir: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One-shot completion with optional screenshots. Returns
    {"ok": bool, "text": str, "tool": str, "error": str|None}."""
    tool = tool or await detect()
    if not tool:
        return {"ok": False, "text": "", "tool": None,
                "error": "no codex/claude CLI logged in on this Mac"}
    image_paths = image_paths or []
    cd = f"cd {shlex.quote(workdir)} && " if workdir else ""

    if tool == "codex":
        parts = ["codex", "exec", "--skip-git-repo-check",
                 "-c", "rmcp_client=false", "-c", "web_search=disabled"]
        if model:
            parts += ["-m", model]
        if effort:
            parts += ["-c", f"model_reasoning_effort={effort}"]
        for p in image_paths:
            parts += ["--image", shlex.quote(p)]
        parts += ["--", shlex.quote(prompt)]
    else:  # claude — it reads the listed local paths itself
        hinted = prompt + ("\nImages:\n" + "\n".join(f"- {p}" for p in image_paths)
                           if image_paths else "")
        parts = ["claude", "-p"]
        if model:
            parts += ["--model", model]
        parts += ["--dangerously-skip-permissions", "--strict-mcp-config",
                  "--", shlex.quote(hinted)]

    cmd = cd + "exec " + " ".join(parts)
    try:
        rc, out, err = await _shell(cmd, timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "text": "", "tool": tool,
                "error": f"{tool} CLI timed out after {int(timeout)}s"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "tool": tool, "error": str(exc)}

    text = _parse_codex_stdout(out) if tool == "codex" else out.strip()
    if rc != 0 and not text:
        if "command not found" in err.lower():
            _detected["tool"] = None   # invalidate cache; it vanished
        return {"ok": False, "text": "", "tool": tool,
                "error": (err.strip() or f"{tool} exited {rc}")[:400]}
    return {"ok": True, "text": text, "tool": tool, "error": None}
