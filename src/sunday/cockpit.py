"""Cockpit — the bridge to the user's REAL logged-in Chrome.

Cockpit is a Chrome MV3 extension the user installs in their everyday browser.
It pairs with Sunday the same way the Playwright extension does: the extension
displays a token, the user pastes it into Settings → Tools → Cockpit, and the
extension connects OUT to this daemon over a WebSocket. From then on, the brain
can perceive and drive the user's actual on-screen tabs — logged in as them, so
private docs, dashboards, and signed-in web apps just work.

Topology (the daemon is the SERVER, the extension is the CLIENT):

    extension  ──ws──▶  ws://127.0.0.1:<port>/v1/cockpit/ws?token=<COCKPIT_TOKEN>

Frozen wire protocol (the extension is built against this verbatim):
  - JSON text frames.
  - Daemon → extension request:  {id, method, params}
  - Extension → daemon reply:    {id, result}  or  {id, error}
  - Extension → daemon push:     {event: 'status'|'user_done', ...}

Auth: the connection's ?token= must match the stored COCKPIT_TOKEN credential
(constant-time compare). The /v1/cockpit/ws route is exempt from the normal
bearer middleware — the extension can't carry the daemon's bearer — so the
token check INSIDE the handler is the authentication, mirroring /v1/ws.

CockpitBridge holds the single live extension socket (latest connection wins),
maps in-flight request ids to futures, and exposes async request() that raises
clear errors on no-connection / timeout. Tools are thin wrappers over it.
"""

from __future__ import annotations

import asyncio
import hmac
import uuid
from typing import Any

import structlog
from aiohttp import web

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.cockpit")

# instruct_user hands the wheel to a human (login, OTP, consent, payment) and
# only resolves once they confirm — it gets a long leash. Everything else is a
# mechanical browser action that should come back quickly.
_INSTRUCT_TIMEOUT = 300.0
_DEFAULT_TIMEOUT = 30.0

_NOT_CONNECTED_MSG = (
    "Cockpit isn't connected — open Settings → Tools → Cockpit, then make sure "
    "the Cockpit extension is running in your Chrome (it connects automatically "
    "once the token is paired)."
)


class CockpitNotConnected(RuntimeError):
    """Raised when a tool fires but no extension socket is live."""


