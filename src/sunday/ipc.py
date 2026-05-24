"""JSON-RPC over Unix domain socket.

Newline-delimited JSON frames. Request: {"method": str, "params": object}.
Response: {"result": any} or {"error": str}.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

IPC_LIMIT_BYTES = 16 * 1024 * 1024


class IpcError(RuntimeError):
    pass


@dataclass(slots=True)
class Request:
    method: str
    params: dict[str, Any]


async def read_json(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise IpcError("empty IPC frame")
    try:
        payload = json.loads(line.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise IpcError(f"invalid IPC JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise IpcError("IPC payload must be a JSON object")
    return payload


async def write_json(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    writer.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
    await writer.drain()


async def call(socket: str | Path, method: str, params: dict[str, Any] | None = None) -> Any:
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket), limit=IPC_LIMIT_BYTES)
    except OSError as exc:
        raise IpcError(f"Sunday daemon is not reachable at {socket}") from exc

    try:
        await write_json(writer, {"method": method, "params": params or {}})
        response = await read_json(reader)
    finally:
        writer.close()
        await writer.wait_closed()

    if "error" in response:
        raise IpcError(str(response["error"]))
    return response.get("result")
