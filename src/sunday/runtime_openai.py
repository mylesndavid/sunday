"""OpenAI-compatible direct runtime.

Talks to DeepSeek (default), OpenAI, or any other vendor exposing the
chat-completions + tool-calling spec. Native tool calling — the model
emits a tool_calls field and our harness loop dispatches.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.runtime import CompletionResult, Runtime, ToolCall


class OpenAIRuntime:
    name = "openai"

    def __init__(self, config: SundayConfig) -> None:
        self.config = config

    def _client(self) -> AsyncOpenAI:
        provider = self.config.model.provider

        if provider == "openrouter":
            key = get_credential("OPENROUTER_API_KEY")
            if not key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY is not set. "
                    "Run: sunday credential set OPENROUTER_API_KEY <key>"
                )
            return AsyncOpenAI(
                api_key=key,
                base_url=self.config.model.base_url,
                # OpenRouter uses these for attribution + per-app analytics.
                default_headers={
                    "HTTP-Referer": "https://sunday.local",
                    "X-Title": "Sunday",
                },
            )

        if provider == "openai":
            key = get_credential("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            return AsyncOpenAI(api_key=key)

        if provider == "deepseek-direct":
            # Only use this when you specifically want DeepSeek's own API.
            # The default 'openrouter' provider also lets you call DeepSeek
            # models — go through it unless you have a reason not to.
            key = get_credential("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError("DEEPSEEK_API_KEY is not set.")
            return AsyncOpenAI(api_key=key, base_url=self.config.model.base_url)

        raise RuntimeError(f"OpenAIRuntime does not handle provider: {provider}")

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tools_schema: list[dict[str, Any]] | None,
    ) -> CompletionResult:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self.config.model.name,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
        }
        if tools_schema:
            kwargs["tools"] = tools_schema
            kwargs["tool_choice"] = "auto"

        completion = await client.chat.completions.create(**kwargs)
        msg = completion.choices[0].message
        tool_calls_raw = getattr(msg, "tool_calls", None) or []

        return CompletionResult(
            content=msg.content or "",
            tool_calls=[
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                for tc in tool_calls_raw
            ],
            finish_reason=completion.choices[0].finish_reason,
            raw={"model": self.config.model.name},
        )