class CockpitBridge:
    """Daemon-side endpoint for the Cockpit extension's WebSocket.

    Single live socket: a new connection replaces (and closes) the old one,
    so reloading the extension or re-pairing never leaves a zombie behind.
    """

    def __init__(self) -> None:
        self._ws: web.WebSocketResponse | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # Set by the daemon at startup: async callable(text) -> reply string.
        # Lets the extension's side-panel chat talk to Sunday over the same
        # paired socket — no second auth surface, no app ping-pong while
        # Sunday is driving the browser.
        self.say_handler = None
        # When an extension knocked with the wrong/no token (monotonic time).
        # Surfaced via /v1/cockpit/status so the Settings card can say "an
        # extension is dialing with a DIFFERENT token — re-copy it from the
        # popup" instead of leaving the mismatch silent (tokens regenerate on
        # extension reinstall, so this happens to real users).
        self.last_reject: float = 0.0

    # ── connection state ────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        ws = self._ws
        return ws is not None and not ws.closed

    def paired(self) -> bool:
        """A COCKPIT_TOKEN credential is set (the user pasted the token)."""
        return bool(get_credential("COCKPIT_TOKEN"))

    def _set_socket(self, ws: web.WebSocketResponse) -> None:
        """Adopt a new socket as THE socket; drop any earlier one."""
        old = self._ws
        self._ws = ws
        if old is not None and old is not ws and not old.closed:
            # Fail in-flight requests bound to the old socket, then close it.
            self._fail_all(CockpitNotConnected("replaced by a newer Cockpit connection"))
            asyncio.create_task(self._safe_close(old))

    async def _safe_close(self, ws: web.WebSocketResponse) -> None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001 — closing a stale socket must never throw
            pass

    def _clear_socket(self, ws: web.WebSocketResponse) -> None:
        if self._ws is ws:
            self._ws = None
            self._fail_all(CockpitNotConnected("Cockpit disconnected"))

    def _fail_all(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(exc)

    # ── request / response ──────────────────────────────────────────────────

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> Any:
        """Send {id, method, params} to the extension and await its reply.

        Raises CockpitNotConnected when no socket is live, RuntimeError on a
        timeout or an error reply from the extension.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise CockpitNotConnected(_NOT_CONNECTED_MSG)

        req_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        try:
            await ws.send_json({"id": req_id, "method": method, "params": params or {}})
        except (ConnectionError, RuntimeError, ValueError) as exc:
            self._pending.pop(req_id, None)
            raise CockpitNotConnected(f"sending {method} to Cockpit failed: {exc}") from exc

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise RuntimeError(
                f"Cockpit timed out after {int(timeout)}s waiting for '{method}'. "
                "The extension may be busy, the tab may be loading, or "
                "(for instruct_user) the user may not have confirmed yet."
            )

    def _resolve(self, payload: dict[str, Any]) -> None:
        """Match a reply frame {id, result|error} to its pending future."""
        req_id = payload.get("id")
        if req_id is None:
            return
        fut = self._pending.pop(req_id, None)
        if fut is None or fut.done():
            return
        if payload.get("error"):
            fut.set_exception(RuntimeError(str(payload["error"])))
        else:
            fut.set_result(payload.get("result"))

    # ── server-side WS handler ──────────────────────────────────────────────

    async def _handle_say(self, ws: web.WebSocketResponse, text: str) -> None:
        """Side-panel chat: run a normal Sunday turn and push the reply back
        over the socket. Protocol v1.1: {event:'say'} in, {event:'thinking'}
        then {event:'reply', text} out."""
        try:
            await ws.send_json({"event": "thinking"})
        except Exception:  # noqa: BLE001 — socket died; the turn is pointless
            return
        try:
            reply = await self.say_handler(text)
        except Exception as exc:  # noqa: BLE001
            reply = f"that didn't work: {exc}"
        try:
            await ws.send_json({"event": "reply", "text": reply or ""})
        except Exception:  # noqa: BLE001
            log.warning("cockpit: reply push failed (socket closed mid-turn)")

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Accept the extension's outbound connection.

        Auth is the ?token= query param vs the stored COCKPIT_TOKEN (the route
        is exempt from the bearer middleware). On mismatch we close with 401
        semantics. Latest connection wins.
        """
        import time as _time
        token = request.query.get("token", "")
        expected = get_credential("COCKPIT_TOKEN") or ""
        if not expected:
            # Not paired yet — nothing to authenticate against. Refuse rather
            # than accept an unauthenticated controller of the user's browser.
            self.last_reject = _time.monotonic()
            return web.json_response(
                {"error": "Cockpit is not paired — set a token in Settings first."},
                status=401,
            )
        if not token or not hmac.compare_digest(token, expected):
            self.last_reject = _time.monotonic()
            log.warning("cockpit: rejected handshake (token mismatch — extension reinstalled?)")
            return web.json_response({"error": "unauthorized"}, status=401)

        ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=32 * 1024 * 1024)
        await ws.prepare(request)
        self._set_socket(ws)
        log.info("cockpit extension connected", remote=request.remote)

        try:
            async for msg in ws:
                if msg.type != web.WSMsgType.TEXT:
                    continue
                try:
                    payload = msg.json()
                except Exception:  # noqa: BLE001 — ignore malformed frames
                    log.warning("cockpit: invalid JSON frame")
                    continue
                if not isinstance(payload, dict):
                    continue
                # Reply to one of our requests?
                if "id" in payload and ("result" in payload or "error" in payload):
                    self._resolve(payload)
                    continue
                # Async push from the extension (status / user_done / say).
                evt = payload.get("event")
                if evt == "say":
                    text = (payload.get("text") or "").strip()
                    if text and self.say_handler:
                        asyncio.create_task(self._handle_say(ws, text))
                    continue
                if evt:
                    log.debug("cockpit event", evt=evt)
                    continue
        finally:
            self._clear_socket(ws)
            log.info("cockpit extension disconnected")
        return ws


