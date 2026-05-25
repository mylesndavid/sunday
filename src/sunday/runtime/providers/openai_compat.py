"""OpenAI-compatible streaming provider.

Covers OpenRouter (default), OpenAI direct, DeepSeek direct, and anything
else exposing the OpenAI Chat Completions wire format. Streams token-by-
token, assembles tool_call deltas by index, normalises to CompletionResult.

Performance choices:
- Lazy import of the openai SDK (defers ~800ms of pydantic-backed type
  initialization until the first actual LLM call — matches Hermes's
  `_load_openai_cls` trick in run_agent.py:77).
- The AsyncOpenAI client is built once per provider instance and reused
  across calls. httpx underneath does HTTP/1.1 keepalive, so the TLS
  handshake is paid once instead of per-turn.
"""

from __future__ import annotations

from typing import Any

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.runtime.types import CompletionResult, DeltaHandler, ToolCall


def _load_async_openai():
    """Lazy import — saves ~800ms of pydantic load on `sunday start` and
    on `sunday init`. Only hits when someone actually wants a completion."""
    from openai import AsyncOpenAI
    return AsyncOpenAI


class OpenAICompatProvider:
    """OpenAI Chat Completions streaming, configurable base_url.

    The underlying AsyncOpenAI client is cached on the instance — same
    client across every call. httpx keeps the TLS connection warm so
    second + subsequent calls skip the ~150ms TLS handshake.
    """

    name = "openai-compat"

    def __init__(self, config: SundayConfig) -> None:
        self.config = config
        self._client_cache: Any = None

    def _client(self):
        if self._client_cache is not None:
            return self._client_cache

        AsyncOpenAI = _load_async_openai()
        provider = self.config.model.provider

        if provider == "openrouter":
            key = get_credential("OPENROUTER_API_KEY")
            if not key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY is not set. "
                    "Run: sunday credential set OPENROUTER_API_KEY <key>"
                )
            self._client_cache = AsyncOpenAI(
                api_key=key,
                base_url=self.config.model.base_url,
                default_headers={
                    "HTTP-Referer": "https://sunday.local",
                    "X-Title": "Sunday",
                },
            )
            return self._client_cache

        if provider == "openai":
            key = get_credential("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("OPENAI_API_KEY is not set.")
            self._client_cache = AsyncOpenAI(api_key=key)
            return self._client_cache

        if provider == "deepseek-direct":
            key = get_credential("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError("DEEPSEEK_API_KEY is not set.")
            self._client_cache = AsyncOpenAI(api_key=key, base_url=self.config.model.base_url)
            return self._client_cache

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
        # Reasoning content (deepseek-reasoner, o-series, kimi, glm) arrives
        # as a parallel `reasoning_content` field on each delta. Accumulated
        # separately so we can preserve it across turns without leaking
        # into the visible reply.
        reasoning_parts: list[str] = []
        # tool_calls arrive as indexed deltas; assemble per index
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
