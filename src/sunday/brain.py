"""Sunday's brain — chat-completion + tool-call loop.

One model call per turn becomes one or more model calls when tools are
involved. The harness owns the loop: every model response is parsed, tool
calls are executed locally, results are fed back in, the model decides
whether to call more tools or to finalize a reply.

The identity prompt is the system message; the chat log is the user/
assistant transcript. Modality is recorded but does not influence the
prompt — there is one Sunday.
"""

from __future__ import annotations

import json

import structlog
from openai import AsyncOpenAI

from sunday.chat import Chat
from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.prompt import stable_prefix
from sunday.tools import ToolContext, ToolRegistry

CONTEXT_WINDOW = 40  # most recent messages sent to the model
MAX_TOOL_ITERATIONS = 8

log = structlog.get_logger("sunday.brain")


def _client(config: SundayConfig) -> AsyncOpenAI:
    """OpenAI-compatible client for the configured provider."""
    if config.model.provider == "deepseek":
        key = get_credential("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. "
                "Run: sunday credential set DEEPSEEK_API_KEY <key>"
            )
        return AsyncOpenAI(api_key=key, base_url=config.model.base_url)
    if config.model.provider == "openai":
        key = get_credential("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        return AsyncOpenAI(api_key=key)
    raise RuntimeError(f"provider not wired yet: {config.model.provider}")


def _build_messages(chat: Chat) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": stable_prefix()}]
    msgs.extend(m.to_llm() for m in chat.recent(limit=CONTEXT_WINDOW))
    return msgs


async def respond(
    chat: Chat,
    user_text: str,
    modality: str,
    config: SundayConfig,
    registry: ToolRegistry | None = None,
) -> str:
    """Take a user message, drive the tool-call loop, return the final reply."""
    chat.append("user", user_text, modality)

    client = _client(config)
    ctx = ToolContext(chat=chat, config=config, modality=modality)
    tool_schema = registry.as_openai_schema() if (registry and registry.list_tools()) else None

    for iteration in range(MAX_TOOL_ITERATIONS):
        kwargs: dict = {
            "model": config.model.name,
            "messages": _build_messages(chat),
        }
        if tool_schema:
            kwargs["tools"] = tool_schema
            kwargs["tool_choice"] = "auto"

        completion = await client.chat.completions.create(**kwargs)
        msg = completion.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None) or []

        if tool_calls:
            chat.append(
                "sunday",
                msg.content or "",
                modality,
                metadata={
                    "model": config.model.name,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                        for tc in tool_calls
                    ],
                },
            )

            assert registry is not None
            for tc in tool_calls:
                result = await registry.execute(tc.function.name, tc.function.arguments, ctx)
                content = result if isinstance(result, str) else json.dumps(result, default=str)
                chat.append(
                    "tool",
                    content,
                    modality,
                    metadata={"tool_call_id": tc.id, "tool_name": tc.function.name},
                )
            continue

        reply = (msg.content or "").strip()
        chat.append("sunday", reply, modality, metadata={"model": config.model.name})
        return reply

    truncated = "I hit my tool-call ceiling. Let me know if you want me to keep going."
    chat.append("sunday", truncated, modality, metadata={"truncated": True})
    log.warning("tool loop ceiling reached")
    return truncated
