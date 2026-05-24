"""The background daemon.

Owns the one chat and the LLM call. Exposes JSON-RPC over a Unix socket so
the CLI (and the Electron app, later, via a WebSocket bridge) can ask
Sunday to respond, read the chat, or check status.

Run with: sunday start
Stop with: sunday stop  (or Ctrl+C in the foreground process)
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog

from sunday import __version__
from sunday.brain import respond
from sunday.chat import Chat
from sunday.config import load_config
from sunday.ipc import IpcError, read_json, write_json
from sunday.paths import ensure_home, socket_path

log = structlog.get_logger("sunday.daemon")


class Daemon:
    def __init__(self) -> None:
        ensure_home()
        self.config = load_config()
        self.chat = Chat()
        self._server: asyncio.Server | None = None
        self._stop = asyncio.Event()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "say":
            text = (params.get("text") or "").strip()
            if not text:
                raise IpcError("'text' is required")
            modality = params.get("modality") or "cli"
            reply = await respond(self.chat, text, modality, self.config)
            return {"reply": reply}

        if method == "log":
            limit = int(params.get("limit") or 20)
            return {"messages": [m.to_json() for m in self.chat.recent(limit=limit)]}

        if method == "status":
            return {
                "version": __version__,
                "model": f"{self.config.model.provider}/{self.config.model.name}",
                "messages": self.chat.count(),
            }

        if method == "stop":
            self._stop.set()
            return {"ok": True}

        raise IpcError(f"unknown method: {method}")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            payload = await read_json(reader)
            method = str(payload.get("method") or "")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise IpcError("params must be an object")
            try:
                result = await self._dispatch(method, params)
                await write_json(writer, {"result": result})
            except IpcError as exc:
                await write_json(writer, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — top-level dispatch boundary
                log.exception("dispatch failed", method=method)
                await write_json(writer, {"error": f"{type(exc).__name__}: {exc}"})
        except IpcError as exc:
            log.warning("ipc framing error", error=str(exc))
        finally:
            writer.close()
            await writer.wait_closed()

    async def run(self) -> None:
        sock = socket_path()
        if sock.exists():
            sock.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(sock))
        log.info("sunday up", socket=str(sock), model=f"{self.config.model.provider}/{self.config.model.name}")

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        try:
            await self._stop.wait()
        finally:
            log.info("sunday down")
            self._server.close()
            await self._server.wait_closed()
            if sock.exists():
                sock.unlink()
            self.chat.close()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
