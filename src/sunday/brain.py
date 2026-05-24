"""Sunday's brain — one LLM call per turn.

Reads recent chat, prepends the identity prompt, calls the model, appends
the reply to the chat. Modality (cli / voice / electron / imessage) is
recorded but does not change the prompt — there is one Sunday, not one
per channel.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from sunday.chat import Chat
from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.prompt import stable_prefix

CONTEXT_WINDOW = 40  # most recent messages sent to the model


def _client(config: SundayConfig) -> AsyncOpenAI:
    """Build an OpenAI-compatible client for the configured provider."""
    if config.model.provider == "deepseek":
        key = get_credential("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set. Run: sunday credential set DEEPSEEK_API_KEY <key>")
        return AsyncOpenAI(api_key=key, base_url=config.model.base_url)
    if config.model.provider == "openai":
        key = get_credential("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        return AsyncOpenAI(api_key=key)
    raise RuntimeError(f"provider not wired yet: {config.model.provider}")


async def respond(chat: Chat, user_text: str, modality: str, config: SundayConfig) -> str:
    """Take a user message, get Sunday's reply, persist both."""
    chat.append("user", user_text, modality)

    recent = chat.recent(limit=CONTEXT_WINDOW)
    messages: list[dict[str, str]] = [{"role": "system", "content": stable_prefix()}]
    messages.extend(m.to_llm() for m in recent)

    client = _client(config)
    completion = await client.chat.completions.create(
        model=config.model.name,
        messages=messages,
    )
    reply = (completion.choices[0].message.content or "").strip()

    chat.append("sunday", reply, modality, metadata={"model": config.model.name})
    return reply
