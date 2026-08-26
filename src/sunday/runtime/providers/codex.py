"""Codex provider — use your ChatGPT/Codex subscription (no API key).

Reads the OAuth token the `codex` CLI stores at ~/.codex/auth.json, refreshes
it when expired, and calls the ChatGPT-backed Codex Responses endpoint. Because
most people run Sunday on the same machine they're logged into Codex on, the
daemon just reads that file directly — no key to paste, no proxy.

Validated wire format (empirically):
  POST https://chatgpt.com/backend-api/codex/responses
  headers: Authorization: Bearer <access>, chatgpt-account-id: <account_id>,
           OpenAI-Beta: responses=experimental, originator: codex_cli_rs,
           session_id: <uuid>
  body:    Responses API — {model, instructions, input[], tools[], stream:true}
  models:  ChatGPT accounts accept the chat models (gpt-5.2, gpt-5.5), NOT the
           "-codex" variants. SSE: response.output_text.delta / .completed.
  refresh: POST https://auth.openai.com/oauth/token (JSON) grant_type=refresh_token.
"""

from __future__ import annotations

import base64
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import structlog

from sunday.runtime.types import CompletionResult, DeltaHandler, ToolCall

log = structlog.get_logger("sunday.runtime.codex")

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"   # the codex CLI's OAuth client id
AUTH_PATH = Path("~/.codex/auth.json").expanduser()


def codex_available() -> bool:
    """True when the machine is logged into Codex (so this provider can work)."""
    try:
        return AUTH_PATH.exists() and bool(json.loads(AUTH_PATH.read_text())["tokens"]["refresh_token"])
    except Exception:  # noqa: BLE001
        return False


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp"))
    except Exception:  # noqa: BLE001
        return None


async def _fresh_token(force: bool = False) -> tuple[str, str]:
    """Return (access_token, account_id), refreshing + persisting if expired."""
    if not AUTH_PATH.exists():
        raise RuntimeError(
            "No Codex login on this host (~/.codex/auth.json missing). Run `codex` to "
            "sign in, or point Sunday at a daemon where you're logged into Codex."
        )
    auth = json.loads(AUTH_PATH.read_text())
    tokens = auth.get("tokens") or {}
    if not tokens.get("refresh_token"):
        raise RuntimeError("Codex auth.json has no refresh_token — run `codex` to sign in again.")
    access = tokens.get("access_token", "")
    exp = _jwt_exp(access)
    needs = force or not access or (exp is not None and exp - time.time() < 120)
    if needs:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(TOKEN_URL, json={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
                "client_id": CLIENT_ID,
            })
            r.raise_for_status()
            tr = r.json()
        tokens["access_token"] = tr["access_token"]
        tokens["refresh_token"] = tr.get("refresh_token") or tokens["refresh_token"]
        if tr.get("id_token"):
            tokens["id_token"] = tr["id_token"]
        auth["tokens"] = tokens
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            AUTH_PATH.write_text(json.dumps(auth, indent=2))
        except Exception:  # noqa: BLE001
            pass   # read-only fs is fine; we still have the token in-memory
        access = tokens["access_token"]
        log.info("codex token refreshed")
    return access, tokens["account_id"]


def _instructions(system_prompt: str, messages: list[dict[str, Any]]) -> str:
    """The Responses API carries the system prompt as `instructions`. Fold any
    system-role messages from the conversation in too (Sunday puts its running
    summary there)."""
    parts = [system_prompt] if system_prompt else []
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            parts.append(m["content"])
    return "\n\n".join(p for p in parts if p)


