"""Hermes sub-agent — delegate a scoped task.

Sunday's brain decides what to do; sometimes the right move is to hand a
narrow, well-scoped task off to a fresh Hermes call so the main context
isn't bloated with sub-task chatter. Use for: focused research, scratch
analysis, anything that should produce one self-contained answer.

The sub-agent does NOT have access to Sunday's tools — it's a pure
text-in/text-out call. If a task needs tools, do it in the main loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.runtime import hermes_binary_path
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.subagent.hermes")

DELEGATE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Self-contained task description. Include all context the sub-agent needs.",
        },
        "context": {
            "type": "string",
            "description": "Optional extra context (transcript snippets, facts, links).",
        },
    },
    "required": ["task"],
}


async def _delegate(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task = (args.get("task") or "").strip()
    if not task:
        return {"error": "'task' is required"}
    extra = (args.get("context") or "").strip()

    binary = hermes_binary_path(ctx.config)
    if not binary:
        return {"error": "hermes binary not found on PATH or in ~/.hermes/bin/"}

    prompt = task if not extra else f"{task}\n\nContext:\n{extra}"

    cmd = [
        binary,
        "chat",
        "-Q",
        "--provider", ctx.config.hermes.provider,
        "-m", ctx.config.hermes.model,
        "--max-turns", "1",
        "--source", "sunday-subagent",
        "-t", "",
        "-q", prompt,
    ]

    log.info("hermes subagent invoked", task_chars=len(task))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return {"error": f"hermes exited {proc.returncode}: {stderr or stdout}"}

    return {"answer": stdout}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    # We register the tool even if Hermes isn't installed; the tool itself
    # returns a helpful error when called. That way `sunday tools` always
    # shows the full surface and the user knows what to install.
    registry.register(
        Tool(
            name="delegate_to_hermes",
            description=(
                "Hand a scoped, self-contained task to a fresh Hermes sub-agent. "
                "Use for focused research or analysis that shouldn't bloat the main "
                "conversation. The sub-agent has no tools; provide all context inline."
            ),
            parameters=DELEGATE_PARAMETERS,
            run=_delegate,
        )
    )
