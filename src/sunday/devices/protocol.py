"""Device wire protocol — JSON over WebSocket.

All frames are JSON objects with a `type` discriminator:

  Satellite → main:
    {"type": "register",  "device_id": str, "capabilities": [str], "platform": str}
    {"type": "response",  "id": str, "result": object | None, "error": str | None}
    {"type": "event",     "kind": str, ...}

  Main → satellite:
    {"type": "command",   "id": str, "method": str, "params": object}
    {"type": "ping"}

  Both directions:
    {"type": "pong"}
"""

from __future__ import annotations

from typing import Any

FRAME_REGISTER = "register"
FRAME_COMMAND = "command"
FRAME_RESPONSE = "response"
FRAME_EVENT = "event"
FRAME_PING = "ping"
FRAME_PONG = "pong"


def register_frame(device_id: str, capabilities: list[str], platform_name: str) -> dict[str, Any]:
    return {
        "type": FRAME_REGISTER,
        "device_id": device_id,
        "capabilities": capabilities,
        "platform": platform_name,
    }


def command_frame(req_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"type": FRAME_COMMAND, "id": req_id, "method": method, "params": params}


def response_frame(req_id: str, result: Any = None, error: str | None = None) -> dict[str, Any]:
    if error is not None:
        return {"type": FRAME_RESPONSE, "id": req_id, "error": error}
    return {"type": FRAME_RESPONSE, "id": req_id, "result": result}


def event_frame(kind: str, **payload: Any) -> dict[str, Any]:
    return {"type": FRAME_EVENT, "kind": kind, **payload}
