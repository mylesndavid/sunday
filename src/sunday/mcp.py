"""MCP client — connect Sunday to Model Context Protocol servers.

Paste a standard `mcpServers` config (the Claude Desktop / Cursor shape) and
Sunday connects to each server, lists its tools, and registers them as
first-class Sunday tools (named `<server>_<tool>`). Two transports:

  remote (Streamable HTTP):  {"url": "https://…", "headers": {...}}
  local  (stdio):            {"command": "npx", "args": [...], "env": {...}}

Config lives at ~/.sunday/mcp.json. Servers are connected in the background
on daemon start and whenever the config is saved; discovered tools appear on
the next turn. JSON-RPC 2.0 throughout.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.mcp")

PROTOCOL_VERSION = "2025-06-18"
_CLIENT_INFO = {"name": "sunday", "version": "0.1"}

# live state for the /v1/mcp status view
STATUS: dict[str, dict[str, Any]] = {}


def config_path() -> Path:
    return sunday_home() / "mcp.json"


def load_config() -> dict[str, Any]:
    p = config_path()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"mcpServers": {}}


def save_config(text_or_obj: str | dict) -> dict[str, Any]:
    """Parse a pasted config (tolerant: accepts the full {mcpServers:{...}}
    or just the inner {name:{...}} map) and persist it."""
    obj = text_or_obj if isinstance(text_or_obj, dict) else json.loads(text_or_obj)
    if "mcpServers" not in obj:
        # they pasted just the server map
        obj = {"mcpServers": obj}
    config_path().write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return obj


def _safe(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()


# ─── built-in one-click connectors ──────────────────────────────────────────
# Curated local (stdio) MCP servers Sunday can wire with one toggle, so they
# work out of the box instead of asking the user to paste JSON. Keyed by id.
BUILTIN_SERVERS: dict[str, dict[str, Any]] = {
    "playwright": {
        "title": "Browser (Playwright)",
        "desc": "Control your real, logged-in Chrome — navigate, click, type, read the page via the accessibility tree (not pixels). Uses the Playwright MCP extension so it drives your actual tab + sessions.",
        "config": {"command": "npx", "args": ["-y", "@playwright/mcp@latest", "--extension"]},
        "needs": "node",   # the daemon spawns `npx`; needs Node 18+ on PATH
        "setup": [
            "Install the Playwright MCP Chrome extension (one time).",
            "Open the extension on the tab you want Sunday to drive and click 'share'.",
            "Then ask Sunday to do something in the browser — browser_* tools are live.",
        ],
        "setup_url": "https://github.com/microsoft/playwright/tree/main/packages/extension#readme",
    },
}


def node_available() -> bool:
    import shutil
    return bool(shutil.which("npx") or shutil.which("node"))


def builtin_status() -> list[dict[str, Any]]:
    """The built-in connectors + whether each is currently enabled in mcp.json
    and whether its runtime requirement (Node) is satisfied."""
    enabled = set((load_config().get("mcpServers") or {}).keys())
    node = node_available()
    out = []
    for bid, b in BUILTIN_SERVERS.items():
        out.append({
            "id": bid, "title": b["title"], "desc": b["desc"],
            "enabled": bid in enabled,
            "ready": (b.get("needs") != "node") or node,
            "needs": b.get("needs"), "setup": b.get("setup", []), "setup_url": b.get("setup_url"),
        })
    return out


def set_builtin(bid: str, enabled: bool) -> dict[str, Any]:
    """Add or remove a built-in connector from mcp.json. Returns the new config."""
    b = BUILTIN_SERVERS.get(bid)
    if not b:
        raise ValueError(f"unknown built-in connector '{bid}'")
    cfg = load_config()
    servers = cfg.setdefault("mcpServers", {})
    if enabled:
        servers[bid] = dict(b["config"])
    else:
        servers.pop(bid, None)
    return save_config(cfg)


# ─── transports ───────────────────────────────────────────────────────────


class _HttpMCP:
    """Streamable-HTTP MCP transport."""

    def __init__(self, url: str, headers: dict | None = None):
        self.url = url
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **(headers or {}),
        }
        self.session_id: str | None = None
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> Any:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            body["id"] = self._next_id()
        if params is not None:
            body["params"] = params
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        async with httpx.AsyncClient(timeout=40) as client:
            res = await client.post(self.url, headers=headers, json=body)
        sid = res.headers.get("mcp-session-id") or res.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if notify:
            return None
        return _parse_rpc(res, body["id"])

    async def initialize(self):
        result = await self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        })
        await self._rpc("notifications/initialized", notify=True)
        return result

    async def list_tools(self) -> list[dict]:
        r = await self._rpc("tools/list", {})
        return (r or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> Any:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _parse_rpc(res: httpx.Response, req_id: int) -> Any:
    """Pull the JSON-RPC result from a response that's either application/json
    or text/event-stream (Streamable HTTP can answer with either)."""
    ctype = res.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in res.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if msg.get("id") == req_id:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                    return msg.get("result")
        raise RuntimeError("no matching JSON-RPC response in SSE stream")
    if res.status_code >= 400:
        raise RuntimeError(f"mcp http {res.status_code}: {res.text[:200]}")
    msg = res.json()
    if "error" in msg:
        raise RuntimeError(msg["error"].get("message", str(msg["error"])))
    return msg.get("result")


class _StdioMCP:
    """stdio MCP transport — spawns the server and talks newline-delimited
    JSON-RPC over its stdin/stdout."""

    def __init__(self, command: str, args: list[str], env: dict | None = None):
        self.command, self.args, self.env = command, args or [], env or {}
        self.proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def _start(self):
        import os
        self.proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env={**os.environ, **self.env},
        )

    async def _rpc(self, method: str, params: dict | None = None, notify: bool = False) -> Any:
        if not self.proc:
            await self._start()
        rid = None
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notify:
            self._id += 1
            rid = self._id
            body["id"] = rid
        if params is not None:
            body["params"] = params
        self.proc.stdin.write((json.dumps(body) + "\n").encode())
        await self.proc.stdin.drain()
        if notify:
            return None
        while True:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=40)
            if not line:
                raise RuntimeError("mcp stdio server closed")
            try:
                msg = json.loads(line.decode())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                return msg.get("result")

    async def initialize(self):
        r = await self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": _CLIENT_INFO})
        await self._rpc("notifications/initialized", notify=True)
        return r

    async def list_tools(self) -> list[dict]:
        return (await self._rpc("tools/list", {}) or {}).get("tools", [])

    async def call_tool(self, name: str, arguments: dict) -> Any:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _make_client(spec: dict):
    # Tolerate both the flat shape ({"url":...}) and the nested transport
    # shape ({"transport":{"type":"http","url":...}}) various tools emit.
    t = spec.get("transport") if isinstance(spec.get("transport"), dict) else {}
    url = spec.get("url") or t.get("url")
    headers = spec.get("headers") or t.get("headers")
    command = spec.get("command") or t.get("command")
    if url:
        return _HttpMCP(url, headers)
    if command:
        return _StdioMCP(command, spec.get("args") or t.get("args"), spec.get("env") or t.get("env"))
    raise ValueError("server needs a 'url' (remote) or 'command' (stdio)")


def _flatten_content(result: Any) -> Any:
    """MCP tool results are {content: [{type:'text', text:...}], ...}. Flatten
    to something the model reads cleanly."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if isinstance(content, list):
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        if texts:
            joined = "\n".join(texts)
            if result.get("structuredContent"):
                return {"text": joined, "data": result["structuredContent"]}
            return joined
    return result.get("structuredContent", result)


