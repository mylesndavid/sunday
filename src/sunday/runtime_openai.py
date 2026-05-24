"""OpenAI-compatible direct runtime with streaming.

Talks to OpenRouter by default (default base URL), or OpenAI / DeepSeek
direct if explicitly configured. Tokens stream back through an optional
`on_delta` callback while we accumulate the full message + any tool calls
for the harness's tool-call loop.

When tools are involved, tool_call deltas arrive interleaved with content
deltas — same pattern dcharness's `complete_with_tools` uses. We assemble
each tool call by index and only finalize it at end-of-stream.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.runtime import CompletionResult, DeltaHandler, Runtime, ToolCall


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
        on_delta: DeltaHandler | None = None,
    ) -> CompletionResult:
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self.config.model.name,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "stream": True,
        }
        if tools_schema:
            kwargs["tools"] = tools_schema
            kwargs["tool_choice"] = "auto"

        stream = await client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        # Tool calls arrive as deltas indexed by position; assemble by index.
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            delta = getattr(choice, "delta", None)
            if delta is None:
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                continue

            if getattr(delta, "content", None):
                piece = delta.content
                content_parts.append(piece)
                if on_delta is not None:
                    await on_delta(piece)

            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tool_calls_acc.setdefault(idx, {
                    "id": "",
                    "function": {"name": "", "arguments": ""},
                })
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["function"]["name"] = (slot["function"]["name"] or "") + fn.name
                    if getattr(fn, "arguments", None) is not None:
                        slot["function"]["arguments"] = (slot["function"]["arguments"] or "") + fn.arguments

            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        tool_calls = [
            ToolCall(
                id=tool_calls_acc[i]["id"],
                name=tool_calls_acc[i]["function"]["name"],
                arguments=tool_calls_acc[i]["function"]["arguments"],
            )
            for i in sorted(tool_calls_acc.keys())
        ]

        return CompletionResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw={"runtime": "openai", "model": self.config.model.name, "streamed": True},
        )
