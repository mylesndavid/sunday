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
from sunday.prompt import stable_prefix
from sunday.runtime import Runtime, build_runtime
from sunday.tools import ToolContext, ToolRegistry

CONTEXT_WINDOW = 40
MAX_TOOL_ITERATIONS = 8

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
    stream_id = uuid.uuid4().hex[:12]

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

    for iteration in range(MAX_TOOL_ITERATIONS):
        result = await rt.complete(
            system_prompt=stable_prefix(),
            messages=_context_messages(chat),
            tools_schema=tool_schema,
            on_delta=_emit_delta,
        )

        if result.tool_calls:
            chat.append(
                "sunday",
                result.content,
                modality,
                metadata={
                    "runtime": rt.name,
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in result.tool_calls
                    ],
                },
            )

            assert registry is not None
            for tc in result.tool_calls:
                tool_result = await registry.execute(tc.name, tc.arguments, ctx)
                content = (
                    tool_result
                    if isinstance(tool_result, str)
                    else json.dumps(tool_result, default=str)
                )
                chat.append(
                    "tool",
                    content,
                    modality,
                    metadata={"tool_call_id": tc.id, "tool_name": tc.name},
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
    chat.append("sunday", truncated, modality, metadata={"truncated": True})
    log.warning("tool loop ceiling reached")
    if broadcast is not None:
        await broadcast({
            "type": "stream_end",
            "stream_id": stream_id,
            "modality": modality,
            "content_full": truncated,
        })
    return truncated