# ─── connect + register ─────────────────────────────────────────────────


_CLIENTS: dict[str, Any] = {}


async def connect_all(registry: ToolRegistry, config: SundayConfig) -> dict[str, Any]:
    """Connect every configured server and register its tools. Idempotent —
    re-running replaces the prior registrations."""
    servers = (load_config().get("mcpServers") or {})
    STATUS.clear()
    for sname, spec in servers.items():
        skey = _safe(sname)
        status = {"name": sname, "connected": False, "tools": [], "error": None}
        STATUS[sname] = status
        try:
            client = _make_client(spec)
            await client.initialize()
            tools = await client.list_tools()
            _CLIENTS[skey] = client
            for t in tools:
                tname = t.get("name")
                if not tname:
                    continue
                full = f"{skey}_{_safe(tname)}"
                registry.register(_proxy_tool(full, skey, tname, t))
                status["tools"].append(tname)
            status["connected"] = True
            log.info("mcp connected", server=sname, tools=len(status["tools"]))
        except Exception as exc:  # noqa: BLE001
            status["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("mcp connect failed", server=sname, error=status["error"])
    return STATUS


def _proxy_tool(full_name: str, server_key: str, remote_name: str, schema: dict) -> Tool:
    async def run(args: dict[str, Any], ctx: ToolContext) -> Any:
        client = _CLIENTS.get(server_key)
        if not client:
            return {"error": f"MCP server '{server_key}' not connected"}
        try:
            return _flatten_content(await client.call_tool(remote_name, args))
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{remote_name} failed: {exc}"}
    desc = (schema.get("description") or f"{remote_name} (via MCP)").strip()
    return Tool(
        name=full_name[:64],
        description=desc[:1024],
        parameters=schema.get("inputSchema") or {"type": "object", "properties": {}},
        run=run,
    )


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    """Called by default_registry. Connection is async + happens in a daemon
    background task (see daemon startup) — nothing to register synchronously."""
    return
