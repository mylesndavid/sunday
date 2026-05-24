"""DeviceManager — main-daemon side.

Tracks connected satellite daemons. Tools call `command()` to dispatch a
method to a specific device and await its response. Satellites can also
push events (screen frames, etc.) which the manager re-broadcasts via the
daemon's broadcast callback.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

import structlog
from aiohttp import web

from sunday.devices.protocol import (
    FRAME_COMMAND,
    FRAME_EVENT,
    FRAME_REGISTER,
    FRAME_RESPONSE,
    command_frame,
)

log = structlog.get_logger("sunday.devices.manager")


@dataclass
class ConnectedDevice:
    device_id: str
    capabilities: list[str]
    platform: str
    ws: web.WebSocketResponse
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class DeviceManager:
    def __init__(
        self,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._devices: dict[str, ConnectedDevice] = {}
        self._broadcast = broadcast

    def list_devices(self) -> list[dict[str, Any]]:
        return [
            {
                "device_id": d.device_id,
                "capabilities": d.capabilities,
                "platform": d.platform,
            }
            for d in self._devices.values()
        ]

    def get(self, device_id: str) -> ConnectedDevice | None:
        return self._devices.get(device_id)

    async def command(
        self,
        device_id: str,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 60.0,
    ) -> Any:
        device = self._devices.get(device_id)
        if device is None:
            raise RuntimeError(f"no such device connected: {device_id}")

        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        device.pending[req_id] = fut

        try:
            await device.ws.send_json(command_frame(req_id, method, params or {}))
        except (ConnectionError, RuntimeError) as exc:
            device.pending.pop(req_id, None)
            raise RuntimeError(f"send to {device_id} failed: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            device.pending.pop(req_id, None)
            raise RuntimeError(f"timeout waiting for {device_id}.{method}")

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Accept an incoming satellite connection and pump its frames."""
        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)

        device: ConnectedDevice | None = None
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = msg.json()
                except Exception:  # noqa: BLE001
                    log.warning("invalid JSON from satellite")
                    continue
                kind = payload.get("type")

                if kind == FRAME_REGISTER:
                    device = ConnectedDevice(
                        device_id=str(payload.get("device_id") or "unknown"),
                        capabilities=list(payload.get("capabilities") or []),
                        platform=str(payload.get("platform") or ""),
                        ws=ws,
                    )
                    self._devices[device.device_id] = device
                    log.info(
                        "device registered",
                        device_id=device.device_id,
                        capabilities=device.capabilities,
                        platform=device.platform,
                    )
                    if self._broadcast is not None:
                        await self._broadcast({
                            "type": "device_online",
                            "device_id": device.device_id,
                            "capabilities": device.capabilities,
                        })
                    continue

                if kind == FRAME_RESPONSE and device is not None:
                    req_id = payload.get("id")
                    fut = device.pending.pop(req_id, None) if req_id else None
                    if fut and not fut.done():
                        if "error" in payload and payload["error"]:
                            fut.set_exception(RuntimeError(payload["error"]))
                        else:
                            fut.set_result(payload.get("result"))
                    continue

                if kind == FRAME_EVENT and device is not None:
                    log.debug("device event", device_id=device.device_id, kind=payload.get("kind"))
                    if self._broadcast is not None:
                        await self._broadcast({
                            "type": "device_event",
                            "device_id": device.device_id,
                            **{k: v for k, v in payload.items() if k != "type"},
                        })
                    continue
        finally:
            if device is not None:
                self._devices.pop(device.device_id, None)
                log.info("device disconnected", device_id=device.device_id)
                if self._broadcast is not None:
                    await self._broadcast({
                        "type": "device_offline",
                        "device_id": device.device_id,
                    })
        return ws
