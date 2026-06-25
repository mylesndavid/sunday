"""VAPI outbound voice calls.

Sunday can pick up the phone on the user's behalf. The brain decides
*why* (purpose) and *who* (number); VAPI handles the actual telephony,
voice synthesis, real-time transcription, and back-and-forth.

When a call ends, VAPI fires our /webhooks/vapi endpoint with the
transcript + summary. We write a single 'sunday' message into the one
chat with modality='vapi' so the next time the user talks to Sunday,
the conversation context already includes "I called X about Y, here's
how it went."

Credentials (via `sunday credential set` or env):
  VAPI_API_KEY              your VAPI private API key
  VAPI_PHONE_NUMBER_ID      the VAPI-provisioned number you call FROM
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog
from aiohttp import web

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.prompt import stable_prefix
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.vapi")


def _call_system_prompt(purpose: str, transcript_context: str | None = None) -> str:
    base = (
        stable_prefix()
        + "\n\n# Voice call\n\n"
        "You're on the phone right now. Speak in short, natural turns. No markdown, "
        "no lists, no formal language. End the call when the purpose is achieved or "
        "the other party signals they're done.\n\n"
        f"# Purpose of this call\n\n{purpose}\n"
    )
    if transcript_context:
        base += f"\n# Context from your earlier conversation with the user\n\n{transcript_context}\n"
    return base


async def _create_call(
    to_number: str,
    purpose: str,
    config: SundayConfig,
    context_snippet: str | None = None,
) -> dict[str, Any]:
    api_key = get_credential("VAPI_API_KEY")
    phone_id = get_credential("VAPI_PHONE_NUMBER_ID")
    if not api_key:
        return {"error": "VAPI_API_KEY is missing. Run: sunday credential set VAPI_API_KEY <key>"}
    if not phone_id:
        return {"error": "VAPI_PHONE_NUMBER_ID is missing."}

    payload: dict[str, Any] = {
        "phoneNumberId": phone_id,
        "customer": {"number": to_number},
        "assistant": {
            "model": {
                "provider": config.vapi.model_provider,
                "model": config.vapi.model_name,
                "messages": [
                    {"role": "system", "content": _call_system_prompt(purpose, context_snippet)},
                ],
            },
            "voice": {
                "provider": config.vapi.voice_provider,
                "voiceId": config.vapi.voice_id,
            },
            "transcriber": {
                "provider": config.vapi.transcriber_provider,
                "model": config.vapi.transcriber_model,
            },
            "firstMessage": config.vapi.first_message,
        },
    }

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(f"{config.vapi.api_base}/call/phone", headers=headers, json=payload)
    try:
        data = res.json()
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        return {"error": f"vapi {res.status_code}: {data}"}
    log.info("vapi call placed", call_id=data.get("id"), to=to_number)
    return {"ok": True, "call_id": data.get("id"), "status": data.get("status"), "data": data}


# ─── reading calls back (Calls view pulls from VAPI directly) ─────────────
#
# The daemon is local-only (127.0.0.1), so VAPI's webhook can't reach it and
# a webhook-fed store would be empty. VAPI already keeps the call list,
# transcripts, and recordings — so the Calls view just queries them through
# the daemon, which holds the API key. The key never reaches the renderer.

_VAPI_BASE = "https://api.vapi.ai"


def _coalesce(d: dict[str, Any], *keys: str) -> Any:
    """First present, non-None value among nested dotted keys."""
    for key in keys:
        cur: Any = d
        for part in key.split("."):
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(part)
        if cur not in (None, ""):
            return cur
    return None


def _duration_seconds(call: dict[str, Any]) -> float | None:
    secs = _coalesce(call, "durationSeconds", "duration")
    if isinstance(secs, (int, float)):
        return float(secs)
    # VAPI also exposes startedAt/endedAt ISO timestamps we can subtract.
    started = call.get("startedAt")
    ended = call.get("endedAt")
    if isinstance(started, str) and isinstance(ended, str):
        from datetime import datetime
        try:
            s = datetime.fromisoformat(started.replace("Z", "+00:00"))
            e = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            return max(0.0, (e - s).total_seconds())
        except ValueError:
            return None
    return None


def _recording_url(call: dict[str, Any]) -> str | None:
    return _coalesce(
        call,
        "recordingUrl",
        "artifact.recordingUrl",
        "artifact.stereoRecordingUrl",
        "stereoRecordingUrl",
    )


def _assistant_label(call: dict[str, Any]) -> str | None:
    """A human label for the call: assistant name, else the purpose we set."""
    name = _coalesce(call, "assistant.name", "assistantId")
    if name:
        return str(name)
    # We seed the system prompt with "# Purpose of this call"; surface it.
    messages = _coalesce(call, "assistant.model.messages") or []
    if isinstance(messages, list):
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                content = str(m.get("content") or "")
                marker = "# Purpose of this call"
                if marker in content:
                    after = content.split(marker, 1)[1].strip()
                    first = after.splitlines()[0].strip() if after else ""
                    if first:
                        return first[:120]
    return None


def _trim_call_row(call: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call.get("id"),
        "createdAt": _coalesce(call, "createdAt", "startedAt"),
        "to": _coalesce(call, "customer.number", "phoneNumber.number") or "?",
        "status": call.get("status"),
        "endedReason": call.get("endedReason"),
        "durationSeconds": _duration_seconds(call),
        "assistantName": _assistant_label(call),
        "hasRecording": bool(_recording_url(call)),
    }


async def list_calls(limit: int = 50) -> dict[str, Any]:
    """Pull the recent call list from VAPI. Returns {calls:[...]} or {error}."""
    api_key = get_credential("VAPI_API_KEY")
    if not api_key:
        return {"error": "VAPI_API_KEY is missing. Run: sunday credential set VAPI_API_KEY <key>"}
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"limit": str(max(1, min(int(limit), 100)))}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{_VAPI_BASE}/call", headers=headers, params=params)
    try:
        data = res.json()
    except ValueError:
        data = {"raw": res.text}
    if res.status_code >= 400:
        return {"error": f"vapi {res.status_code}: {data}"}
    calls = data if isinstance(data, list) else (data.get("results") or data.get("calls") or [])
    rows = [_trim_call_row(c) for c in calls if isinstance(c, dict)]
    rows.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
    return {"calls": rows[:limit]}


async def get_call(call_id: str) -> dict[str, Any]:
    """Pull a single call's detail (transcript, summary, recording) from VAPI."""
    api_key = get_credential("VAPI_API_KEY")
    if not api_key:
        return {"error": "VAPI_API_KEY is missing. Run: sunday credential set VAPI_API_KEY <key>"}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.get(f"{_VAPI_BASE}/call/{call_id}", headers=headers)
    try:
        call = res.json()
    except ValueError:
        call = {"raw": res.text}
    if res.status_code >= 400:
        return {"error": f"vapi {res.status_code}: {call}"}
    if not isinstance(call, dict):
        return {"error": "vapi returned an unexpected call shape"}
    messages = _coalesce(call, "artifact.messages", "messages") or []
    return {
        "id": call.get("id"),
        "createdAt": _coalesce(call, "createdAt", "startedAt"),
        "to": _coalesce(call, "customer.number", "phoneNumber.number") or "?",
        "status": call.get("status"),
        "endedReason": call.get("endedReason"),
        "durationSeconds": _duration_seconds(call),
        "summary": _coalesce(call, "summary", "analysis.summary"),
        "transcript": _coalesce(call, "transcript", "artifact.transcript"),
        "recordingUrl": _recording_url(call),
        "assistantName": _assistant_label(call),
        "messages": messages if isinstance(messages, list) else [],
    }


