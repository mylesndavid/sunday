"""Realtime voice mode — foundation.

Speech-to-speech with the model, wired to Sunday's brain. Realtime is a direct
bidirectional WebSocket/WebRTC API (NOT OpenRouter / chat-completions): OpenAI
`gpt-realtime` or Google `gemini-live-2.5-flash`. The browser connects straight
to the provider with a SHORT-LIVED ephemeral token the daemon mints here, so the
real API key never leaves the Mac, and we inject Sunday's system prompt + tools
into the session so the realtime model can call Sunday's tools (function calls
get executed by the daemon and the results streamed back).

Status: FOUNDATION. This mints the OpenAI Realtime session (prompt + tools +
voice). Still to build: the renderer voice UI (WebRTC mic/playback), the
tool-call execution bridge, barge-in, and a Gemini Live provider.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from sunday.credentials import get_credential

log = structlog.get_logger("sunday.realtime")

OPENAI_REALTIME_SESSIONS = "https://api.openai.com/v1/realtime/sessions"


def _realtime_tools(registry) -> list[dict[str, Any]]:
    """Sunday's tools in the Realtime API's flat function shape (it wants
    {type, name, description, parameters} — not the chat-completions nesting)."""
    out: list[dict[str, Any]] = []
    try:
        for t in registry.as_openai_schema():
            fn = t.get("function", t)
            out.append({"type": "function", "name": fn.get("name"),
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}})})
    except Exception:  # noqa: BLE001
        pass
    return out


async def create_openai_session(config, registry, system_prompt: str) -> dict[str, Any]:
    """Mint an ephemeral OpenAI Realtime session. Returns the session JSON,
    including client_secret.value — the short-lived token the browser uses to
    open the realtime connection directly. Raises with a clear message if no
    OpenAI platform key is configured (realtime needs sk-…, not the ChatGPT/
    codex login)."""
    key = get_credential("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "Realtime voice needs an OpenAI platform API key (OPENAI_API_KEY). "
            "Your ChatGPT/codex login doesn't cover the Realtime API — add a key "
            "in Settings → Advanced, or set OPENAI_API_KEY."
        )
    v = config.voice
    body = {
        "model": v.realtime_model,                     # e.g. gpt-realtime
        "voice": v.tts_voice,
        "modalities": ["audio", "text"],
        "instructions": system_prompt,
        "input_audio_transcription": {"model": v.stt_model},
        "turn_detection": {"type": "server_vad"},      # let the model handle barge-in
        "tools": _realtime_tools(registry),
        "tool_choice": "auto",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(OPENAI_REALTIME_SESSIONS,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json",
                                  "OpenAI-Beta": "realtime=v1"},
                         json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"realtime session {r.status_code}: {r.text[:300]}")
        data = r.json()
    log.info("realtime session minted", model=v.realtime_model, tools=len(body["tools"]))
    return data
