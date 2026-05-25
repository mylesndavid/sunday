"""The background daemon.

Owns the one chat, the brain, and the tool registry. Exposes:

  - Unix-socket JSON-RPC for the CLI (low-latency, local)
  - HTTP + WebSocket on 127.0.0.1:8765 for the Electron app, webhooks
    (Sendblue iMessage, VAPI), and remote-device satellites.

Run:    sunday start
Stop:   sunday stop  (or Ctrl+C in the foreground process)
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from typing import Any

import structlog
from aiohttp import web

from sunday import __version__
from sunday.brain import respond
from sunday.chat import Chat
from sunday.config import load_config
from sunday.devices.manager import DeviceManager
from sunday.ipc import IpcError, read_json, write_json
from sunday.memory import Memory, extract_facts
from sunday.paths import ensure_home, socket_path
from sunday.tools import default_registry

log = structlog.get_logger("sunday.daemon")

# Webhook handlers register themselves here at module import time, so
# channel/cloud subsystems can hook into the daemon's HTTP surface
# without daemon.py needing to know about each one explicitly.
WebhookHandler = Any  # async (request: web.Request, daemon: "Daemon") -> web.Response
_webhooks: dict[str, WebhookHandler] = {}

# Long-running background tasks subsystems want to run alongside the daemon
# (e.g. Sendblue polling, calendar watchers). Each is an async callable
# taking the Daemon and looping forever.
BackgroundTask = Any  # async (daemon: "Daemon") -> None
_background_tasks: list[BackgroundTask] = []


def register_webhook(path: str, handler: WebhookHandler) -> None:
    """Register an HTTP POST webhook. path like '/webhooks/sendblue'."""
    _webhooks[path] = handler


def register_background_task(task: BackgroundTask) -> None:
    """Register a coroutine the daemon should run on startup until shutdown."""
    _background_tasks.append(task)


class Daemon:
    def __init__(self) -> None:
        ensure_home()
        self.config = load_config()
        self.chat = Chat()
        self.memory = Memory()
        if self.memory.available:
            log.info("memory enabled", path=str(self.memory.path), count=self.memory.count())
        else:
            log.info("memory disabled (sqlite-vec or OPENAI_API_KEY missing)")
        self.registry = default_registry(self.config)
        self.devices = DeviceManager(broadcast=self._broadcast_lazy)
        self._unix_server: asyncio.Server | None = None
        self._http_runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        self._stop = asyncio.Event()
        self._started_at = time.time()

    async def _broadcast_lazy(self, event: dict[str, Any]) -> None:
        # DeviceManager is built in __init__ before the event loop runs, so we
        # can't bind _broadcast directly. Defer the call to runtime.
        await self._broadcast(event)

    # ─── shared dispatch (used by both Unix-socket and HTTP) ─────────────

    async def _say(
        self,
        text: str,
        modality: str,
        attachments: list[dict] | None = None,
    ) -> dict[str, Any]:
        reply = await respond(
            self.chat, text, modality, self.config, self.registry,
            attachments=attachments,
            extras={
                "broadcast": self._broadcast,
                "devices":   self.devices,
                "memory":    self.memory,
            },
        )
        await self._broadcast({"type": "reply", "modality": modality, "content": reply})
        # Fire-and-forget memory extraction so the brain returns immediately.
        if self.memory.available:
            asyncio.create_task(self._extract_memories(text, reply))
        return {"reply": reply}

    async def _extract_memories(self, user_text: str, sunday_reply: str) -> None:
        try:
            facts = await extract_facts(user_text, sunday_reply, self.config)
            if facts:
                await self.memory.store_many(facts, source="auto")
                log.info("memory extracted", new_facts=len(facts))
        except Exception as exc:  # noqa: BLE001
            log.warning("memory extraction task failed", error=str(exc))

    async def _dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "say":
            text = (params.get("text") or "").strip()
            attachments = params.get("attachments")
            if not text and not attachments:
                raise IpcError("'text' or 'attachments' is required")
            return await self._say(
                text,
                params.get("modality") or "cli",
                attachments if isinstance(attachments, list) else None,
            )

        if method == "log":
            limit = int(params.get("limit") or 20)
            return {"messages": [m.to_json() for m in self.chat.recent(limit=limit)]}

        if method == "status":
            return {
                "version": __version__,
                "model": f"{self.config.model.provider}/{self.config.model.name}",
                "messages": self.chat.count(),
                "memories": self.memory.count() if self.memory.available else None,
                "tools": self.registry.names(),
                "devices": self.devices.list_devices(),
                "server": {"host": self.config.server.host, "port": self.config.server.port},
            }

        if method == "tools":
            return {
                "tools": [
                    {"name": t.name, "description": t.description}
                    for t in self.registry.list_tools()
                ]
            }

        if method == "stop":
            self._stop.set()
            return {"ok": True}

        raise IpcError(f"unknown method: {method}")

    async def _broadcast(self, event: dict[str, Any]) -> None:
        if not self._ws_clients:
            return
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_json(event)
            except (ConnectionError, RuntimeError):
                dead.append(ws)
        for ws in dead:
            self._ws_clients.discard(ws)

    # ─── Unix socket ─────────────────────────────────────────────────────

    async def _handle_unix(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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
            except Exception as exc:  # noqa: BLE001
                log.exception("unix dispatch failed", method=method)
                await write_json(writer, {"error": f"{type(exc).__name__}: {exc}"})
        except IpcError as exc:
            log.warning("ipc framing error", error=str(exc))
        finally:
            writer.close()
            await writer.wait_closed()

    # ─── HTTP + WebSocket ────────────────────────────────────────────────

    async def _http_say(self, request: web.Request) -> web.Response:
        body = await request.json()
        text = (body.get("text") or "").strip()
        modality = body.get("modality") or "http"
        attachments = body.get("attachments")
        if not text and not attachments:
            return web.json_response({"error": "'text' or 'attachments' is required"}, status=400)
        try:
            result = await self._say(
                text, modality,
                attachments if isinstance(attachments, list) else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("http say failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    async def _http_log(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        return web.json_response(
            {"messages": [m.to_json() for m in self.chat.recent(limit=limit)]}
        )

    async def _http_status(self, request: web.Request) -> web.Response:
        return web.json_response(await self._dispatch("status", {}))

    async def _http_tools(self, request: web.Request) -> web.Response:
        return web.json_response(await self._dispatch("tools", {}))

    async def _http_health(self, request: web.Request) -> web.Response:
        """Rich health snapshot for admin UIs — daemon stats, satellites,
        memory growth, skills, recent tool activity."""
        from sunday.skills import list_skills
        recent_memories = []
        if self.memory.available:
            try:
                recent_memories = [
                    {
                        "id": m.id,
                        "content": m.content,
                        "source": m.source,
                        "created_at": m.created_at,
                    }
                    for m in self.memory.all(limit=20)
                ]
            except Exception:  # noqa: BLE001
                pass

        skills_list = []
        try:
            skills_list = [
                {"slug": s.slug, "name": s.name, "description": s.description}
                for s in list_skills()
            ]
        except Exception:  # noqa: BLE001
            pass

        # Recent tool activity from the chat log — last 50 tool messages
        recent_tools = []
        for m in self.chat.recent(limit=80):
            if m.role == "tool":
                recent_tools.append({
                    "tool_name": (m.metadata or {}).get("tool_name", "?"),
                    "modality":  m.modality,
                    "created_at": m.created_at,
                })
        recent_tools = recent_tools[-20:]

        return web.json_response({
            "daemon": {
                "version":     __version__,
                "model":       f"{self.config.model.provider}/{self.config.model.name}",
                "messages":    self.chat.count(),
                "tools_count": len(self.registry.names()),
                "uptime_s":    round(time.time() - self._started_at, 1),
                "started_at":  self._started_at,
            },
            "memory": {
                "available": self.memory.available,
                "total":     self.memory.count() if self.memory.available else 0,
                "recent":    recent_memories,
            },
            "skills":  skills_list,
            "devices": self.devices.list_devices(),
            "recent_tool_calls": recent_tools,
        })

    async def _ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        log.info("ws client connected", remote=request.remote)
        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    await ws.send_json({"error": "invalid JSON"})
                    continue
                method = str(payload.get("method") or "")
                params = payload.get("params") or {}
                req_id = payload.get("id")
                try:
                    result = await self._dispatch(method, params)
                    await ws.send_json({"id": req_id, "result": result})
                except IpcError as exc:
                    await ws.send_json({"id": req_id, "error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    log.exception("ws dispatch failed", method=method)
                    await ws.send_json({"id": req_id, "error": f"{type(exc).__name__}: {exc}"})
        finally:
            self._ws_clients.discard(ws)
            log.info("ws client disconnected")
        return ws

    async def _webhook_dispatch(self, request: web.Request) -> web.Response:
        path = request.path
        handler = _webhooks.get(path)
        if handler is None:
            return web.json_response({"error": "no such webhook"}, status=404)
        try:
            return await handler(request, self)
        except Exception as exc:  # noqa: BLE001
            log.exception("webhook failed", path=path)
            return web.json_response({"error": str(exc)}, status=500)

    def _build_http_app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/v1/say", self._http_say)
        app.router.add_get("/v1/log", self._http_log)
        app.router.add_get("/v1/status", self._http_status)
        app.router.add_get("/v1/tools", self._http_tools)
        app.router.add_get("/v1/health", self._http_health)
        app.router.add_get("/v1/ws", self._ws_handler)
        # Satellite devices connect here.
        app.router.add_get("/v1/devices/ws", self.devices.handle_ws)
        # Catch-all webhook dispatcher — modules register paths in _webhooks.
        app.router.add_post("/webhooks/{name}", self._webhook_dispatch)
        return app

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def run(self) -> None:
        # Unix socket. Record the inode we create so the shutdown handler
        # only unlinks our own socket — protects against stop/start races
        # where the previous daemon's cleanup runs after the new one's
        # startup and would otherwise delete the fresh socket file.
        sock = socket_path()
        if sock.exists():
            sock.unlink()
        self._unix_server = await asyncio.start_unix_server(self._handle_unix, path=str(sock))
        try:
            self._sock_inode: int | None = sock.stat().st_ino
        except OSError:
            self._sock_inode = None

        # HTTP + WS
        app = self._build_http_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.config.server.host, self.config.server.port)
        await site.start()
        self._http_runner = runner

        log.info(
            "sunday up",
            socket=str(sock),
            http=f"http://{self.config.server.host}:{self.config.server.port}",
            model=f"{self.config.model.provider}/{self.config.model.name}",
            tools=self.registry.names(),
        )

        # Kick off any registered background tasks (Sendblue poller, etc.).
        self._bg_tasks: list[asyncio.Task] = []
        for task_fn in _background_tasks:
            self._bg_tasks.append(asyncio.create_task(task_fn(self)))
        if self._bg_tasks:
            log.info("background tasks started", count=len(self._bg_tasks))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)

        try:
            await self._stop.wait()
        finally:
            log.info("sunday down")
            for t in getattr(self, "_bg_tasks", []):
                t.cancel()
            self._unix_server.close()
            await self._unix_server.wait_closed()
            if self._http_runner is not None:
                await self._http_runner.cleanup()
            # Only remove the socket file if it still has the inode we
            # created — otherwise a newer daemon has already taken it over.
            if sock.exists() and self._sock_inode is not None:
                try:
                    if sock.stat().st_ino == self._sock_inode:
                        sock.unlink()
                except OSError:
                    pass
            self.chat.close()


def main() -> None:
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
    )
    # Only render the banner on attached terminals — keeps systemd /
    # launchd logs clean of ANSI gradient escape sequences.
    import sys as _sys
    if _sys.stdout.isatty():
        from sunday.banner import render as _render
        _render(tagline=f"a personal AI you self-host  ·  v{__version__}")
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