# ─── webhook ─────────────────────────────────────────────────────────────


async def _webhook_handler(request: web.Request, daemon: Any) -> web.Response:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid JSON"}, status=400)

    msg = body.get("message") or {}
    event = msg.get("type") or ""
    call = msg.get("call") or {}

    if event in ("end-of-call-report", "call-end"):
        to = (call.get("customer") or {}).get("number") or "?"
        transcript = msg.get("transcript") or ""
        summary = msg.get("summary") or ""
        reason = msg.get("endedReason") or "ended"
        duration = msg.get("durationSeconds") or msg.get("duration") or 0
        body_text = (
            f"Call to {to} ended ({reason}, {duration:.0f}s).\n"
            + (f"Summary: {summary}\n\n" if summary else "")
            + (f"Transcript:\n{transcript}" if transcript else "")
        ).strip()
        daemon.chat.append(
            "sunday",
            body_text or f"Call to {to} ended ({reason}).",
            "vapi",
            metadata={
                "call_id": call.get("id"),
                "ended_reason": reason,
                "duration": duration,
                "to": to,
            },
        )
        log.info("vapi call recorded", call_id=call.get("id"), reason=reason)
        return web.json_response({"ok": True})

    if event == "status-update":
        log.debug("vapi status", call_id=call.get("id"), status=msg.get("status"))
        return web.json_response({"ok": True})

    if event in ("transcript", "speech-update"):
        # Stream-level events. We could broadcast these over WS for a live
        # transcript view — not in v0.1.
        return web.json_response({"ok": True})

    log.debug("vapi event ignored", event_type=event)
    return web.json_response({"ok": True})


# ─── tool ────────────────────────────────────────────────────────────────


_CALL_PARAMS = {
    "type": "object",
    "properties": {
        "to": {"type": "string", "description": "Phone number to call in E.164 ('+15551234567')."},
        "purpose": {
            "type": "string",
            "description": (
                "What this call is for, in Sunday's own words. Becomes the call's "
                "system prompt. Be specific: who to ask for, what to find out, "
                "what to accomplish."
            ),
        },
        "context": {
            "type": "string",
            "description": "Optional context from the user's earlier conversation that the call should reference.",
        },
    },
    "required": ["to", "purpose"],
}


async def _t_call_phone(args: dict[str, Any], ctx: ToolContext) -> Any:
    to = args.get("to")
    purpose = args.get("purpose")
    if not to or not purpose:
        return {"error": "'to' and 'purpose' are required"}
    context = args.get("context")
    return await _create_call(str(to), str(purpose), ctx.config, str(context) if context else None)


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    from sunday.daemon import register_webhook
    register_webhook("/webhooks/vapi", _webhook_handler)

    registry.register(Tool(
        name="call_phone",
        description=(
            "Place an outbound phone call via VAPI. Sunday speaks on the user's behalf "
            "with a stated purpose. The call's transcript and a summary land in the "
            "one chat (modality='vapi') when the call ends."
        ),
        parameters=_CALL_PARAMS,
        run=_t_call_phone,
    ))