def _to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat-shaped messages -> Responses API `input` items."""
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            continue   # handled in instructions
        if role == "tool":
            out = content if isinstance(content, str) else json.dumps(content, default=str)
            items.append({"type": "function_call_output",
                          "call_id": m.get("tool_call_id", ""), "output": out})
            continue
        if role == "assistant":
            if isinstance(content, str) and content:
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": content}]})
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                items.append({"type": "function_call",
                              "name": fn.get("name", ""),
                              "arguments": fn.get("arguments") or "{}",
                              "call_id": tc.get("id", "")})
            continue
        # user (string or multimodal)
        if isinstance(content, str):
            items.append({"type": "message", "role": "user",
                          "content": [{"type": "input_text", "text": content}]})
        elif isinstance(content, list):
            parts: list[dict[str, Any]] = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    parts.append({"type": "input_text", "text": p.get("text", "")})
                elif p.get("type") == "image_url":
                    url = p.get("image_url")
                    url = url.get("url") if isinstance(url, dict) else url
                    parts.append({"type": "input_image", "image_url": url})
            items.append({"type": "message", "role": "user", "content": parts})
    return items


def _to_tools(tools_schema: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools_schema:
        return None
    out = []
    for t in tools_schema:
        fn = t.get("function", t)
        out.append({"type": "function", "name": fn.get("name"),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {})})
    return out


class CodexProvider:
    name = "codex"

    def __init__(self, config) -> None:
        self.config = config

    def _headers(self, access: str, account_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "OpenAI-Beta": "responses=experimental",
            "originator": "codex_cli_rs",
            "session_id": str(uuid.uuid4()),
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

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
        started = time.monotonic()
        body: dict[str, Any] = {
            "model": self.config.model.name,
            "instructions": _instructions(system_prompt, messages),
            "input": _to_input(messages),
            "stream": True,
            "store": False,
        }
        tools = _to_tools(tools_schema)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if getattr(self.config.model, "reasoning", False):
            from sunday.runtime.providers.openai_compat import _reasoning_effort
            body["reasoning"] = {"summary": "auto", "effort": _reasoning_effort(self.config)}

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        finish_reason: str | None = None
        usage_prompt: int | None = None
        usage_completion: int | None = None

        async def _run(access: str, account_id: str):
            nonlocal finish_reason, usage_prompt, usage_completion
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as c:
                async with c.stream("POST", RESPONSES_URL, json=body,
                                    headers=self._headers(access, account_id)) as resp:
                    if resp.status_code != 200:
                        detail = (await resp.aread()).decode("utf-8", "replace")[:300]
                        raise RuntimeError(f"codex {resp.status_code}: {detail}")
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            ev = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        t = ev.get("type", "")
                        if t == "response.output_text.delta":
                            piece = ev.get("delta", "")
                            if piece:
                                content_parts.append(piece)
                                if on_delta is not None:
                                    await on_delta(piece)
                        elif t.startswith("response.reasoning") and t.endswith(".delta"):
                            piece = ev.get("delta", "")
                            if piece:
                                reasoning_parts.append(piece)
                                if on_reasoning is not None:
                                    await on_reasoning(piece)
                        elif t == "response.output_item.done":
                            item = ev.get("item", {})
                            if item.get("type") == "function_call":
                                tool_calls.append(ToolCall(
                                    id=item.get("call_id", "") or item.get("id", ""),
                                    name=item.get("name", ""),
                                    arguments=item.get("arguments", "") or "{}",
                                ))
                        elif t == "response.completed":
                            r = ev.get("response", {})
                            finish_reason = "tool_calls" if tool_calls else "stop"
                            u = r.get("usage") or {}
                            usage_prompt = u.get("input_tokens")
                            usage_completion = u.get("output_tokens")

        access, account_id = await _fresh_token()
        try:
            await _run(access, account_id)
        except RuntimeError as exc:
            # One forced-refresh retry on auth failure (token raced to expiry).
            if "401" in str(exc) or "expired" in str(exc).lower():
                access, account_id = await _fresh_token(force=True)
                await _run(access, account_id)
            else:
                raise

        try:
            from sunday.cost import get_store
            get_store().log_llm(
                purpose=purpose or "unknown", provider="codex",
                model=self.config.model.name,
                prompt_tokens=usage_prompt or max(1, sum(len(str(m.get("content") or "")) for m in messages) // 4),
                completion_tokens=usage_completion or max(0, sum(len(p) for p in content_parts) // 4),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001
            pass

        raw: dict[str, Any] = {"provider": "codex", "model": self.config.model.name, "streamed": True}
        if usage_prompt is not None or usage_completion is not None:
            raw["usage"] = {"prompt_tokens": usage_prompt or 0, "completion_tokens": usage_completion or 0}
        if reasoning_parts:
            raw["reasoning_content"] = "".join(reasoning_parts)
        return CompletionResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            raw=raw,
        )
