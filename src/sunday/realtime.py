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

OPENAI_CLIENT_SECRETS = "https://api.openai.com/v1/realtime/client_secrets"
GEMINI_TOKENS = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
GEMINI_LIVE_MODEL = "gemini-2.5-flash-native-audio-preview"   # cheapest realtime; configurable


def _gemini_tools(registry) -> list[dict[str, Any]]:
    """Sunday's tools as Gemini functionDeclarations."""
    decls: list[dict[str, Any]] = []
    try:
        for t in registry.as_openai_schema():
            fn = t.get("function", t)
            decls.append({"name": fn.get("name"), "description": fn.get("description", ""),
                          "parameters": fn.get("parameters", {"type": "object", "properties": {}})})
    except Exception:  # noqa: BLE001
        pass
    return [{"functionDeclarations": decls}] if decls else []


async def create_gemini_session(config, registry, system_prompt: str) -> dict[str, Any]:
    """Mint an ephemeral Gemini Live token + the setup message the browser sends.
    The browser opens the Live API WebSocket directly with ?access_token=; the
    real Google key stays here. Needs GEMINI_API_KEY (or GOOGLE_API_KEY)."""
    key = get_credential("GEMINI_API_KEY") or get_credential("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError(
            "Realtime voice on Gemini needs a Google AI key (GEMINI_API_KEY). "
            "Add one in Settings → Brain → Advanced, or set GEMINI_API_KEY."
        )
    model = getattr(config.voice, "realtime_gemini_model", GEMINI_LIVE_MODEL)
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{GEMINI_TOKENS}?key={key}",
                         json={"uses": 1, "liveConnectConstraints": {"model": f"models/{model}"}})
        if r.status_code >= 400:
            raise RuntimeError(f"gemini token {r.status_code}: {r.text[:300]}")
        token = (r.json() or {}).get("name")
    if not token:
        raise RuntimeError("gemini ephemeral token: no token returned")
    log.info("gemini live session minted", model=model)
    return {
        "provider": "gemini",
        "model": model,
        "ws_url": f"{GEMINI_WS}?access_token={token}",
        # the browser sends this immediately after the socket opens
        "setup": {"setup": {
            "model": f"models/{model}",
            "generationConfig": {"responseModalities": ["AUDIO"]},
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "tools": _gemini_tools(registry),
        }},
    }


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
    """Mint an ephemeral OpenAI Realtime client secret (GA API). Returns the
    response JSON — the short-lived token is at top-level `value`; the browser
    uses it to open the WebRTC connection directly (POST .../realtime/calls).
    Raises with a clear message if no OpenAI platform key is configured
    (realtime needs sk-…, not the ChatGPT/codex login).

    GA shape (replaced the beta /v1/realtime/sessions): the config nests under a
    `session` object — voice under audio.output, turn detection under
    audio.input, tools at the top of the session."""
    key = get_credential("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "Realtime voice needs an OpenAI platform API key (OPENAI_API_KEY). "
            "Your ChatGPT/codex login doesn't cover the Realtime API — add a key "
            "in Settings → Brain → Advanced, or set OPENAI_API_KEY."
        )
    v = config.voice
    tools = _realtime_tools(registry)
    session = {
        "type": "realtime",
        "model": v.realtime_model,                     # e.g. gpt-realtime
        "instructions": system_prompt,
        "audio": {
            "input": {"turn_detection": {"type": "server_vad"}},   # barge-in
            "output": {"voice": v.tts_voice},
        },
        "tools": tools,
        "tool_choice": "auto",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(OPENAI_CLIENT_SECRETS,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"},
                         json={"session": session})
        if r.status_code >= 400:
            raise RuntimeError(f"realtime client secret {r.status_code}: {r.text[:300]}")
        data = r.json()
    log.info("realtime client secret minted", model=v.realtime_model, tools=len(tools))
    # normalize: surface the ephemeral token + model where the renderer expects
    data.setdefault("model", v.realtime_model)
    return data
