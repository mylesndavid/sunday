"""Native sub-agent — scoped LLM call without the main chat context.

Sunday's main brain carries a long chat history. For self-contained sub-
tasks (deep research, a focused analysis, drafting a one-shot piece of
text) the context dilutes attention. `delegate` spawns a fresh LLM call
with only the task + optional skill loaded — same model, no chat baggage,
returns one self-contained answer.

This is the framework-free version of what `delegate_to_hermes` does when
hermes is installed. Works anywhere Sunday runs.
"""

from __future__ import annotations

from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.skills import load_skill
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.subagent.native")

_DELEGATE_PARAMS = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": "Self-contained task. Include all the context the sub-agent needs — it can't see the main chat.",
        },
        "context": {
            "type": "string",
            "description": "Optional extra context (transcript snippets, links, facts).",
        },
        "skill": {
            "type": "string",
            "description": "Optional skill slug to load as additional system context (see list_skills).",
        },
    },
    "required": ["task"],
}


async def _t_delegate(args: dict[str, Any], ctx: ToolContext) -> Any:
    task = (args.get("task") or "").strip()
    if not task:
        return {"error": "'task' is required"}
    extra = (args.get("context") or "").strip()
    skill_slug = (args.get("skill") or "").strip()

    from sunday.runtime_openai import OpenAIRuntime

    system = (
        "You are a focused sub-agent dispatched by Sunday for a single, "
        "self-contained task. No tools, no memory, no chat history — just "
        "the task and any provided context. Be direct, complete, and brief."
    )

    if skill_slug:
        skill = load_skill(skill_slug)
        if skill is None:
            return {"error": f"no such skill: {skill_slug}"}
        system += f"\n\n# Skill loaded: {skill.name}\n\n{skill.body()}"

    user_block = task if not extra else f"{task}\n\n# Extra context\n\n{extra}"

    try:
        rt = OpenAIRuntime(ctx.config)
        result = await rt.complete(
            system_prompt=system,
            messages=[{"role": "user", "content": user_block}],
            tools_schema=None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("sub-agent failed", error=str(exc))
        return {"error": f"sub-agent failed: {type(exc).__name__}: {exc}"}

    answer = (result.content or "").strip()
    log.info("sub-agent answered", task_chars=len(task), answer_chars=len(answer), skill=skill_slug or None)
    return {"answer": answer}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="delegate",
        description=(
            "Hand off a scoped, self-contained task to a fresh sub-agent that "
            "has NO access to the main chat history, memory, or tools — just "
            "the task you give it. Optionally load a skill (see list_skills) "
            "to give the sub-agent extra procedure context. Returns one "
            "self-contained answer. Use when you want focused work that "
            "shouldn't dilute the main conversation."
        ),
        parameters=_DELEGATE_PARAMS,
        run=_t_delegate,
    ))
