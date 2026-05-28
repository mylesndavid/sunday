"""Native sub-agent — a real worker that runs the full agent loop on a
scoped task, with tools, in its own isolated context.

`delegate` spawns a fresh agent: its own in-memory chat (no main history,
no memory writes), the same toolset minus the dangerous/recursive ones
(it can browse, read, run commands, drive apps — but not spawn more
sub-agents or send messages/place calls on its own), running to completion
and returning one self-contained result. Use it to keep multi-step grunt
work (research a thing, dig through a doc, check N pages) out of the main
conversation — and run several at once for independent tasks.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.skills import load_skill
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.subagent.native")

# Sub-agents run in the background to completion (no user waiting on them), so
# they get a much bigger tool budget than the interactive main agent (60).
# Deep research — browsing many company sites for prospects/emails — was
# hitting the main ceiling and coming back empty.
SUBAGENT_MAX_ITERATIONS = 120

# Live registry of running sub-agents — powers the notch HUD's agent count.
import itertools
_ACTIVE: dict[int, str] = {}
_AGENT_SEQ = itertools.count(1)

# Strong refs to in-flight background delegations so the event loop doesn't
# garbage-collect the tasks mid-run.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


def active_agents() -> list[dict[str, Any]]:
    """Currently-running sub-agents (id + short task label). Read by the
    daemon's /v1/status for the HUD."""
    return [{"id": i, "task": t} for i, t in _ACTIVE.items()]


async def _broadcast_agents(ctx: ToolContext) -> None:
    bc = (ctx.extras or {}).get("broadcast")
    if bc:
        try:
            await bc({"type": "agents", "active": active_agents()})
        except Exception:  # noqa: BLE001
            pass

# Tools a sub-agent must NOT have: spawning more sub-agents (recursion) and
# irreversible comms / memory mutation it shouldn't do unsupervised.
_EXCLUDED = {
    "delegate", "delegate_parallel", "delegate_to_hermes",
    "imessage_send", "sendblue_send", "call_phone",
    "remember", "forget",
}

_WORKER_PROMPT = (
    "You are a sub-agent dispatched by Sunday for one scoped task. You have "
    "tools — use them to actually do the work (read pages in the browser, run "
    "commands, drive apps, search). You do NOT have the main conversation, so "
    "everything you need is in the task. Work until the task is done, then "
    "return a single complete, self-contained result: what you found or did, "
    "with the concrete details (links, values, file paths) the caller needs. "
    "No chit-chat, no questions back — just do it and report. If you genuinely "
    "can't, say exactly what blocked you."
)


def _worker_registry(config: SundayConfig) -> ToolRegistry:
    """A fresh registry with the dangerous/recursive tools stripped."""
    from sunday.tools import default_registry
    reg = default_registry(config)
    for name in _EXCLUDED:
        reg.remove(name)
    return reg


async def _run_one(task: str, extra: str, skill_slug: str, ctx: ToolContext) -> str:
    from sunday.brain import respond
    from sunday.chat import Chat

    system = _WORKER_PROMPT
    if skill_slug:
        skill = load_skill(skill_slug)
        if skill:
            system += f"\n\n# Skill: {skill.name}\n\n{skill.body()}"

    prompt = task if not extra else f"{task}\n\n# Context\n\n{extra}"
    sub_chat = Chat(path=":memory:")           # isolated, ephemeral
    # Pass the parent's tool extras (devices, etc.) so the worker's tools
    # work — but drop broadcast + memory so it runs silently and writes
    # nothing to the user's memory.
    extras = {k: v for k, v in (ctx.extras or {}).items() if k not in ("broadcast", "memory")}
    reg = _worker_registry(ctx.config)
    agent_id = next(_AGENT_SEQ)
    _ACTIVE[agent_id] = (task[:60] + "…") if len(task) > 60 else task
    await _broadcast_agents(ctx)
    try:
        return await respond(
            sub_chat, prompt, "subagent", ctx.config,
            registry=reg, extras=extras, system_prompt=system,
            runtime=(ctx.extras or {}).get("runtime"),  # reuse the warm client
            max_iterations=SUBAGENT_MAX_ITERATIONS,     # background grunt work gets a bigger budget
        )
    finally:
        _ACTIVE.pop(agent_id, None)
        await _broadcast_agents(ctx)


