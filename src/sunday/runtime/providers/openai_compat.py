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
        on_reasoning: DeltaHandler | None = None,
        purpose: str | None = None,
    ) -> CompletionResult:
        import time
        client = self._client()
        kwargs: dict[str, Any] = {
            "model": self.config.model.name,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "stream": True,
            # Ask the provider to emit a final chunk with token usage. Both
            # OpenAI and OpenRouter honour this; providers that don't will
            # ignore the unknown field, in which case we fall back to a
            # char-based token estimate so cost still gets logged.
            "stream_options": {"include_usage": True},
        }
        if tools_schema:
            kwargs["tools"] = tools_schema
            kwargs["tool_choice"] = "auto"

        # OpenRouter: pin latency-sorted provider routing. Default routing sends
        # the request to a rotating set of backends with wildly variable TTFT
        # (measured 0.4–14s); sort=latency holds it to the fastest backend and
        # cuts the tail dramatically (measured a tight 0.56–0.75s). Harmless on
        # non-OpenRouter base URLs (they ignore unknown extra_body keys, but we
        # only attach it for OpenRouter to be safe).
        if "openrouter" in (self.config.model.base_url or ""):
            kwargs["extra_body"] = {"provider": {"sort": "latency"}}

        started = time.monotonic()
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
        # Usage arrives in a terminal chunk when stream_options.include_usage
        # is set. Default to None so we can fall back to estimation below.
        usage_prompt: int | None = None
        usage_completion: int | None = None

        async for chunk in stream:
            # Final-chunk usage (OpenAI + OpenRouter): a chunk with no choices
            # but a populated `usage` object. Capture before the loop body
            # because choices=[] would otherwise skip it.
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                usage_prompt = getattr(usage, "prompt_tokens", None) or usage_prompt
                usage_completion = getattr(usage, "completion_tokens", None) or usage_completion

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
                if on_reasoning is not None:
                    await on_reasoning(str(r_piece))

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

        # --- cost logging ---------------------------------------------------
        # Fall back to a char-based estimate if the provider didn't return
        # usage. Better an approximate cost than no record at all.
        if usage_prompt is None or usage_completion is None:
            prompt_chars = sum(len(m.get("content") or "") for m in kwargs["messages"] if isinstance(m.get("content"), str))
            usage_prompt = usage_prompt or max(1, prompt_chars // 4)
            completion_chars = sum(len(p) for p in content_parts) + sum(len(p) for p in reasoning_parts)
            usage_completion = usage_completion or max(0, completion_chars // 4)
        try:
            from sunday.cost import get_store
            get_store().log_llm(
                purpose=purpose or "unknown",
                provider=self.config.model.provider or "openai-compat",
                model=self.config.model.name,
                prompt_tokens=usage_prompt,
                completion_tokens=usage_completion,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001
            # Cost logging is observability — never let it break a turn.
            pass

        raw: dict[str, Any] = {
            "provider": self.name,
            "model": self.config.model.name,
            "streamed": True,
            "prompt_tokens": usage_prompt,
            "completion_tokens": usage_completion,
        }
        if reasoning_parts:
            raw["reasoning_content"] = "".join(reasoning_parts)
        return CompletionResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=raw,
        )