# ── tool wrappers ───────────────────────────────────────────────────────────


def _bridge(ctx: ToolContext) -> CockpitBridge:
    bridge = ctx.extras.get("cockpit")
    if bridge is None:
        raise CockpitNotConnected(
            "Cockpit bridge not attached to this context (running outside the daemon?)."
        )
    return bridge


async def _call(ctx: ToolContext, method: str, params: dict[str, Any], *, timeout: float = _DEFAULT_TIMEOUT) -> Any:
    """Run a Cockpit method, turning bridge errors into clean tool results the
    model can act on rather than raising into the tool-execution boundary."""
    try:
        result = await _bridge(ctx).request(method, params, timeout=timeout)
    except CockpitNotConnected as exc:
        return {"error": str(exc)}
    except RuntimeError as exc:
        return {"error": str(exc)}
    # Pass the extension's structured result straight through; wrap bare values.
    if isinstance(result, (dict, list)) or result is None:
        return result if result is not None else {"ok": True}
    return {"result": result}


async def _t_read_page(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _call(ctx, "read_page", {})


async def _t_click(args: dict[str, Any], ctx: ToolContext) -> Any:
    if "index" not in args:
        return {"error": "'index' is required (an element index from cockpit_read_page)"}
    params: dict[str, Any] = {"index": args["index"]}
    if args.get("reason"):
        params["reason"] = args["reason"]
    return await _call(ctx, "click", params)


async def _t_fill(args: dict[str, Any], ctx: ToolContext) -> Any:
    if "index" not in args:
        return {"error": "'index' is required (an element index from cockpit_read_page)"}
    if "value" not in args:
        return {"error": "'value' is required"}
    params: dict[str, Any] = {"index": args["index"], "value": str(args["value"])}
    if args.get("submit") is not None:
        params["submit"] = bool(args["submit"])
    return await _call(ctx, "fill", params)


async def _t_press_key(args: dict[str, Any], ctx: ToolContext) -> Any:
    key = (args.get("key") or "").strip()
    if not key:
        return {"error": "'key' is required (e.g. Enter, Tab, Escape, ArrowDown)"}
    params: dict[str, Any] = {"key": key}
    if args.get("index") is not None:
        params["index"] = args["index"]
    return await _call(ctx, "press_key", params)


async def _t_scroll(args: dict[str, Any], ctx: ToolContext) -> Any:
    params: dict[str, Any] = {}
    if args.get("to"):
        params["to"] = args["to"]
    if args.get("index") is not None:
        params["index"] = args["index"]
    return await _call(ctx, "scroll", params)


async def _t_navigate(args: dict[str, Any], ctx: ToolContext) -> Any:
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "'url' is required (absolute, including https://)"}
    return await _call(ctx, "navigate", {"url": url})


async def _t_tabs(args: dict[str, Any], ctx: ToolContext) -> Any:
    action = (args.get("action") or "").strip().lower()
    if action in ("", "list"):
        return await _call(ctx, "list_tabs", {})
    if action == "open":
        url = (args.get("url") or "").strip()
        if not url:
            return {"error": "action 'open' needs a 'url'"}
        params: dict[str, Any] = {"url": url}
        if args.get("focus") is not None:
            params["focus"] = bool(args["focus"])
        return await _call(ctx, "open_tab", params)
    if action == "switch":
        if args.get("tab_id") is None:
            return {"error": "action 'switch' needs a 'tab_id' (from cockpit_tabs list)"}
        return await _call(ctx, "switch_tab", {"tab_id": args["tab_id"]})
    if action == "close":
        if args.get("tab_id") is None:
            return {"error": "action 'close' needs a 'tab_id' (from cockpit_tabs list)"}
        return await _call(ctx, "close_tab", {"tab_id": args["tab_id"]})
    return {"error": f"unknown action '{action}' (use list / open / switch / close)"}