async def _t_delegate(args: dict[str, Any], ctx: ToolContext) -> Any:
    task = (args.get("task") or "").strip()
    if not task:
        return {"error": "'task' is required"}
    context = (args.get("context") or "").strip()
    skill = (args.get("skill") or "").strip()
    inject = (ctx.extras or {}).get("inject_and_wake")

    # No async channel (tests, or a context without the daemon's wake hook) —
    # fall back to running inline and returning the answer directly.
    if inject is None:
        try:
            answer = await _run_one(task, context, skill, ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("sub-agent failed", error=str(exc))
            return {"error": f"sub-agent failed: {type(exc).__name__}: {exc}"}
        return {"answer": answer}

    # Async: run in the background, inject the result as a wake turn when done.
    async def _bg() -> None:
        try:
            answer = await _run_one(task, context, skill, ctx)
            msg = f"[Sub-agent finished — task: {task}]\n\n{answer}"
            log.info("sub-agent done", task_chars=len(task), answer_chars=len(answer or ""))
        except Exception as exc:  # noqa: BLE001
            log.warning("sub-agent failed", error=str(exc))
            msg = f"[Sub-agent failed — task: {task}]\n\n{type(exc).__name__}: {exc}"
        try:
            await inject(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("sub-agent inject failed", error=str(exc))

    _spawn(_bg())
    return {
        "status": "started",
        "note": ("Sub-agent is working in the background. Its result will come "
                 "back to you as a follow-up message — do NOT wait for it. Tell "
                 "the user you're on it, then end your turn."),
    }


async def _t_delegate_parallel(args: dict[str, Any], ctx: ToolContext) -> Any:
    tasks = args.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return {"error": "'tasks' must be a non-empty list of task strings"}
    tasks = [str(t).strip() for t in tasks if str(t).strip()][:6]
    inject = (ctx.extras or {}).get("inject_and_wake")

    if inject is None:  # inline fallback
        results = await asyncio.gather(
            *[_run_one(t, "", "", ctx) for t in tasks], return_exceptions=True
        )
        out = []
        for t, r in zip(tasks, results):
            out.append({"task": t, "error": str(r)} if isinstance(r, Exception) else {"task": t, "answer": r})
        return {"results": out}

    async def _bg() -> None:
        results = await asyncio.gather(
            *[_run_one(t, "", "", ctx) for t in tasks], return_exceptions=True
        )
        blocks = []
        for t, r in zip(tasks, results):
            body = f"[failed] {type(r).__name__}: {r}" if isinstance(r, Exception) else str(r)
            blocks.append(f"## {t}\n{body}")
        msg = "[Sub-agents finished — parallel batch]\n\n" + "\n\n".join(blocks)
        try:
            await inject(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("sub-agent inject failed", error=str(exc))

    _spawn(_bg())
    return {
        "status": "started",
        "count": len(tasks),
        "note": ("Sub-agents are working in the background; their results arrive "
                 "together as a follow-up. Do NOT wait — tell the user you've "
                 "kicked them off, then end your turn."),
    }


_DELEGATE_PARAMS = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Self-contained task. Include everything the worker needs — it can't see the main chat."},
        "context": {"type": "string", "description": "Optional extra context (snippets, links, facts)."},
        "skill": {"type": "string", "description": "Optional skill slug to load as procedure context (see list_skills)."},
    },
    "required": ["task"],
}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="delegate",
        description=(
            "Hand a scoped multi-step task to a worker sub-agent that runs the "
            "full agent loop WITH tools (browse, read, run commands, drive apps) "
            "in its own isolated context — no main chat history, no memory "
            "writes, can't send messages or spawn sub-agents. ASYNC: returns "
            "immediately, and the sub-agent's result comes back to you later as "
            "a follow-up message. So don't wait — when you delegate, tell the "
            "user you've started on it and END YOUR TURN. You'll be re-woken "
            "with the result and can report it then. Use it to keep grunt work "
            "(research, digging through a doc, checking pages) off the main "
            "thread without freezing the conversation."
        ),
        parameters=_DELEGATE_PARAMS,
        run=_t_delegate,
    ))
    registry.register(Tool(
        name="delegate_parallel",
        description=(
            "Run several independent worker sub-agents at once (each with tools, "
            "isolated). Pass a list of self-contained task strings. ASYNC: "
            "returns immediately; all their results arrive together as a "
            "follow-up message later. Don't wait — tell the user you've kicked "
            "them off and end your turn. Use for fan-out work (check five links, "
            "research three options) that doesn't need to be sequential."
        ),
        parameters={"type": "object", "properties": {
            "tasks": {"type": "array", "items": {"type": "string"}, "description": "Up to 6 self-contained tasks to run concurrently."},
        }, "required": ["tasks"]},
        run=_t_delegate_parallel,
    ))
