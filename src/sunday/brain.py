"""Sunday's brain — chat-completion + tool-call loop.

The harness owns the loop. The Runtime owns the LLM call. They cooperate:
the harness keeps appending messages to the chat, calls Runtime.complete,
dispatches any tool calls, and repeats until the model returns a plain reply.

The runtime can be Hermes (subprocess, dcharness-style) or direct
OpenAI-compatible (DeepSeek by default). Same loop either way.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import structlog

from sunday.chat import Chat
from sunday.config import SundayConfig
from sunday.memory import recall_block as memory_recall_block
from sunday.prompt import stable_prefix
from sunday.runtime import (
    IterationBudget,
    Runtime,
    build_runtime,
    repair_message_sequence,
    repair_tool_call_arguments,
    sanitize_messages_surrogates,
)
from sunday.tools import ToolContext, ToolRegistry

CONTEXT_WINDOW = 40
# Per-turn iteration budget. Same shape as Hermes's --max-iterations.
# 30 is generous for normal use; a runaway tool loop would still bail.
MAX_TOOL_ITERATIONS = 30

# Tools that have no shared mutable state and are safe to run concurrently
# in the same iteration. Port of Hermes's _PARALLEL_SAFE_TOOLS list
# (run_agent.py:340), adapted to Sunday's tool surface.
PARALLEL_SAFE_TOOLS = frozenset({
    # Reads
    "recall", "list_skills", "load_skill",
    "imessage_list_threads", "imessage_read_thread", "imessage_read_recent",
    "browser_markdown", "browser_screenshot", "browser_scrape",
    "device_screenshot",
    # Pure: no side effects on Sunday's state
    "delegate", "delegate_to_hermes",
})

log = structlog.get_logger("sunday.brain")


def _context_messages(chat: Chat, memory_block: str = "") -> list[dict]:
    """Build the messages list for the next provider call, with the same
    defensive belts Hermes runs before every API call: surrogate
    sanitization + role-alternation repair. Both are no-ops on healthy
    histories and silent fixes when something's off.

    `memory_block`, if non-empty, is prepended to the LATEST user message
    (not injected into the system prompt). This keeps the system prefix
    byte-stable across turns so providers' prompt cache fires on every
    turn after the first — 60-90% cheaper + faster TTFT.
    """
    messages = [m.to_llm() for m in chat.recent(limit=CONTEXT_WINDOW)]

    if memory_block and messages:
        # Find the LAST user message and prepend the context block to it.
        # We do this on a copy of the message so chat storage is untouched.
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                base = messages[i].get("content")
                if isinstance(base, str):
                    messages[i] = {**messages[i], "content": memory_block + "\n\n" + base}
                elif isinstance(base, list):
                    # Multimodal — prepend a text block
                    messages[i] = {**messages[i], "content": [{"type": "text", "text": memory_block}, *base]}
                break

    if sanitize_messages_surrogates(messages):
        log.info("sanitized surrogate code points in messages")
    repaired = repair_message_sequence(messages)
    if repaired:
        log.info("repaired role-alternation violations", count=repaired)
    return messages


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

    # Pull relevant memories ONCE per turn (not per tool iteration). The
    # block gets PREPENDED to the user's latest message inside
    # _context_messages — NOT injected into the system prompt — so the
    # system prefix stays byte-stable across turns and providers' prompt
    # caches actually fire.
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
    iteration = 0
    while budget.consume():
        iteration += 1
        # Step callback — observability event before each provider call.
        # Lets the Electron app render "thinking", "calling tool X", etc.
        if broadcast is not None:
            await broadcast({
                "type": "agent_step",
                "stream_id": stream_id,
                "iteration": iteration,
                "budget_used": budget.used,
                "budget_max": budget.max_total,
            })

        result = await rt.complete(
            system_prompt=stable_prefix(),
            messages=_context_messages(chat, memory_block=memory_block),
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
            meta: dict = {"runtime": rt.name, "tool_calls": sanitized}
            # Preserve reasoning content across turns when the model emitted it.
            if result.raw.get("reasoning_content"):
                meta["reasoning_content"] = result.raw["reasoning_content"]
            chat.append(
                "sunday",
                result.content,
                modality,
                metadata=meta,
            )

            assert registry is not None

            async def _execute_one(tc):
                if broadcast is not None:
                    await broadcast({
                        "type": "tool_call",
                        "stream_id": stream_id,
                        "tool_name": tc["name"],
                        "tool_call_id": tc["id"],
                    })
                tool_result = await registry.execute(tc["name"], tc["arguments"], ctx)
                content = (
                    tool_result
                    if isinstance(tool_result, str)
                    else json.dumps(tool_result, default=str)
                )
                if broadcast is not None:
                    await broadcast({
                        "type": "tool_result",
                        "stream_id": stream_id,
                        "tool_call_id": tc["id"],
                        "tool_name": tc["name"],
                    })
                return tc, content

            # Parallel-safe batch: gather. Otherwise: serial.
            # We persist tool results to chat in the same order the model
            # called them, regardless of completion order, so the next
            # iteration sees them in a deterministic position.
            if all(tc["name"] in PARALLEL_SAFE_TOOLS for tc in sanitized) and len(sanitized) > 1:
                log.info("parallel tool batch", count=len(sanitized), names=[tc["name"] for tc in sanitized])
                results = await asyncio.gather(*(_execute_one(tc) for tc in sanitized))
            else:
                results = []
                for tc in sanitized:
                    results.append(await _execute_one(tc))

            for tc, content in results:
                chat.append(
                    "tool",
                    content,
                    modality,
                    metadata={"tool_call_id": tc["id"], "tool_name": tc["name"]},
                )
            continue

        reply = result.content.strip()
        final_meta: dict = {"runtime": rt.name}
        if result.raw.get("reasoning_content"):
            final_meta["reasoning_content"] = result.raw["reasoning_content"]
        chat.append("sunday", reply, modality, metadata=final_meta)
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