async def _t_screenshot(args: dict[str, Any], ctx: ToolContext) -> Any:
    return await _call(ctx, "screenshot", {})


async def _t_highlight(args: dict[str, Any], ctx: ToolContext) -> Any:
    indexes = args.get("indexes")
    if not isinstance(indexes, list) or not indexes:
        return {"error": "'indexes' is required (a list of element indexes from cockpit_read_page)"}
    params: dict[str, Any] = {"indexes": indexes}
    if args.get("note"):
        params["note"] = args["note"]
    return await _call(ctx, "highlight", params)


async def _t_instruct_user(args: dict[str, Any], ctx: ToolContext) -> Any:
    message = (args.get("message") or "").strip()
    if not message:
        return {"error": "'message' is required — tell the user exactly what to do and why"}
    params: dict[str, Any] = {"message": message}
    if isinstance(args.get("indexes"), list) and args["indexes"]:
        params["indexes"] = args["indexes"]
    return await _call(ctx, "instruct_user", params, timeout=_INSTRUCT_TIMEOUT)


# ── registration ────────────────────────────────────────────────────────────

_DRIVES_REAL_CHROME = (
    "This drives the user's REAL, logged-in Chrome via the Cockpit extension — "
    "their actual on-screen tabs, signed in as them. "
)


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="cockpit_read_page",
        description=(
            _DRIVES_REAL_CHROME
            + "Perceive the CURRENT tab: returns the URL, title, a numbered list of "
            "interactive elements (each like `[7] <button> \"Sign in\"`), scroll "
            "position, and the visible text. ALWAYS call this after navigating or "
            "after any action that may change the page, BEFORE using any element "
            "index — indexes are only valid until the page changes."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        run=_t_read_page,
    ))
    registry.register(Tool(
        name="cockpit_click",
        description=(
            _DRIVES_REAL_CHROME
            + "Click an interactive element by its index from the latest "
            "cockpit_read_page. Never click a login, consent, or payment button "
            "on the user's behalf — hand those off with cockpit_instruct_user."
        ),
        parameters={"type": "object", "properties": {
            "index": {"type": "integer", "description": "Element index from cockpit_read_page."},
            "reason": {"type": "string", "description": "Short reason, shown to the user."},
        }, "required": ["index"]},
        run=_t_click,
    ))
    registry.register(Tool(
        name="cockpit_fill",
        description=(
            _DRIVES_REAL_CHROME
            + "Type a value into an input, textarea, contenteditable, select, "
            "checkbox or radio by index (from cockpit_read_page). Set submit=true "
            "to press Enter after. NEVER use this for passwords, one-time codes, "
            "or payment details — use cockpit_instruct_user for those."
        ),
        parameters={"type": "object", "properties": {
            "index": {"type": "integer", "description": "Element index from cockpit_read_page."},
            "value": {"type": "string", "description": "Text to enter (or 'true'/'false' for checkboxes)."},
            "submit": {"type": "boolean", "description": "Press Enter / submit the form after filling."},
        }, "required": ["index", "value"]},
        run=_t_fill,
    ))
    registry.register(Tool(
        name="cockpit_press_key",
        description=(
            _DRIVES_REAL_CHROME
            + "Press a keyboard key in the current tab, optionally after focusing "
            "an element by index. Useful for Enter, Tab, Escape, ArrowDown/Up "
            "(dropdowns/autocomplete), PageDown, Backspace."
        ),
        parameters={"type": "object", "properties": {
            "key": {"type": "string", "description": "Key name: Enter, Tab, Escape, ArrowDown, ArrowUp, Backspace, PageDown…"},
            "index": {"type": "integer", "description": "Optional element index to focus first."},
        }, "required": ["key"]},
        run=_t_press_key,
    ))
    registry.register(Tool(
        name="cockpit_scroll",
        description=(
            _DRIVES_REAL_CHROME
            + "Scroll the current tab. Give a direction (top/bottom/up/down) or an "
            "element index to bring into view."
        ),
        parameters={"type": "object", "properties": {
            "to": {"type": "string", "enum": ["top", "bottom", "up", "down"], "description": "Direction to scroll."},
            "index": {"type": "integer", "description": "Optional element index to scroll into view."},
        }, "required": []},
        run=_t_scroll,
    ))
    registry.register(Tool(
        name="cockpit_navigate",
        description=(
            _DRIVES_REAL_CHROME
            + "Navigate the current tab to a URL and wait for load. Because the "
            "browser is logged in as the user, private docs, dashboards, and "
            "signed-in apps open directly. Returns the new page perception."
        ),
        parameters={"type": "object", "properties": {
            "url": {"type": "string", "description": "Absolute URL including https://"},
        }, "required": ["url"]},
        run=_t_navigate,
    ))
    registry.register(Tool(
        name="cockpit_tabs",
        description=(
            _DRIVES_REAL_CHROME
            + "Manage the user's open tabs. action='list' returns each tab "
            "(id, title, url, active). action='open' opens a new tab (url, "
            "optional focus). action='switch' makes an existing tab the working "
            "tab (tab_id). action='close' closes a tab (tab_id) — don't close the "
            "user's main tab without telling them."
        ),
        parameters={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["list", "open", "switch", "close"], "description": "What to do."},
            "url": {"type": "string", "description": "URL to open (action='open')."},
            "focus": {"type": "boolean", "description": "Make the opened tab the working tab (default true)."},
            "tab_id": {"type": "integer", "description": "Tab id from action='list' (for switch/close)."},
        }, "required": ["action"]},
        run=_t_tabs,
    ))
    registry.register(Tool(
        name="cockpit_screenshot",
        description=(
            _DRIVES_REAL_CHROME
            + "Take a screenshot of the current tab so you can SEE it visually — "
            "for canvas/map/visual UIs, image-heavy pages, drag-and-drop, or to "
            "verify an action worked when the DOM text is ambiguous."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        run=_t_screenshot,
    ))
    registry.register(Tool(
        name="cockpit_highlight",
        description=(
            _DRIVES_REAL_CHROME
            + "Draw attention to one or more elements with a pulsing box (and an "
            "optional short note) WITHOUT pausing — point things out while you "
            "keep working or explaining."
        ),
        parameters={"type": "object", "properties": {
            "indexes": {"type": "array", "items": {"type": "integer"}, "description": "Element indexes (from cockpit_read_page) to highlight."},
            "note": {"type": "string", "description": "Optional short note shown beside the first element."},
        }, "required": ["indexes"]},
        run=_t_highlight,
    ))
    registry.register(Tool(
        name="cockpit_instruct_user",
        description=(
            _DRIVES_REAL_CHROME
            + "THE SAFE HANDOFF. Pause and hand control to the human for anything "
            "only they can or should do: logging in, entering a password / 2FA / "
            "one-time code, approving an OAuth consent or permission screen, "
            "making a payment, or any irreversible or personal-judgment step. "
            "NEVER attempt those yourself with click/fill — highlight the element "
            "and instruct the user here instead. Explain clearly what to do and "
            "why; this resolves once the user confirms they're done (waits up to "
            "5 minutes)."
        ),
        parameters={"type": "object", "properties": {
            "message": {"type": "string", "description": "Clear, specific instruction for the user."},
            "indexes": {"type": "array", "items": {"type": "integer"}, "description": "Element index(es) to highlight, if applicable."},
        }, "required": ["message"]},
        run=_t_instruct_user,
    ))
