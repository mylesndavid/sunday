"""Sunday's brain — chat-completion + tool-call loop.

The harness owns the loop. The Runtime owns the LLM call. They cooperate:
the harness keeps appending messages to the chat, calls Runtime.complete,
dispatches any tool calls, and repeats until the model returns a plain reply.

The runtime can be Hermes (subprocess, dcharness-style) or direct
OpenAI-compatible (DeepSeek by default). Same loop either way.
"""

from __future__ import annotations

import json
import uuid

import structlog

from sunday.chat import Chat
from sunday.config import SundayConfig
from sunday.memory import recall_block as memory_recall_block
from sunday.prompt import stable_prefix
from sunday.runtime import IterationBudget, Runtime, build_runtime, repair_tool_call_arguments
from sunday.tools import ToolContext, ToolRegistry

CONTEXT_WINDOW = 40
# Per-turn iteration budget. Same shape as Hermes's --max-iterations.
# 30 is generous for normal use; a runaway tool loop would still bail.
MAX_TOOL_ITERATIONS = 30

log = structlog.get_logger("sunday.brain")


def _context_messages(chat: Chat) -> list[dict]:
    return [m.to_llm() for m in chat.recent(limit=CONTEXT_WINDOW)]


async def respond(
    chat: Chat,
    user_text: str,
    modality: str,
    config: SundayConfig,
    registry: ToolRegistry | None = None,
    runtime: Runtime | None = None,
    attachments: list[dict] | None = None,
    extras: dict | None = None,
) -> str:
    """Take a user message, drive the tool-call loop, return the final reply.

    `attachments` is a list of Attachment-shaped dicts (see sunday.attachments)
    that get stored on the user message metadata and forwarded to the model
    via Message.to_llm()'s multipart handling.

    `extras` is forwarded to ToolContext — the daemon uses this to hand tools
    a broadcast callback (live-view, Electron events, etc.).
    """
    user_meta: dict = {}
    if attachments:
        user_meta["attachments"] = attachments
    chat.append("user", user_text, modality, metadata=user_meta or None)

    rt = runtime or build_runtime(config)
    ctx = ToolContext(chat=chat, config=config, modality=modality, extras=extras or {})
    tool_schema = registry.as_openai_schema() if (registry and registry.list_tools()) else None

    broadcast = (extras or {}).get("broadcast")
    memory    = (extras or {}).get("memory")
    stream_id = uuid.uuid4().hex[:12]

    # Pull relevant memories ONCE per turn (not per tool iteration) and bake
    # them into the system prompt. The model can also call the `recall` tool
    # for deeper searches if it needs them.
    memory_block = ""
    if memory is not None and getattr(memory, "available", False):
        try:
            recalled = await memory.recall(user_text, top_k=6)
            memory_block = memory_recall_block(recalled)
            log.info(
                "memory recall",
                hits=len(recalled),
                top_distance=(f"{recalled[0].distance:.3f}" if recalled else None),
                stored_total=memory.count(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("memory recall failed", error=str(exc))

    def _system_prompt() -> str:
        return stable_prefix() + memory_block

    async def _emit_delta(piece: str) -> None:
        if broadcast is not None:
            await broadcast({
                "type": "stream_delta",
                "stream_id": stream_id,
                "modality": modality,
                "content": piece,
            })

    if broadcast is not None:
        await broadcast({
            "type": "stream_start",
            "stream_id": stream_id,
            "modality": modality,
        })

    budget = IterationBudget(MAX_TOOL_ITERATIONS)
    while budget.consume():
        result = await rt.complete(
            system_prompt=_system_prompt(),
            messages=_context_messages(chat),
            tools_schema=tool_schema,
            on_delta=_emit_delta,
        )

        if result.tool_calls:
            # Repair any malformed JSON argument blobs before persisting +
            # dispatching — covers models that emit trailing commas,
            # unclosed structures, unescaped control chars, etc.
            sanitized = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": repair_tool_call_arguments(tc.arguments, tc.name),
                }
                for tc in result.tool_calls
            ]
            chat.append(
                "sunday",
                result.content,
                modality,
                metadata={
                    "runtime": rt.name,
                    "tool_calls": sanitized,
                },
            )

            assert registry is not None
            for tc in sanitized:
                tool_result = await registry.execute(tc["name"], tc["arguments"], ctx)
                content = (
                    tool_result
                    if isinstance(tool_result, str)
                    else json.dumps(tool_result, default=str)
                )
                chat.append(
                    "tool",
                    content,
                    modality,
                    metadata={"tool_call_id": tc["id"], "tool_name": tc["name"]},
                )
            continue

        reply = result.content.strip()
        chat.append("sunday", reply, modality, metadata={"runtime": rt.name})
        if broadcast is not None:
            await broadcast({
                "type": "stream_end",
                "stream_id": stream_id,
                "modality": modality,
                "content_full": reply,
            })
        return reply

    truncated = "I hit my tool-call ceiling. Let me know if you want me to keep going."
    chat.append("sunday", truncated, modality, metadata={"truncated": True, "budget_used": budget.used})
    log.warning("tool loop ceiling reached", budget_used=budget.used, budget_max=budget.max_total)
    if broadcast is not None:
        await broadcast({
            "type": "stream_end",
            "stream_id": stream_id,
            "modality": modality,
            "content_full": truncated,
        })
    return truncated
