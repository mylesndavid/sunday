"""OpenAI-compatible streaming provider.

Covers OpenRouter (default), OpenAI direct, DeepSeek direct, and anything
else exposing the OpenAI Chat Completions wire format. Streams token-by-
token, assembles tool_call deltas by index, normalises to CompletionResult.

Hermes-shape: provider is dumb and self-contained. The agent loop in
core.py handles iteration budgeting, tool dispatch, and message history.
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.runtime.types import CompletionResult, DeltaHandler, ToolCall


class OpenAICompatProvider:
    """OpenAI Chat Completions streaming, configurable base_url."""

    name = "openai-compat"

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

        raise RuntimeError(f"OpenAICompatProvider does not handle: {provider}")

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
        # Reasoning content (deepseek-reasoner, o-series, etc.) arrives as
        # a parallel `reasoning_content` field on each delta. We accumulate
        # it separately so we can preserve it across turns.
        reasoning_parts: list[str] = []
        # tool_calls arrive as indexed deltas; accumulate into a dict keyed by index
        tc_acc: dict[int, dict[str, Any]] = {}
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

            # Reasoning content — accumulate but don't stream (most UIs
            # don't render it; let the brain stash it in metadata so it
            # flows back on the next turn).
            r_piece = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if r_piece:
                reasoning_parts.append(str(r_piece))

            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tc_acc.setdefault(idx, {
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
                id=tc_acc[i]["id"],
                name=tc_acc[i]["function"]["name"],
                arguments=tc_acc[i]["function"]["arguments"],
            )
            for i in sorted(tc_acc.keys())
        ]

        raw: dict[str, Any] = {
            "provider": self.name,
            "model": self.config.model.name,
            "streamed": True,
        }
        if reasoning_parts:
            raw["reasoning_content"] = "".join(reasoning_parts)
        return CompletionResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=raw,
        )
