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
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import structlog
from aiohttp import web

from sunday import __version__
from sunday.brain import respond
from sunday.chat import Chat
from sunday.cockpit import CockpitBridge
from sunday.config import load_config
from sunday.devices.manager import DeviceManager
from sunday.ipc import IpcError, read_json, write_json
from sunday.memory import Memory, extract_facts
from sunday.paths import auth_token_path, ensure_home, socket_path
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


# ── Authentication ──────────────────────────────────────────────────────
# Single shared bearer token stored at ~/.sunday/auth.token. Generated on
# first daemon start (256 bits of os.urandom, base64). Sunday.app reads it
# from the same file when running against a local daemon, or from the user's
# saved prefs when pointing at a remote one.

_AUTH_TOKEN_CACHE: str | None = None

# Extract durable facts every N exchanges (batched) rather than once per turn.
_EXTRACT_EVERY = 3


def _auth_token_path() -> Path:
    return auth_token_path()


def get_or_create_auth_token() -> str:
    """Read the bearer token from disk; generate + persist one if missing.

    When the desktop app spawns the embedded daemon it passes the token it will
    use via the SUNDAY_AUTH_TOKEN env var — that takes precedence so the app and
    daemon can never disagree (the old file-only path raced: the daemon could
    cache a token the app never managed to read). We still persist it so a
    satellite or a later read picks it up."""
    import os
    global _AUTH_TOKEN_CACHE
    if _AUTH_TOKEN_CACHE:
        return _AUTH_TOKEN_CACHE
    p = _auth_token_path()
    env_tok = (os.environ.get("SUNDAY_AUTH_TOKEN") or "").strip()
    if env_tok:
        _AUTH_TOKEN_CACHE = env_tok
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(env_tok, encoding="utf-8")
            p.chmod(0o600)
        except Exception:  # noqa: BLE001
            pass
        return env_tok
    if p.exists():
        tok = p.read_text(encoding="utf-8").strip()
        if tok:
            _AUTH_TOKEN_CACHE = tok
            return tok
    import secrets
    tok = "sunday_" + secrets.token_urlsafe(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(tok, encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass
    _AUTH_TOKEN_CACHE = tok
    log.info("auth token generated", path=str(p))
    return tok


# Paths that intentionally remain unauthenticated:
#   /webhooks/*       — external services (Sendblue, Composio) POST here
#                        and have no way to carry our bearer token
#   /v1/health        — load balancer / uptime probes
#   /v1/auth/check    — for the desktop app to verify a token is valid
#   /v1/ws            — browser WebSocket; the handshake can't set an
#                        Authorization header, so _ws_handler enforces the
#                        token via the ?token= query param instead.
#
# /v1/devices/ws is deliberately NOT exempt. A satellite is a real client
# (the `websockets` lib, not a browser) and CAN set Authorization on the
# handshake — it already sends `Bearer <token>`. Letting it ride the normal
# middleware means an unauthenticated peer can't register a device, hijack a
# device_id, or feed the brain forged tool results over a public daemon.
# /v1/cockpit/ws is exempt for the same reason as /v1/ws: the Cockpit browser
# extension can't set our Authorization header on the WS handshake, so the
# handler enforces the COCKPIT_TOKEN credential via the ?token= query param.
_AUTH_EXEMPT_PREFIXES = ("/webhooks/", "/v1/health", "/v1/auth/check", "/v1/ws", "/v1/cockpit/ws", "/v1/cockpit/precheck")


# Conversational off-switch for proactive check-ins. Deliberately narrow — it
# must catch a clear "stop reaching out" instruction without misfiring on the
# user simply *checking in* about something. Requires both a stop/quiet verb
# AND a reference to the check-in/pinging/reaching-out behavior.
_STOP_VERBS = ("stop", "don't", "dont", "quit", "no more", "cut", "knock it off",
               "turn off", "disable", "stop with", "enough with", "lay off", "ease off")
_CHECKIN_NOUNS = ("check in", "check-in", "checking in", "checkin", "reaching out",
                  "reach out", "messaging me", "pinging me", "texting me first",
                  "messaging me first", "the pings", "unprompted")


def _wants_to_stop_checkins(text: str) -> bool:
    t = (text or "").lower()
    if not any(n in t for n in _CHECKIN_NOUNS):
        return False
    return any(v in t for v in _STOP_VERBS)


@web.middleware
async def _auth_middleware(request: web.Request, handler):
    """Reject any request without a valid bearer token. Covers ALL `/v1/*`
    routes that aren't explicitly exempted above. CORS preflights pass."""
    path = request.path or ""
    if request.method == "OPTIONS":
        return await handler(request)
    if any(path.startswith(pfx) for pfx in _AUTH_EXEMPT_PREFIXES):
        return await handler(request)
    expected = get_or_create_auth_token()
    auth = request.headers.get("Authorization") or ""
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # Constant-time comparison so timing leaks don't reveal the token.
    import hmac
    if not presented or not hmac.compare_digest(presented, expected):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)



# Stop-words so "watching a video about X" vs "video about X" still match.
_NOW_STOP = {"a", "an", "the", "to", "of", "on", "in", "is", "and", "with",
             "about", "watching", "listening", "reading", "discussing", "talking"}


def _now_similar(a: str, b: str) -> bool:
    """Are two 'now' lines the same activity, modulo phrasing drift? Word-set
    Jaccard over content words; >0.4 overlap counts as continuation. Cheap and
    good enough to stop the every-tick churn for one continuous activity."""
    def words(s: str) -> set[str]:
        return {w for w in "".join(c if c.isalnum() else " " for c in s.lower()).split()
                if len(w) > 2 and w not in _NOW_STOP}
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    inter = len(wa & wb)
    union = len(wa | wb)
    return union > 0 and (inter / union) >= 0.4


class TurnControl:
    """Lets the user grab the wheel on a running task. brain.respond() checks
    this at each step boundary: pending steering messages get folded into the
    chat (the model sees them next call), and a stop request ends the loop."""

    def __init__(self) -> None:
        self._stop = False
        self._steer: list[str] = []

    def stop(self) -> None:
        self._stop = True

    def should_stop(self) -> bool:
        return self._stop

    def steer(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return False
        self._steer.append(t)
        return True

    def drain_steering(self) -> list[str]:
        out, self._steer = self._steer, []
        return out


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
        # Build the LLM runtime ONCE and reuse it across every turn — keeps the
        # HTTP connection to the provider warm (keep-alive) instead of doing a
        # fresh TCP+TLS handshake + client construction on every message.
        from sunday.runtime import build_runtime
        self.runtime = build_runtime(self.config)
        # Tools find_tools has activated this session (beyond the core set).
        self._active_tools: set[str] = set()
        self.devices = DeviceManager(broadcast=self._broadcast_lazy)
        # The brain runs on the user's own machine, so register it as an
        # in-process "local" device — shell commands execute right here, no
        # satellite WebSocket required. This is why device_run_command works the
        # moment the daemon is up, instead of erroring "no device — start the
        # satellite" whenever the satellite's WS link has dropped (it silently
        # does on restarts). Satellites on OTHER machines still connect over
        # /v1/devices/ws and add their own capabilities (screen, control, …).
        if sys.platform in ("darwin", "linux"):
            import platform as _platform
            from sunday.devices import local as _local
            self.devices.register_local(
                device_id="local",
                capabilities=_local.CAPABILITIES,
                platform=_platform.platform(),
                handlers=_local.HANDLERS,
            )
        # Bridge to the user's real logged-in Chrome via the Cockpit extension.
        # Holds the single live extension WS; tools route through it.
        self.cockpit = CockpitBridge()

        async def _cockpit_say(text: str) -> str:
            # Side-panel chat rides the normal turn path (turn lock, memory,
            # broadcast) — the reply ALSO lands in the app via _broadcast, so
            # the two surfaces never diverge.
            out = await self._say(text, "cockpit", None)
            return (out or {}).get("reply") or ""
        self.cockpit.say_handler = _cockpit_say
        self._unix_server: asyncio.Server | None = None
        self._http_runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
        # One-click Codex sign-in: in-flight OAuth state + the temporary
        # localhost:1455 callback server that catches OpenAI's redirect.
        self._codex_login: dict[str, Any] | None = None
        self._codex_cb_runner: web.AppRunner | None = None
        # Buffered (user, reply) exchanges awaiting batched fact extraction.
        self._extract_buf: list[tuple[str, str]] = []
        self._stop = asyncio.Event()
        self._started_at = time.time()
        # "What is the user doing right now" — pushed in by the observer (the
        # tick worker on the user's Mac). Surfaced in /v1/status and the notch
        # HUD. since = unix ts the current activity started; updated_at = last
        # observer ping (used to detect staleness so a dead observer doesn't
        # leave a frozen "now" line on the hub).
        self._now: dict[str, Any] = {"now": None, "since": None, "updated_at": None}
        # Atom store — structured open threads/commitments/decisions surfaced
        # by the observer (or created by the agent / user). Lifecycle state.
        from sunday.atoms import AtomStore
        self.atoms = AtomStore()
        # Conversation store — audio-originated, OMI-style segmentation. The
        # observer closes a conversation on 2-min silence, fires a structured
        # summary, and posts it here; atoms born during the window get linked.
        from sunday.conversations import ConversationStore
        self.conversations = ConversationStore()
        # Activity store — the unified Inbox's local source of truth (spec §4b).
        # Inbound events (calls, texts, emails, webhooks) normalize into one
        # table here, so /v1/inbox reads SQLite instead of three provider APIs.
        # Voice still live-fetches VAPI for now; store-backed channels read here.
        from sunday.activity import ActivityStore
        self.activity = ActivityStore()
        # Interjections — Sunday's proactive notes + (later) nudges. Shared
        # substrate; the consumer is the notch flare. Engagement converts to
        # a real chat message; dismissal extends cooldown.
        from sunday.interjections import InterjectionStore
        self.interjections = InterjectionStore()
        # Observer conversation buffer — transcripts accumulate here across
        # ticks; on a run of silent ticks we close + summarize the window.
        # (Capture happens in Sunday.app on the Mac; transcripts arrive via
        # /v1/observer/tick.)
        # Meeting recording state — set by the Mac app while a meeting is
        # being recorded; surfaced in /v1/status so the notch HUD can show
        # the timer + red dot.
        self._meeting: dict[str, Any] = {"recording": False, "since": None}
        self._obs_buffer: list[tuple[float, str]] = []   # (ts, transcript)
        self._obs_silent_streak: int = 0
        self._obs_conv_started: float | None = None
        # Close triggers, in priority order:
        #   1. Activity shift — `now` changes to something genuinely different
        #      (the natural break — "stopped doing X, started doing Y")
        #   2. Silence — 4 ticks (~2 min) of nothing
        #   3. Safety nets — 20 min wall-clock or 30k chars
        # The activity-shift trigger is the important one. Without it the
        # buffer accumulates a continuous "open conversation" until silence,
        # which never happens during a normal day.
        self._obs_silent_to_close = 4
        self._obs_shift_min_age = 60               # don't shift-close a freshly-started conv
        self._obs_conv_max_seconds = 20 * 60
        self._obs_conv_max_chars = 30_000
        # Serializes agent turns (user-initiated + sub-agent wake turns) so the
        # single chat log never interleaves two concurrent respond() loops.
        self._turn_lock = asyncio.Lock()
        # The control handle for the turn currently running (None when idle).
        # Lets the user steer or stop a task mid-flight — see TurnControl.
        self._active_control: TurnControl | None = None

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
        thread_id: int | None = None,
    ) -> dict[str, Any]:
        # If a MAIN-timeline task is running, a new main message isn't a queued
        # turn — it's the user grabbing the wheel; fold it in as steering. A
        # thread post is a deliberate side-bar reply, never steering, so it
        # always waits for the lock and runs as its own scoped turn.
        if thread_id is None and self._turn_lock.locked() and self._active_control is not None:
            self._active_control.steer(text)
            return {"steered": True, "reply": None}
        # Conversational off-switch: if the user tells Sunday to stop checking
        # in, honor it immediately (the model still replies normally on this
        # turn — this just flips the persisted flag). Err toward easy-to-silence.
        if _wants_to_stop_checkins(text):
            self.pause_checkins(reason="user said so in chat")
        # Serialize turns: a user turn and a sub-agent wake turn must not run
        # concurrently or they'd interleave messages in the single chat.
        async with self._turn_lock:
            reply = await self._run_turn(text, modality, attachments=attachments, thread_id=thread_id)
        return {"reply": reply, "thread_id": thread_id}

    async def _run_turn(
        self,
        text: str,
        modality: str,
        attachments: list[dict] | None = None,
        user_metadata: dict[str, Any] | None = None,
        thread_id: int | None = None,
    ) -> str:
        """One agent turn: drive the loop, broadcast the reply, kick off the
        background memory + compaction passes. Caller MUST hold _turn_lock.

        `thread_id` scopes the turn to a Slack-style thread — the messages land
        in that thread and the brain's context is the thread, not the main chat.
        Main-only background work (rolling summary, fact extraction) is skipped
        for thread turns since the summary describes the main timeline."""
        control = TurnControl()
        self._active_control = control
        from sunday import obs
        _trace, _tok = obs.start_turn("chat_turn", session=getattr(self.chat, "key", None), user=getattr(self.config, "user_id", None))
        try:
            reply = await respond(
                self.chat, text, modality, self.config, self.registry,
                runtime=self.runtime,
                attachments=attachments,
                user_metadata=user_metadata,
                thread_id=thread_id,
                extras={
                    "broadcast": self._broadcast,
                    "devices":   self.devices,
                    "cockpit":   self.cockpit,
                    # the daemon itself — tools that surface async results back
                    # into the conversation (e.g. call_phone's outcome poller)
                    # use this to reach self.chat / append the end-of-call report.
                    "daemon":    self,
                    "memory":    self.memory,
                    "runtime":       self.runtime,
                    # tiered tools: lean core + whatever find_tools has pulled in
                    # this session (persists across turns until the daemon restarts)
                    "registry":      self.registry,
                    "active_tools":  self._active_tools,
                    # async sub-agents: tools hand a finished result back here to
                    # be injected as a hidden message + folded in on a wake turn.
                    "inject_and_wake": self._inject_and_wake,
                    # steer/stop handle — the user can grab the wheel mid-task.
                    "control":       control,
                },
            )
        except Exception as _turn_exc:
            obs.end_turn(_trace, _tok, error=str(_turn_exc))
            raise
        finally:
            self._active_control = None
        obs.end_turn(_trace, _tok)
        await self._broadcast({"type": "reply", "modality": modality, "content": reply, "thread_id": thread_id})
        # Thread turns are scoped side-bars: they don't feed the main-timeline
        # rolling summary or fact extraction (both describe the main chat), so
        # skip the background passes entirely for them.
        if thread_id is not None:
            return reply
        # Buffer this exchange; extract facts in batches (every few turns) on the
        # cheap utility model instead of one full-model call per turn.
        if self.memory.available:
            self._extract_buf.append((text, reply))
            if len(self._extract_buf) >= _EXTRACT_EVERY:
                asyncio.create_task(self._flush_extract())
        # Fire-and-forget conversation compaction — folds messages that have
        # aged out of the live tail into the rolling summary. No-ops until a
        # real batch has accumulated, so it's cheap to call every turn.
        asyncio.create_task(self._compact())
        return reply

    async def _inject_and_wake(self, text: str) -> None:
        """Inject a model-only message (e.g. a finished sub-agent's result)
        and run a fresh turn so the agent folds it in and messages the user —
        the async-delegation 'wake'. Hidden from the UI; the model sees it as
        ordinary user input. Waits for any in-flight turn to finish first."""
        async with self._turn_lock:
            await self._run_turn(
                text, "agent",
                user_metadata={"hidden": True, "kind": "subagent_result"},
            )

    async def _compact(self) -> None:
        # Flush any buffered exchanges first so trailing turns aren't stranded
        # when the conversation goes quiet (compaction fires on a natural cadence).
        if self.memory.available and self._extract_buf:
            await self._flush_extract()
        try:
            from sunday.compaction import maybe_compact
            await maybe_compact(self.chat, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("compaction task failed", error=str(exc))

    async def _flush_extract(self) -> None:
        """Extract durable facts from the buffered exchanges in one cheap call."""
        if not self._extract_buf:
            return
        batch, self._extract_buf = self._extract_buf, []
        try:
            facts = await extract_facts(batch, self.config)
            if facts:
                await self.memory.store_many(facts, source="auto")
                log.info("memory extracted", new_facts=len(facts), from_turns=len(batch))
                # Graph refresh piggybacks compaction (incremental ingest) — no
                # extra LLM call here.
        except Exception as exc:  # noqa: BLE001
            log.warning("memory extraction task failed", error=str(exc))
            # don't lose the batch on a transient failure — requeue it
            self._extract_buf = batch + self._extract_buf

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

        if method == "clear":
            n = self.chat.clear()
            try:
                from sunday.compaction import reset_state
                reset_state()
            except Exception:  # noqa: BLE001
                log.warning("compaction reset failed during clear")
            await self._broadcast({"type": "cleared"})
            return {"ok": True, "removed": n}

        if method == "stop_task":
            if self._active_control is None:
                return {"ok": False, "error": "no task running"}
            self._active_control.stop()
            return {"ok": True}

        if method == "steer":
            text = (params.get("text") or "").strip()
            if not text:
                raise IpcError("'text' is required")
            if self._active_control is None:
                return await self._say(text, params.get("modality") or "cli")
            self._active_control.steer(text)
            return {"ok": True, "steered": True}

        if method == "log":
            limit = int(params.get("limit") or 20)
            # Hide model-only injected messages (e.g. sub-agent results) from
            # the UI log — the model sees them, the user shouldn't.
            return {"messages": [m.to_json() for m in self.chat.recent(limit=limit)
                                 if not (m.metadata or {}).get("hidden")]}

        if method == "status":
            try:
                from sunday.subagents.native import active_agents
                agents = active_agents()
            except Exception:  # noqa: BLE001
                agents = []
            # Surface the observer's "what's the user doing right now" if it's
            # fresh — older than ~2 minutes is treated as stale (observer dead
            # or paused), so the hub falls back to its default.
            now_text, now_since = None, None
            if self._now.get("updated_at") and time.time() - self._now["updated_at"] < 120:
                now_text = self._now.get("now")
                now_since = self._now.get("since")
            return {
                "version": __version__,
                "model": f"{self.config.model.provider}/{self.config.model.name}",
                "messages": self.chat.count(),
                "memories": self.memory.count() if self.memory.available else None,
                "tools": self.registry.names(),
                "devices": self.devices.list_devices(),
                "agents": agents,
                "meeting": self._meeting,
                "now": now_text,
                "since": now_since,
                "atoms_open": self.atoms.count_working(),
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

    async def _http_message_edit(self, request: web.Request) -> web.Response:
        """Edit a previous USER message and rewind: drop that message and
        everything after it, then re-run the turn from the edited text — the
        ChatGPT/Claude "edit and branch" gesture. Only user messages; Sunday's
        own replies aren't editable."""
        body = await request.json()
        try:
            message_id = int(body.get("message_id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "'message_id' is required"}, status=400)
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments")
        if not text and not attachments:
            return web.json_response({"error": "'text' or 'attachments' is required"}, status=400)

        msg = self.chat.get(message_id)
        if msg is None:
            return web.json_response({"error": "no such message"}, status=404)
        if msg.role != "user":
            return web.json_response({"error": "only your own messages can be edited"}, status=400)

        # Abort anything in flight so the rewind isn't racing a live turn.
        if self._active_control is not None:
            self._active_control.stop()

        removed = self.chat.truncate_from(message_id)
        # Keep the rolling summary honest: if we cut at or before the last
        # message folded into it, the summary now describes a branch that
        # didn't happen — reset it (it rebuilds from the new tail over time).
        try:
            from sunday.compaction import load_state, reset_state
            if message_id <= int(load_state().get("through_id") or 0):
                reset_state()
        except Exception:  # noqa: BLE001
            log.warning("compaction summary reset failed during edit")

        await self._broadcast({"type": "rewound", "from_id": message_id, "removed": removed})

        # Preserve the original message's attachments unless the edit supplies
        # its own (text-only edits keep the images).
        atts = attachments if isinstance(attachments, list) else (msg.metadata or {}).get("attachments")
        try:
            result = await self._say(text, msg.modality or "electron", atts if isinstance(atts, list) else None)
        except Exception as exc:  # noqa: BLE001
            log.exception("http message edit failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "removed": removed, **result})

    async def _http_task_stop(self, request: web.Request) -> web.Response:
        """Stop the task running right now (cleanly, at the next step boundary).
        No-op if nothing is running."""
        if self._active_control is None:
            return web.json_response({"ok": False, "error": "no task running"})
        self._active_control.stop()
        return web.json_response({"ok": True})

    async def _http_task_steer(self, request: web.Request) -> web.Response:
        """Steer the running task — fold a message into the live loop. If
        nothing is running, it just becomes a normal turn."""
        body = await request.json() if request.body_exists else {}
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "'text' is required"}, status=400)
        if self._active_control is None:
            return web.json_response(await self._say(text, body.get("modality") or "chat"))
        self._active_control.steer(text)
        return web.json_response({"ok": True, "steered": True})

    async def _http_export(self, request: web.Request) -> web.Response:
        """Hand off this daemon's data so it can be migrated to another machine
        (e.g. cloud -> local to use Codex). Without ?file: list the available
        DBs. With ?file=<name>: stream that DB (WAL-checkpointed for a
        consistent copy). Auth-gated by the middleware."""
        import sqlite3
        from sunday.paths import sunday_home
        allowed = ["sunday.db", "memories.db", "atoms.db", "conversations.db", "interjections.db"]
        home = sunday_home()
        name = request.query.get("file", "")
        if not name:
            return web.json_response({"files": [n for n in allowed if (home / n).exists()]})
        if name not in allowed:
            return web.json_response({"error": "not exportable"}, status=400)
        p = home / name
        if not p.exists():
            return web.json_response({"error": "no such db"}, status=404)
        try:
            c = sqlite3.connect(str(p))
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
        except Exception:  # noqa: BLE001
            pass   # best-effort checkpoint; a slightly-behind copy is still fine
        return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    async def _http_chat_clear(self, request: web.Request) -> web.Response:
        """Wipe the conversation (fresh start). Clears the message log + the
        rolling compaction summary. Durable memory facts are untouched."""
        n = self.chat.clear()
        try:
            from sunday.compaction import reset_state
            reset_state()
        except Exception:  # noqa: BLE001
            log.warning("compaction reset failed during clear")
        await self._broadcast({"type": "cleared"})
        log.info("conversation cleared", removed=n)
        return web.json_response({"ok": True, "removed": n})

    async def _http_log(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        # recent() is main-timeline only (thread replies stay off the main log).
        # reply_counts lets the renderer badge rooted messages with "N replies"
        # without a per-message round-trip. Keyed by root_message_id.
        return web.json_response({
            "messages": [m.to_json() for m in self.chat.recent(limit=limit)
                         if not (m.metadata or {}).get("hidden")],
            "reply_counts": {str(k): v for k, v in self.chat.reply_counts().items()},
        })

    # ─── threads (Slack-style reply branches) ────────────────────────────

    async def _http_threads_list(self, request: web.Request) -> web.Response:
        """All threads, most-recently-active first, each with a root preview +
        reply count."""
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            limit = 100
        return web.json_response({"threads": self.chat.list_threads(limit=limit)})

    async def _http_thread_create(self, request: web.Request) -> web.Response:
        """Open a thread off an existing main-timeline message. Idempotent: a
        message has at most one thread, so a repeat call returns the same id."""
        body = await request.json() if request.body_exists else {}
        try:
            root_id = int(body.get("root_message_id"))
        except (TypeError, ValueError):
            return web.json_response({"error": "'root_message_id' is required"}, status=400)
        root = self.chat.get(root_id)
        if root is None:
            return web.json_response({"error": "no such message"}, status=404)
        if root.thread_id is not None:
            return web.json_response({"error": "cannot thread off a thread reply"}, status=400)
        title = (body.get("title") or "").strip() or None
        thread_id = self.chat.create_thread(root_id, title=title)
        thread = self.chat.get_thread(thread_id)
        await self._broadcast({"type": "thread_created", "thread": thread})
        return web.json_response({"ok": True, "thread": thread})

    async def _http_thread_get(self, request: web.Request) -> web.Response:
        """A thread's root message + its replies (oldest first)."""
        try:
            thread_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid id"}, status=400)
        thread = self.chat.get_thread(thread_id)
        if thread is None:
            return web.json_response({"error": "no such thread"}, status=404)
        root = self.chat.get(thread["root_message_id"])
        replies = self.chat.thread_messages(thread_id)
        return web.json_response({
            "thread": thread,
            "root": root.to_json() if root else None,
            "messages": [m.to_json() for m in replies
                         if not (m.metadata or {}).get("hidden")],
        })

    async def _http_thread_say(self, request: web.Request) -> web.Response:
        """Post a message into a thread — routes through the normal turn path
        with thread_id set, so Sunday replies with thread-scoped context."""
        try:
            thread_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid id"}, status=400)
        if self.chat.get_thread(thread_id) is None:
            return web.json_response({"error": "no such thread"}, status=404)
        body = await request.json() if request.body_exists else {}
        text = (body.get("text") or "").strip()
        attachments = body.get("attachments")
        if not text and not attachments:
            return web.json_response({"error": "'text' or 'attachments' is required"}, status=400)
        try:
            result = await self._say(
                text, body.get("modality") or "electron",
                attachments if isinstance(attachments, list) else None,
                thread_id=thread_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("http thread say failed")
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response(result)

    async def _http_status(self, request: web.Request) -> web.Response:
        return web.json_response(await self._dispatch("status", {}))

    async def _http_atoms_list(self, request: web.Request) -> web.Response:
        state = request.query.get("state")
        try:
            limit = int(request.query.get("limit", "200"))
        except ValueError:
            limit = 200
        return web.json_response({"atoms": self.atoms.list(state=state, limit=limit)})

    async def _http_atoms_add(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (body.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "'text' is required"}, status=400)
        aid = self.atoms.add(
            text,
            kind=body.get("kind"),
            state=body.get("state") or "active",
            owner=body.get("owner"),
            evidence=body.get("evidence"),
            source=body.get("source") or "observer",
            completion_signal=body.get("completion_signal"),
            confidence=float(body.get("confidence") or 1.0),
        )
        return web.json_response({"ok": True, "id": aid})

    async def _http_atoms_update(self, request: web.Request) -> web.Response:
        """V2 update path. Body: {action, state?, evidence?, confidence?,
        superseded_by?, source?}. Text is immutable — mutation goes through
        action=superseded (new atom + link). The store enforces the
        confidence guard internally."""
        try:
            aid = int(request.match_info["id"])
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid request"}, status=400)
        action = body.get("action") or "reinforced"
        if action not in ("reinforced", "closed", "dropped", "superseded"):
            return web.json_response({"error": f"invalid action: {action}"}, status=400)
        result = self.atoms.apply_update(
            aid,
            action,
            state=body.get("state"),
            evidence=body.get("evidence"),
            confidence=(float(body["confidence"]) if "confidence" in body and body["confidence"] is not None else None),
            source=body.get("source") or "observer",
            superseded_by=body.get("superseded_by"),
        )
        return web.json_response(result)

    async def _http_conversations_list(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        since = request.query.get("since")
        # Value filter — default hides "low" (TikTok scroll, ambient sound,
        # TV bleed, short fragments). `min_value=low` / `all` overrides.
        min_value = (request.query.get("min_value") or "medium").lower()
        ranked = {"low": 0, "medium": 1, "high": 2}
        floor = ranked.get(min_value, 1) if min_value != "all" else 0
        from sunday.conversations import ConversationStore as _CS

        raw = self.conversations.list(
            limit=limit * 3 if floor > 0 else limit,
            since=float(since) if since else None,
            category=request.query.get("category"),
            source=request.query.get("source"),
        )
        out, skipped = [], {"low": 0, "medium": 0, "high": 0}
        for c in raw:
            v = _CS.value_tier(c.get("category"), int(c.get("transcript_chars") or 0), c.get("summary"))
            c["value"] = v
            if ranked[v] < floor:
                skipped[v] += 1
                continue
            out.append(c)
            if len(out) >= limit:
                break
        return web.json_response({
            "conversations": out,
            "filtered": floor > 0,
            "hidden": skipped,
        })

    async def _http_conversations_get(self, request: web.Request) -> web.Response:
        try:
            cid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid id"}, status=400)
        c = self.conversations.get(cid)
        if not c:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(c)

    async def _http_conversations_search(self, request: web.Request) -> web.Response:
        q = request.query.get("q", "")
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        return web.json_response({"conversations": self.conversations.search(q, limit=limit)})

    async def _http_conversations_add(self, request: web.Request) -> web.Response:
        """Create a conversation. Observer calls this when it closes one on
        silence. If `link_atoms_since` is set, every atom created in
        [link_atoms_since, now] that isn't already in a conversation gets
        linked to the new id — so commitments born inside the conversation
        carry a back-pointer to where they came from."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        started_at = body.get("started_at")
        if not started_at:
            return web.json_response({"error": "'started_at' is required"}, status=400)
        cid = self.conversations.add(
            started_at=float(started_at),
            ended_at=float(body.get("ended_at") or time.time()),
            title=body.get("title"),
            summary=body.get("summary"),
            category=body.get("category"),
            participants=body.get("participants"),
            transcript=body.get("transcript"),
            source=body.get("source") or "observer",
        )
        linked = 0
        if body.get("link_atoms_since"):
            linked = self.atoms.link_to_conversation(
                cid, since=float(body["link_atoms_since"]),
                until=float(body.get("ended_at") or time.time()),
            )
        return web.json_response({"ok": True, "id": cid, "linked_atoms": linked})

    async def _http_atoms_wipe(self, request: web.Request) -> web.Response:
        """Clear the store. Used to nuke pre-v2 spike data."""
        n = self.atoms.wipe()
        return web.json_response({"ok": True, "deleted": n})

    async def _http_observer_now(self, request: web.Request) -> web.Response:
        """Observer (the tick worker on the user's Mac) pushes "what the user
        is doing right now" here. Body: {"now": "<text>", "same_as_last": bool}.
        If same_as_last, keep `since` so duration accumulates; else reset it.
        """
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        text = (body.get("now") or "").strip()
        if not text:
            return web.json_response({"error": "'now' is required"}, status=400)
        same = bool(body.get("same_as_last"))
        now_ts = time.time()
        if same and self._now.get("since"):
            self._now["now"] = text
            self._now["updated_at"] = now_ts
        else:
            self._now = {"now": text, "since": now_ts, "updated_at": now_ts}
        log.info("observer now", text=text[:80], same_as_last=same)
        return web.json_response({"ok": True, "since": self._now["since"]})

    async def _http_observer_tick(self, request: web.Request) -> web.Response:
        """The observation tick. Sunday.app on the Mac captures ~30s of mic,
        transcribes it locally, and POSTs {transcript} here. We run the tick
        brain → create/update atoms, refresh the "now" line, and buffer the
        transcript so we can close + summarize a conversation on silence.

        Body: {transcript: str, silent?: bool}
        Returns: {now, atoms_created, atoms_updated, conversation_closed?}
        """
        from sunday import observer as obs
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)

        transcript = (body.get("transcript") or "").strip()
        now_ts = time.time()
        # The Mac flags a chunk as silent when transcription returns nothing
        # meaningful; treat short noise as silence too.
        silent = bool(body.get("silent")) or len(transcript) < 8

        if silent:
            self._obs_silent_streak += 1
            closed = None
            # After ~1 min (2 silent 30s chunks) drop the stale "now" line so
            # the notch isn't lying about an activity that ended. The label
            # only persists when there's continuing evidence for it.
            if self._obs_silent_streak >= 2 and self._now.get("now"):
                self._now = {"now": None, "since": None, "updated_at": now_ts}
            # Close the open conversation after enough quiet.
            if self._obs_silent_streak >= self._obs_silent_to_close and self._obs_buffer:
                closed = await self._close_observer_conversation()
            self._log_observer_tick(now_ts, "", self._now.get("now"), 0, True)
            return web.json_response({"now": self._now.get("now"), "silent": True, "conversation_closed": closed})

        self._obs_silent_streak = 0
        if self._obs_conv_started is None:
            self._obs_conv_started = now_ts
        self._obs_buffer.append((now_ts, transcript))

        # Cap conversation length: even continuous audio gets cut into
        # closeable chunks. Otherwise the buffer can accumulate for hours
        # before silence triggers a close, and the user never sees it.
        wall_age = now_ts - (self._obs_conv_started or now_ts)
        total_chars = sum(len(t) for _, t in self._obs_buffer)
        if wall_age >= self._obs_conv_max_seconds or total_chars >= self._obs_conv_max_chars:
            await self._close_observer_conversation()

        # Run the tick brain over the open working atoms, handing it the last
        # 'now' so it can keep the activity sticky instead of re-narrating
        # every 30s scene change.
        open_atoms = self.atoms.list(state="active", limit=20)
        try:
            tick = await obs.run_tick(transcript, open_atoms, self.config,
                                      current_now=self._now.get("now"))
        except Exception as exc:  # noqa: BLE001
            log.warning("observer tick failed", error=str(exc))
            return web.json_response({"error": str(exc)}, status=502)

        # Update the "now" line — but keep it STICKY. The model re-phrases the
        # same activity slightly every tick ("AI bubble" → "AI investment
        # discussion"); without a guard the counter resets and the HUD flashes
        # every 30s for one continuous activity. So: treat a new line as a
        # continuation when the model says same_as_last OR the wording clearly
        # overlaps the current line — and in that case keep the ORIGINAL text +
        # since, so nothing flickers.
        now_line = (tick.get("now") or "").strip()
        # "idle" is not an activity — it's the absence of one. Treat any idle/
        # no-activity phrasing as no signal: hold the last real activity and
        # let staleness clear it. Never store or surface an idle label.
        if now_line and ("idle" in now_line.lower() or "no clear activity" in now_line.lower()):
            now_line = ""
        activity_shifted = False
        if now_line:
            same = bool(tick.get("same_as_last"))
            prev = (self._now.get("now") or "").strip()
            if not same and prev and _now_similar(now_line, prev):
                same = True
            if same and self._now.get("since"):
                self._now["updated_at"] = now_ts   # keep original text + since
            else:
                # Genuinely new activity. If we had a buffered conversation
                # of meaningful length under the OLD activity, close it here
                # — the natural conversation boundary is "you stopped doing X
                # and started doing Y", not "60s of silence happened".
                if prev and self._obs_conv_started and (now_ts - self._obs_conv_started) >= self._obs_shift_min_age:
                    activity_shifted = True
                self._now = {"now": now_line, "since": now_ts, "updated_at": now_ts}

        # Activity-shift close: drop the previous conversation now, with the
        # NEW tick's transcript starting a fresh buffer for the new activity.
        if activity_shifted and len(self._obs_buffer) > 1:
            # The just-appended chunk belongs to the new activity — hold it.
            last_chunk = self._obs_buffer.pop()
            await self._close_observer_conversation()
            self._obs_buffer.append(last_chunk)
            self._obs_conv_started = last_chunk[0]

        # Create new atoms; remember index→id so supersede-by-"new:N" resolves.
        new_ids: list[int] = []
        for na in (tick.get("new_atoms") or []):
            aid = self.atoms.add(
                text=na.get("text") or "",
                kind=na.get("kind"),
                owner=na.get("owner"),
                completion_signal=na.get("completion_signal"),
                evidence=transcript[:200],
                source="observer",
            )
            if aid:
                new_ids.append(aid)

        # Apply updates to existing atoms.
        updated = 0
        for u in (tick.get("atom_updates") or []):
            try:
                aid = int(u.get("id"))
            except (TypeError, ValueError):
                continue
            sup = u.get("superseded_by")
            if isinstance(sup, str) and sup.startswith("new:"):
                try:
                    sup = new_ids[int(sup.split(":")[1])]
                except (ValueError, IndexError):
                    sup = None
            self.atoms.apply_update(
                aid,
                action=u.get("action") or "reinforced",
                state=u.get("state"),
                evidence=u.get("evidence"),
                confidence=u.get("confidence"),
                superseded_by=sup if isinstance(sup, int) else None,
            )
            updated += 1

        # Proactive interjection (rare, gated). The tick brain may surface a
        # knowledge-gap; only fire if confidence ≥ floor and cooldowns clear,
        # then spawn a formulation subagent + broadcast to clients via WS.
        proac_fired = None
        proac = tick.get("proac") or None
        if proac and isinstance(proac, dict):
            try:
                proac_fired = await self._maybe_fire_proac(proac, transcript)
            except Exception as exc:  # noqa: BLE001
                log.warning("proac fire failed", error=str(exc))

        self._log_observer_tick(now_ts, transcript, self._now.get("now"), len(new_ids), False)
        log.info("observer tick", now=(self._now.get("now") or "")[:60],
                 atoms_created=len(new_ids), atoms_updated=updated, chars=len(transcript))
        return web.json_response({
            "now": self._now.get("now"),
            "atoms_created": len(new_ids),
            "atoms_updated": updated,
            "proac": proac_fired,
        })

    async def _maybe_fire_proac(self, proac: dict[str, Any], transcript: str) -> dict[str, Any] | None:
        """Gate + formulate + store + broadcast. Returns the stored interjection
        dict on fire, None when gated."""
        from sunday import observer as obs
        from sunday.interjections import cooldown_ok, CONFIDENCE_FLOOR
        try:
            conf = float(proac.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < CONFIDENCE_FLOOR:
            return None
        ok, why = cooldown_ok(self.interjections)
        if not ok:
            log.info("proac gated", reason=why)
            return None
        ask = (proac.get("ask") or "").strip()
        evidence = (proac.get("evidence") or transcript[:160]).strip()
        if not ask:
            return None
        text = await obs.formulate_proac(ask, evidence, self.config)
        if not text:
            return None
        iid = self.interjections.add(
            kind="proac", trigger=proac.get("trigger") or "knowledge_gap",
            text=text, evidence=evidence, confidence=conf,
        )
        payload = {
            "type": "interjection",
            "id": iid,
            "kind": "proac",
            "trigger": proac.get("trigger") or "knowledge_gap",
            "text": text,
            "evidence": evidence,
        }
        # Broadcast to every connected client (notch HUD + desktop app).
        await self._broadcast(payload)
        return payload

    def _log_observer_tick(self, ts: float, transcript: str, now: str | None,
                           atoms_created: int, silent: bool) -> None:
        """Append a raw tick to ~/.sunday/observer_log.jsonl so we can actually
        inspect what the observer heard + decided. This is the corpus for
        evaluating quality (the atoms are sparse; the stream is the truth)."""
        try:
            from sunday.paths import sunday_home
            line = json.dumps({
                "ts": ts,
                "silent": silent,
                "transcript": transcript[:500],
                "now": now,
                "atoms_created": atoms_created,
            })
            with open(sunday_home() / "observer_log.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    # ─── interjections (proactive notes + nudges) ──────────────────────

    async def _http_interjections_latest(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit") or 20)
        except (TypeError, ValueError):
            limit = 20
        return web.json_response({"interjections": self.interjections.latest(limit=limit)})

    async def _http_interjection_engage(self, request: web.Request) -> web.Response:
        """User engaged with a flare. Body: {feedback?: 'up'|'down', reply?: str}.
        Marks engaged_at + (if reply present) folds it into the main chat as a
        real exchange — Sunday's interjection becomes a sunday-role message,
        the user's reply becomes a user-role message, and the brain runs a
        turn so a normal reply lands."""
        try:
            iid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "bad id"}, status=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        feedback = body.get("feedback")
        reply    = (body.get("reply") or "").strip() or None

        rows = self.interjections.latest(limit=200)
        match = next((r for r in rows if r["id"] == iid), None)
        if not match:
            return web.json_response({"error": "not found"}, status=404)
        if match.get("engaged_at"):
            return web.json_response({"ok": True, "already_engaged": True})

        self.interjections.mark_engaged(iid, feedback=feedback, reply=reply)

        # If this was a proactive check-in and the user actually replied, clear
        # the back-off streak — they engaged, so Sunday is welcome to keep
        # reaching out. A bare dismiss (no reply) leaves it "ignored".
        if match.get("trigger") == "checkin" and reply:
            try:
                from sunday import checkin
                st = checkin.load_state()
                if checkin.record_engagement(st, iid):
                    checkin.save_state(st)
            except Exception as exc:  # noqa: BLE001
                log.warning("checkin engagement record failed", error=str(exc))

        # Fold into main chat: Sunday's note becomes a sunday-role message
        # (with metadata so the UI can show it as proactive). If the user
        # replied, that becomes a user-role message and we run a turn.
        try:
            self.chat.append("sunday", match["text"], modality="proac",
                             metadata={"proactive": True, "interjection_id": iid,
                                       "trigger": match.get("trigger")})
        except Exception as exc:  # noqa: BLE001
            log.warning("proac fold failed", error=str(exc))

        if reply:
            # Run a normal turn off the reply so Sunday actually responds.
            # _say doesn't take user_metadata directly; the proac context lives
            # in the prior sunday-role message's metadata, which gives the
            # brain enough signal that this turn is a reply to her interjection.
            asyncio.create_task(self._say(reply, "proac"))

        return web.json_response({"ok": True, "engaged": True, "ran_turn": bool(reply)})

    async def _http_interjection_dismiss(self, request: web.Request) -> web.Response:
        try:
            iid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "bad id"}, status=400)
        self.interjections.mark_dismissed(iid)
        return web.json_response({"ok": True})

    # ─── proactive check-in ────────────────────────────────────────────────

    async def _http_checkin_state(self, request: web.Request) -> web.Response:
        """Current check-in settings + a little observability (last ping,
        recent engagement). Read-only."""
        from sunday import checkin
        st = checkin.load_state()
        s = st.settings
        return web.json_response({
            "enabled": s.enabled,
            "gap_min_hours": s.gap_min_hours,
            "gap_max_hours": s.gap_max_hours,
            "quiet_start_hour": s.quiet_start_hour,
            "quiet_end_hour": s.quiet_end_hour,
            "cooldown_hours": s.cooldown_hours,
            "last_checkin_at": st.last_checkin_at or None,
            "recent_outcomes": st.ledger[-5:],
        })

    async def _http_checkin_set(self, request: web.Request) -> web.Response:
        """Update check-in settings. Partial — only the keys present are
        changed. {enabled: bool, gap_min_hours, gap_max_hours,
        quiet_start_hour, quiet_end_hour, cooldown_hours}. The off-switch is
        just {enabled: false}, persisted, effective immediately."""
        from sunday import checkin
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        st = checkin.load_state()
        s = st.settings
        if "enabled" in body:
            s.enabled = bool(body["enabled"])
        for key in ("gap_min_hours", "gap_max_hours", "cooldown_hours"):
            if key in body:
                try:
                    setattr(s, key, float(body[key]))
                except (TypeError, ValueError):
                    return web.json_response({"error": f"{key} must be a number"}, status=400)
        for key in ("quiet_start_hour", "quiet_end_hour"):
            if key in body:
                try:
                    setattr(s, key, int(body[key]))
                except (TypeError, ValueError):
                    return web.json_response({"error": f"{key} must be an integer"}, status=400)
        checkin.save_state(st)
        log.info("checkin settings updated", enabled=s.enabled)
        return web.json_response({"ok": True, "enabled": s.enabled})

    def pause_checkins(self, reason: str = "user asked") -> None:
        """The conversational off-switch: when the user tells Sunday to stop
        checking in, disable it. Persisted so it survives restarts; re-enable
        from Settings (or by asking)."""
        try:
            from sunday import checkin
            st = checkin.load_state()
            st.settings.enabled = False
            checkin.save_state(st)
            log.info("check-ins disabled", reason=reason)
        except Exception as exc:  # noqa: BLE001
            log.warning("pause_checkins failed", error=str(exc))

    def _user_is_active(self, now_ts: float) -> bool:
        """Best-effort 'is the user here right now' for the recent-activity
        guard. True if the ambient observer has a fresh activity line (they're
        clearly around) — the gap check covers chat recency separately."""
        upd = self._now.get("updated_at")
        return bool(upd and (now_ts - upd) < 120 and self._now.get("now"))

    def _checkin_context(self) -> tuple[list[str], list[str]]:
        """Gather the contextual hooks for a check-in: open working threads
        (atoms) + a few durable memory facts. Both best-effort; the quality
        gate decides whether they're enough to actually send."""
        threads: list[str] = []
        try:
            for a in self.atoms.list(state="active", limit=40):
                if a.get("kind") in ("thread", "commitment", "deadline"):
                    t = (a.get("text") or "").strip()
                    if t:
                        threads.append(t)
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin atoms read failed", error=str(exc))
        notes: list[str] = []
        try:
            if getattr(self.memory, "available", False):
                # the freshest durable facts read like "what's going on with them"
                for row in self.memory.all(limit=6):
                    txt = (getattr(row, "content", "") or "").strip()
                    if txt:
                        notes.append(txt)
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin memory read failed", error=str(exc))
        return threads[:5], notes[:5]

    async def _maybe_check_in(self) -> bool:
        """One evaluation cycle. Decide → (if open) generate-with-quality-gate
        → deliver. Returns True if a check-in actually went out. Never raises."""
        import random as _random
        from datetime import datetime as _dt
        from sunday import checkin

        now = _dt.now().astimezone()
        now_ts = now.timestamp()
        state = checkin.load_state()

        # When did we last hear from the user? Most-recent message that isn't
        # one of Sunday's own proactive pings.
        last_activity = self._last_user_activity_ts()

        decision = checkin.should_check_in(
            now=now, now_ts=now_ts,
            last_user_activity_ts=last_activity,
            state=state,
            is_active=self._user_is_active(now_ts),
            rng=_random.Random(),
        )
        if not decision.go:
            log.debug("checkin skipped", reason=decision.reason)
            return False

        # Gate is open — but the quality gate still has the final say.
        gap = now_ts - (last_activity or now_ts)
        threads, notes = self._checkin_context()
        text = await checkin.generate_checkin_text(
            config=self.config, now=now, gap_seconds=gap,
            open_threads=threads, memory_notes=notes,
        )
        if not text:
            log.info("checkin quality-gated; nothing worth saying this cycle")
            return False

        await self._deliver_checkin(text, now_ts, state)
        return True

    def _last_user_activity_ts(self) -> float | None:
        """Timestamp of the user's last real message — ignoring Sunday's own
        proactive pings (so an unanswered check-in doesn't reset the silence
        clock and suppress the next one forever)."""
        try:
            for m in reversed(self.chat.recent(limit=30)):
                if m.role == "user":
                    return m.created_at
        except Exception as exc:  # noqa: BLE001
            log.warning("last user activity read failed", error=str(exc))
        return None

    async def _deliver_checkin(self, text: str, now_ts: float, state: "Any") -> None:
        """Fold the check-in into the one chat AND fire a real desktop
        notification — reusing the interjection substrate so engage/dismiss
        flows the same as any other proactive note."""
        from sunday import checkin
        iid = 0
        try:
            iid = self.interjections.add(
                kind="proac", trigger="checkin", text=text,
                evidence="proactive check-in (time gap)", confidence=1.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin interjection store failed", error=str(exc))

        # Land it in the conversation immediately so it's waiting when the app
        # opens — not gated behind engagement like knowledge-gap proacs.
        try:
            self.chat.append("sunday", text, modality="checkin",
                             metadata={"proactive": True, "checkin": True,
                                       "interjection_id": iid})
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin chat append failed", error=str(exc))

        # Stamp cooldown + ledger BEFORE broadcasting so a crash mid-broadcast
        # can't double-fire.
        try:
            checkin.record_checkin(state, now_ts, iid=iid)
            checkin.save_state(state)
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin state persist failed", error=str(exc))

        # Broadcast: `notify: true` tells the app to raise a macOS desktop
        # notification; `type: interjection` reuses the existing flare path so
        # the chat refreshes and the note is engageable.
        payload = {
            "type": "interjection",
            "id": iid,
            "kind": "proac",
            "trigger": "checkin",
            "text": text,
            "notify": True,
            "notify_title": "Sunday",
        }
        try:
            await self._broadcast(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("checkin broadcast failed", error=str(exc))
        log.info("proactive check-in sent", preview=text[:60])

    async def _http_observer_buffer(self, request: web.Request) -> web.Response:
        """The conversation Sunday is CURRENTLY buffering — not yet closed +
        summarized. Lets the UI show 'in progress' instead of hiding live
        speech until a 2-min silence triggers a close."""
        if not self._obs_buffer:
            return web.json_response({"empty": True})
        started = self._obs_conv_started or self._obs_buffer[0][0]
        transcript = "\n".join(t for _, t in self._obs_buffer)
        return web.json_response({
            "empty": False,
            "started_at": started,
            "chunks": len(self._obs_buffer),
            "chars": len(transcript),
            "transcript_preview": transcript[-400:],
        })

    async def _http_meeting_hud(self, request: web.Request) -> web.Response:
        """The Mac app sets meeting-recording state here so the notch HUD
        (which polls /v1/status) can show the timer + red dot. Body:
        {recording: bool, since?: float}."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        recording = bool(body.get("recording"))
        self._meeting = {
            "recording": recording,
            "since": float(body["since"]) if recording and body.get("since") else (time.time() if recording else None),
        }
        return web.json_response({"ok": True, "meeting": self._meeting})

    async def _http_meeting_done(self, request: web.Request) -> web.Response:
        """Broadcast a 'summary ready' toast to the notch HUD."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        title = (body.get("title") or "Meeting").strip()
        await self._broadcast({"type": "toast", "text": f"Meeting notes ready · {title}"})
        return web.json_response({"ok": True})

    async def _http_meeting_stop_request(self, request: web.Request) -> web.Response:
        """The notch (or anything) requests the active meeting stop. The Mac
        app polls meeting state and stops the recorder when it sees this."""
        if self._meeting.get("recording"):
            self._meeting["stop_requested"] = True
        return web.json_response({"ok": True})

    async def _http_meeting_finalize(self, request: web.Request) -> web.Response:
        """The Mac records + transcribes a meeting locally, then POSTs the
        speaker-labeled transcript here. We summarize (Granola-style), store
        it as a high-value Conversation, and turn action items into atoms.

        Body: {transcript, started_at?, ended_at?}
        Returns the full notes so the app can show them immediately.
        """
        from sunday import observer as obs
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        transcript = (body.get("transcript") or "").strip()
        # Reject near-empty captures rather than storing a junk "meeting".
        # If we got this little, audio capture failed (usually system audio
        # without a Screen Recording grant) — tell the app so, don't store.
        if len(transcript) < 120:
            return web.json_response({
                "error": "no_audio",
                "detail": "Barely any speech was captured. Make sure Screen Recording is granted to Sunday — system audio (the other people on the call) needs it.",
            }, status=422)
        started = float(body.get("started_at") or time.time())
        ended = float(body.get("ended_at") or time.time())

        notes = await obs.summarize_meeting(transcript, self.config)

        # Store as a Conversation. Build a readable summary body from the
        # structured notes so the existing Conversations UI renders it well.
        summary_lines = [notes["tldr"]]
        if notes["key_points"]:
            summary_lines.append("\nKey points:\n" + "\n".join(f"• {p}" for p in notes["key_points"]))
        if notes["decisions"]:
            summary_lines.append("\nDecisions:\n" + "\n".join(f"• {d}" for d in notes["decisions"]))
        if notes["action_items"]:
            summary_lines.append("\nAction items:\n" + "\n".join(
                f"• [{a.get('owner','?')}] {a.get('task','')}" + (f" (due {a['due']})" if a.get("due") else "")
                for a in notes["action_items"]))
        summary_text = "\n".join(summary_lines).strip()

        cid = self.conversations.add(
            started_at=started, ended_at=ended,
            title=notes["title"], summary=summary_text,
            category="meeting", participants=notes["participants"],
            transcript=transcript, source="meeting",
        )

        # Action items → tracked atoms (this is the Sunday-over-Granola wedge).
        atom_ids = []
        for a in notes["action_items"]:
            task = (a.get("task") or "").strip()
            if not task:
                continue
            aid = self.atoms.add(
                text=task,
                kind="deadline" if a.get("due") else "commitment",
                owner=a.get("owner") or "you",
                completion_signal=None,
                evidence=f"from meeting: {notes['title']}",
                source="meeting",
            )
            if aid:
                atom_ids.append(aid)
        if atom_ids:
            self.atoms.link_to_conversation(cid, since=started, until=ended)

        log.info("meeting finalized", id=cid, title=notes["title"][:50],
                 action_items=len(notes["action_items"]), atoms=len(atom_ids))
        return web.json_response({"ok": True, "conversation_id": cid, "notes": notes, "atoms_created": len(atom_ids)})

    async def _http_observer_log(self, request: web.Request) -> web.Response:
        """Read the recent raw observer ticks (transcript + decided 'now' +
        atoms created). The window into what it's actually capturing."""
        from sunday.paths import sunday_home
        try:
            limit = int(request.query.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        p = sunday_home() / "observer_log.jsonl"
        if not p.exists():
            return web.json_response({"ticks": [], "note": "no observer ticks logged yet"})
        try:
            lines = p.read_text(encoding="utf-8").strip().splitlines()[-limit:]
            ticks = [json.loads(ln) for ln in lines if ln.strip()]
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ticks": ticks, "count": len(ticks)})

    async def _close_observer_conversation(self) -> dict[str, Any] | None:
        """Summarize the buffered transcript window, store it, link atoms born
        during the window, and reset the buffer."""
        from sunday import observer as obs
        if not self._obs_buffer:
            return None
        started = self._obs_conv_started or self._obs_buffer[0][0]
        ended = self._obs_buffer[-1][0]
        transcript = "\n".join(t for _, t in self._obs_buffer)
        # Reset buffer immediately so a slow summary doesn't double-close.
        self._obs_buffer = []
        self._obs_conv_started = None
        self._obs_silent_streak = 0

        # Don't even summarize trivially short windows — that's the source of
        # the one-sentence "conversations". Needs real substance to count.
        if len(transcript) < 280:
            log.info("observer conversation discarded (too short)", chars=len(transcript))
            return None
        try:
            summary = await obs.summarize_conversation(transcript, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("observer conversation summary failed", error=str(exc))
            return None

        # Discard at the source unless the summarizer judged it worth keeping.
        # "significant" = real work, decisions, plans, meaningful moments —
        # not chess banter, food orders, sports chatter, or media bleed.
        if not summary.get("significant", False):
            log.info("observer conversation discarded (not significant)",
                     category=summary["category"], title=summary["title"][:40])
            return None

        cid = self.conversations.add(
            started_at=started, ended_at=ended,
            title=summary["title"], summary=summary["summary"],
            category=summary["category"], participants=summary["participants"],
            transcript=transcript, source="observer",
        )
        linked = self.atoms.link_to_conversation(cid, since=started, until=ended)
        log.info("observer conversation closed", id=cid, title=summary["title"][:50], atoms_linked=linked)
        return {"id": cid, "title": summary["title"], "atoms_linked": linked}

    async def _http_tools(self, request: web.Request) -> web.Response:
        return web.json_response(await self._dispatch("tools", {}))

    async def _http_get_config(self, request: web.Request) -> web.Response:
        """Read-only snapshot of editable daemon config."""
        from sunday.prompt import default_prompt, stable_prefix
        from sunday.paths import custom_prompt_path
        p = custom_prompt_path()
        return web.json_response({
            "model": {
                "provider":   self.config.model.provider,
                "name":       self.config.model.name,
                "base_url":   self.config.model.base_url,
            },
            "identity_prompt": {
                "effective":      stable_prefix(),
                "default":        default_prompt(),
                "custom_present": p.exists(),
            },
            "memory": {
                "available": self.memory.available,
                "count":     self.memory.count() if self.memory.available else 0,
            },
        })

    async def _http_post_config(self, request: web.Request) -> web.Response:
        """Update editable daemon config. Partial — only the keys in the
        body get written. Returns the resulting effective state."""
        from sunday.paths import custom_prompt_path
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)

        applied: dict[str, Any] = {}

        # Identity prompt: write to ~/.sunday/identity.md (empty/null = reset to default)
        if "identity_prompt" in body:
            value = body["identity_prompt"]
            p = custom_prompt_path()
            if value is None or (isinstance(value, str) and not value.strip()):
                if p.exists():
                    p.unlink()
                applied["identity_prompt"] = "reset to default"
            elif isinstance(value, str):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(value, encoding="utf-8")
                applied["identity_prompt"] = f"saved ({len(value)} chars)"
            else:
                return web.json_response({"error": "identity_prompt must be a string or null"}, status=400)

        # Model name swap (provider locked to current). Updates the live
        # config + clears the router cache so the next turn picks it up.
        if "model_name" in body:
            new_name = body["model_name"]
            if not isinstance(new_name, str) or not new_name.strip():
                return web.json_response({"error": "model_name must be a non-empty string"}, status=400)
            from dataclasses import replace
            self.config.model = replace(self.config.model, name=new_name.strip())
            # Invalidate the runtime cache so the next call rebuilds with the new model.
            # The router caches Provider instances per provider_name; we patch in-place.
            applied["model_name"] = new_name.strip()

        # Provider swap (e.g. "codex" to use your ChatGPT subscription). Rebuilds
        # the runtime below so the next turn routes through the new provider.
        if "provider" in body:
            from dataclasses import replace
            prov = body["provider"]
            valid = {"openrouter", "openai", "anthropic", "deepseek-direct", "codex", "ollama"}
            if prov not in valid:
                return web.json_response({"error": f"provider must be one of {sorted(valid)}"}, status=400)
            if prov == "ollama":
                # Local models via Ollama's OpenAI-compatible endpoint — point the
                # config there so the next turn talks to localhost:11434.
                self.config.model = replace(self.config.model, base_url="http://localhost:11434/v1")
            if prov == "codex":
                # Codex reads ~/.codex on THIS daemon's host. Refuse the switch if
                # there's no login here — otherwise the next turn crashes on a
                # missing auth file (e.g. selecting Codex while pointed at a VPS).
                from sunday.runtime.providers.codex import codex_available
                if not codex_available():
                    return web.json_response({"error": (
                        "No Codex login on this daemon. Codex runs where you're signed into the "
                        "codex CLI (~/.codex) — switch the app to 'This Mac' to use it."
                    )}, status=400)
                # ChatGPT accounts use the chat models; default to a working one.
                if not (self.config.model.name or "").startswith("gpt-5"):
                    self.config.model = replace(self.config.model, name="gpt-5.2")
                    applied["model_name"] = "gpt-5.2"
            self.config.model = replace(self.config.model, provider=prov)
            applied["provider"] = prov

        # API keys — write a credential (OPENROUTER_API_KEY, OPENAI_API_KEY, …).
        if isinstance(body.get("credentials"), dict):
            from sunday.credentials import set_credential
            saved = []
            for k, v in body["credentials"].items():
                if isinstance(v, str) and v.strip():
                    set_credential(k.strip(), v.strip())
                    saved.append(k.strip())
            applied["credentials"] = saved

        # Rebuild the runtime if anything affecting routing changed.
        if any(x in applied for x in ("provider", "credentials")):
            from sunday.runtime import build_runtime
            self.runtime = build_runtime(self.config)

        return web.json_response({"applied": applied, "ok": True})

    async def _http_codex_login(self, request: web.Request) -> web.Response:
        """Start a one-click ChatGPT (Codex) sign-in. Spins up the localhost:1455
        callback server, returns the authorize URL for the app to open. The
        browser redirect lands on the callback below, which exchanges the code,
        writes ~/.codex/auth.json, and flips the provider to Codex."""
        from sunday.runtime.providers import codex_auth

        # Already signed in? Skip the browser dance — just activate Codex.
        from sunday.runtime.providers.codex import codex_available
        if codex_available():
            await self._codex_activate()
            return web.json_response({"connected": True, "email": codex_auth.account_email()})

        verifier = codex_auth.new_verifier()
        state = codex_auth.new_state()
        self._codex_login = {"verifier": verifier, "state": state,
                             "connected": False, "email": None, "error": None}
        try:
            await self._start_codex_callback()
        except OSError as exc:
            self._codex_login = None
            return web.json_response({"error": f"could not open the sign-in listener on port 1455 ({exc})"}, status=500)
        return web.json_response({"auth_url": codex_auth.build_authorize(verifier, state)})

    async def _http_ollama_models(self, request: web.Request) -> web.Response:
        """List models installed in the local Ollama (for the provider picker).
        Returns {available, models:[{name, size}]}. Never errors — if Ollama
        isn't running, available:false so the UI can show a friendly hint."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get("http://localhost:11434/api/tags")
                r.raise_for_status()
                data = r.json()
            models = [{"name": m.get("name", ""), "size": m.get("size", 0)}
                      for m in (data.get("models") or []) if m.get("name")]
            return web.json_response({"available": True, "models": models})
        except Exception:  # noqa: BLE001
            return web.json_response({"available": False, "models": []})

    async def _http_local_recommend(self, request: web.Request) -> web.Response:
        """Hardware verdict for fully-local mode + the Gemma-line model menu.
        Drives onboarding's "Fully local vs I have keys" choice and the Brain
        settings affordance. Never errors."""
        import platform
        import shutil
        import subprocess

        def _sysctl(key: str) -> str:
            # absolute path — launchd/app-spawned daemons have a minimal PATH
            try:
                return subprocess.run(["/usr/sbin/sysctl", "-n", key],
                                      capture_output=True, text=True, timeout=3).stdout.strip()
            except Exception:  # noqa: BLE001
                return ""

        ram_gb = 0
        try:
            ram_gb = round(int(_sysctl("hw.memsize") or 0) / 1073741824)
        except ValueError:
            pass
        chip = _sysctl("machdep.cpu.brand_string") or platform.processor() or "unknown"
        apple = platform.machine() == "arm64"
        installed = bool(shutil.which("ollama")) or Path("/Applications/Ollama.app").exists() \
            or Path("/opt/homebrew/bin/ollama").exists() or Path("/usr/local/bin/ollama").exists()
        running, have, oversion = False, [], ""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=1.5) as c:
                r = await c.get("http://localhost:11434/api/tags")
                running = r.status_code == 200
                have = [m.get("name", "") for m in (r.json().get("models") or [])]
                try:
                    oversion = (await c.get("http://localhost:11434/api/version")).json().get("version", "")
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # The Gemma-line ladder. Recommendation gates on hardware; the OPTION
        # is always there — we just don't push local on machines it'd hurt.
        # NB: there is no bare `gemma4:12b` tag — on Apple Silicon the right
        # one is the MLX build (and we only recommend local on arm64 anyway).
        if apple and ram_gb >= 16:
            rec = "local"
            menu = [{"name": "gemma4:12b-mlx", "label": "Gemma 4 12B",
                     "note": "multimodal, 256K context — the one to get (~10GB)", "recommended": True}]
            if ram_gb >= 32:
                menu.append({"name": "gemma4:26b", "label": "Gemma 4 26B (MoE)",
                             "note": f"bigger brain — comfortable with {ram_gb}GB", "recommended": False})
            menu.append({"name": "gemma3:4b", "label": "Gemma 3 4B",
                         "note": "small + fast", "recommended": False})
        elif apple and ram_gb >= 8:
            rec = "local-light"
            menu = [{"name": "gemma3:4b", "label": "Gemma 3 4B",
                     "note": f"right-sized for {ram_gb}GB", "recommended": True},
                    {"name": "gemma3:1b", "label": "Gemma 3 1B", "note": "tiny + instant", "recommended": False}]
        else:
            rec = "keys"
            menu = []
        from sunday.embeddings import EMBED_MODEL
        return web.json_response({
            "apple_silicon": apple, "chip": chip, "ram_gb": ram_gb,
            "ollama": {"installed": installed, "running": running, "models": have, "version": oversion},
            "recommendation": rec, "models": menu, "embed_model": EMBED_MODEL,
        })

    async def _http_ollama_start(self, request: web.Request) -> web.Response:
        """Best-effort start of an installed Ollama (the app autostarts its
        server). Caller polls /v1/local/recommend until running."""
        import shutil
        import subprocess
        if Path("/Applications/Ollama.app").exists():
            try:
                subprocess.Popen(["/usr/bin/open", "-a", "Ollama"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return web.json_response({"ok": True, "via": "app"})
            except Exception as exc:  # noqa: BLE001
                return web.json_response({"ok": False, "error": str(exc)}, status=500)
        binp = shutil.which("ollama") or next((p for p in
                ("/opt/homebrew/bin/ollama", "/usr/local/bin/ollama") if Path(p).exists()), None)
        if not binp:
            return web.json_response({"ok": False, "error": "ollama not installed"}, status=404)
        try:
            subprocess.Popen([binp, "serve"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return web.json_response({"ok": True, "via": "serve"})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _http_ollama_pull(self, request: web.Request) -> web.Response:
        """Streaming proxy for `ollama pull` — NDJSON progress lines
        ({status, total, completed}) so onboarding can render a real bar."""
        import httpx
        body = await request.json()
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"error": "'model' is required"}, status=400)
        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        try:
            async with httpx.AsyncClient(timeout=None) as c:
                async with c.stream("POST", "http://localhost:11434/api/pull",
                                    json={"model": model}) as r:
                    async for line in r.aiter_lines():
                        if line:
                            await resp.write((line + "\n").encode())
        except Exception as exc:  # noqa: BLE001
            await resp.write((json.dumps({"error": str(exc)}) + "\n").encode())
        await resp.write_eof()
        return resp

    async def _http_codex_status(self, request: web.Request) -> web.Response:
        from sunday.runtime.providers import codex_auth
        from sunday.runtime.providers.codex import codex_available
        connected = codex_available()
        pend = self._codex_login or {}
        return web.json_response({
            "connected": connected,
            "email": codex_auth.account_email() if connected else None,
            "pending": bool(self._codex_login) and not pend.get("connected") and not pend.get("error"),
            "error": pend.get("error"),
        })

    async def _http_gmail_status(self, request: web.Request) -> web.Response:
        """Report whether the direct Gmail connector (IMAP/SMTP app password)
        is configured, and — once — whether the credentials actually log in.

        The IMAP login probe is cached for the daemon's lifetime after the
        first success (an app password doesn't change underneath us), runs with
        a tight timeout, and never throws — a flaky network must not 500 the
        Settings page. {configured, address, ok}."""
        from sunday.credentials import get_credential
        address = get_credential("GMAIL_ADDRESS")
        password = get_credential("GMAIL_APP_PASSWORD")
        if password:
            password = password.replace(" ", "")
        configured = bool(address and password)
        if not configured:
            return web.json_response({"configured": False, "address": None, "ok": False})

        # Cached probe, keyed on the address so a credential change re-tests.
        cache = getattr(self, "_gmail_login_ok", None)
        if cache and cache.get("address") == address and cache.get("ok"):
            return web.json_response({"configured": True, "address": address, "ok": True})

        def _probe() -> bool:
            import imaplib
            try:
                imap = imaplib.IMAP4_SSL("imap.gmail.com", timeout=3)
                try:
                    imap.login(address, password)
                    return True
                finally:
                    try:
                        imap.logout()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001 — never throw out of a status probe
                return False

        ok = False
        try:
            ok = await asyncio.wait_for(asyncio.to_thread(_probe), timeout=3.5)
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            self._gmail_login_ok = {"address": address, "ok": True}
        return web.json_response({"configured": True, "address": address, "ok": ok})

    async def _http_cockpit_status(self, request: web.Request) -> web.Response:
        """Cockpit pairing + connection state for the Settings card.

        paired          — a COCKPIT_TOKEN credential is set (the user pasted the token).
        connected       — the extension's WebSocket is live right now.
        token_mismatch  — an extension knocked with a wrong/absent token in the
                          last 60s (tokens regenerate on extension reinstall;
                          the card uses this to say "re-copy the token").
        """
        import time as _time
        recent_reject = (
            self.cockpit.last_reject > 0
            and (_time.monotonic() - self.cockpit.last_reject) < 60
        )
        return web.json_response({
            "paired": self.cockpit.paired(),
            "connected": self.cockpit.connected,
            "token_mismatch": recent_reject and not self.cockpit.connected,
        })

    async def _http_vapi_status(self, request: web.Request) -> web.Response:
        """Whether phone calling is wired up, for the Calling card in Settings.

        configured       — both the API key and a from-number id are set, so a
                           call can actually be placed.
        has_api_key      — VAPI_API_KEY credential present.
        has_from_number  — VAPI_PHONE_NUMBER_ID credential present.
        from_number_id   — the id itself (not a secret — it's a phone-number
                           handle, safe to show so the owner can confirm it).
        model            — the on-call voice model VAPI will run.
        """
        from sunday.credentials import get_credential
        api_key = get_credential("VAPI_API_KEY")
        from_number = get_credential("VAPI_PHONE_NUMBER_ID")
        return web.json_response({
            "configured": bool(api_key and from_number),
            "has_api_key": bool(api_key),
            "has_from_number": bool(from_number),
            "from_number_id": from_number or None,
            "model": self.config.vapi.model_name,
        })

    async def _http_vapi_test(self, request: web.Request) -> web.Response:
        """Place a real test call so the whole calling round-trip can be proven
        from the desktop. Dials the number the owner entered with a short,
        self-describing purpose; the transcript + summary land in the one chat
        (modality='vapi') when the call ends, same as any other call."""
        from sunday.channels.vapi import _create_call, spawn_call_poller
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        to = (body.get("to") or "").strip()
        if not to:
            return web.json_response({"ok": False, "error": "enter a number to call"}, status=400)
        purpose = (
            "This is a test call placed from Sunday's Settings to confirm phone "
            "calling works. Briefly say you're Sunday testing the connection, "
            "confirm the line is clear, then politely end the call."
        )
        try:
            result = await _create_call(str(to), purpose, self.config)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=502)
        if isinstance(result, dict) and result.get("error"):
            return web.json_response({"ok": False, "error": result["error"]}, status=400)
        # Learn the outcome: VAPI's webhook can't reach the local daemon, so
        # poll for the end-of-call report in the background. It lands in the
        # one chat (modality='vapi') just like a real call's outcome.
        spawn_call_poller(self, result.get("call_id"), result.get("status"))
        return web.json_response({"ok": True, "result": result})

    async def _http_vapi_calls(self, request: web.Request) -> web.Response:
        """The Calls view's list — pulled live from VAPI (we hold the key; the
        renderer never sees it). Trimmed rows, newest first, capped at ~50."""
        from sunday.channels.vapi import list_calls
        try:
            result = await list_calls(limit=50)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)
        if isinstance(result, dict) and result.get("error"):
            return web.json_response({"error": result["error"]}, status=400)
        return web.json_response(result)

    async def _http_vapi_call_get(self, request: web.Request) -> web.Response:
        """One call's detail for the Calls view: transcript, summary, and the
        VAPI-stored recording URL the in-app audio player points at."""
        from sunday.channels.vapi import get_call
        call_id = request.match_info.get("id", "").strip()
        if not call_id:
            return web.json_response({"error": "missing call id"}, status=400)
        try:
            result = await get_call(call_id)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)
        if isinstance(result, dict) and result.get("error"):
            return web.json_response({"error": result["error"]}, status=400)
        return web.json_response(result)

    # ── unified Inbox (spec §4b) ─────────────────────────────────────────
    # The Inbox merges two sources into ONE item shape so the renderer reads a
    # single feed. Voice still live-fetches VAPI (no store push for calls yet);
    # text/email/webhook read the local activity store. As push lands, voice
    # joins the store too and this merge collapses to a pure store read.
    #
    # Item shape (the contract the UI agent matches):
    #   {id, channel, direction, peer, ts, preview, status, thread_id, provider_id}

    @staticmethod
    def _voice_row_to_item(call: dict[str, Any]) -> dict[str, Any]:
        """Map a VAPI list_calls() row into the unified Inbox item shape.
        Voice is always outbound (Sunday placed the call) and the peer is the
        number she dialed; the call id doubles as id and provider_id."""
        cid = call.get("id")
        dur = call.get("durationSeconds")
        reason = call.get("endedReason")
        preview = (f"{dur:.0f}s — {reason}" if isinstance(dur, (int, float)) and reason
                   else (reason or (f"{dur:.0f}s" if isinstance(dur, (int, float)) else "")))
        return {
            "id": cid,
            "channel": "voice",
            "direction": "out",
            "peer": call.get("to"),
            "ts": call.get("createdAt"),
            "preview": preview or None,
            "status": call.get("status"),
            "thread_id": None,
            "provider_id": cid,
        }

    async def _http_inbox_list(self, request: web.Request) -> web.Response:
        """GET /v1/inbox?channel=all|voice|text|email|webhook&limit=50 — the
        merged activity feed, newest first. Voice comes live from VAPI; the
        store-backed channels come from the local activity store. Returns
        {"items":[...]} of the unified item shape."""
        channel = (request.query.get("channel") or "all").strip().lower()
        try:
            limit = int(request.query.get("limit", "50"))
        except ValueError:
            limit = 50
        limit = max(1, min(limit, 200))

        items: list[dict[str, Any]] = []

        # Voice: live-fetch VAPI for 'voice' or 'all' (no store rows yet).
        if channel in ("all", "voice"):
            try:
                from sunday.channels.vapi import list_calls
                vres = await list_calls(limit=limit)
                if isinstance(vres, dict) and not vres.get("error"):
                    items.extend(self._voice_row_to_item(c) for c in (vres.get("calls") or []))
            except Exception as exc:  # noqa: BLE001
                log.warning("inbox voice fetch failed", error=str(exc))

        # Store-backed channels: read the local activity store.
        store_channel = None if channel == "all" else channel
        if channel != "voice":
            try:
                rows = await self.activity.list(channel=store_channel, limit=limit)
                items.extend(rows)
            except Exception as exc:  # noqa: BLE001
                log.warning("inbox store read failed", error=str(exc))

        # Merge newest-first across both sources; ts is iso8601 so a string
        # sort orders correctly. None ts sinks to the bottom.
        items.sort(key=lambda r: r.get("ts") or "", reverse=True)
        return web.json_response({"items": items[:limit]})

    async def _http_inbox_get(self, request: web.Request) -> web.Response:
        """GET /v1/inbox/{id} — one item's detail. Voice delegates to VAPI's
        get_call (transcript/recording fetched fresh); everything else returns
        the stored row plus its raw_json. Tries the store first, then falls
        through to VAPI for ids the store doesn't know (a live voice row)."""
        item_id = request.match_info.get("id", "").strip()
        if not item_id:
            return web.json_response({"error": "missing id"}, status=400)

        # Store-backed row (text/email/webhook) — includes raw_json.
        try:
            row = await self.activity.get(item_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("inbox detail store read failed", error=str(exc))
            row = None
        if row is not None:
            if row.get("channel") == "voice":
                # A voice row that somehow landed in the store: still prefer the
                # live VAPI detail for transcript/recording.
                return await self._inbox_voice_detail(item_id)
            return web.json_response(row)

        # Unknown to the store → treat as a live voice call id.
        return await self._inbox_voice_detail(item_id)

    async def _inbox_voice_detail(self, call_id: str) -> web.Response:
        """Voice detail via VAPI get_call — transcript, summary, recording."""
        from sunday.channels.vapi import get_call
        try:
            result = await get_call(call_id)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)
        if isinstance(result, dict) and result.get("error"):
            return web.json_response({"error": result["error"]}, status=404)
        return web.json_response(result)

    async def _start_codex_callback(self) -> None:
        """Temporary aiohttp server on 127.0.0.1:1455 (no auth middleware) that
        catches the OAuth redirect. Torn down once we have the code."""
        from sunday.runtime.providers import codex_auth

        async def _cb(req: web.Request) -> web.Response:
            done_html = ("<!doctype html><meta charset=utf-8><title>Sunday</title>"
                         "<body style=\"font-family:-apple-system,system-ui;background:#f7f4ef;"
                         "color:#1c1b19;display:flex;align-items:center;justify-content:center;"
                         "height:100vh;margin:0\"><div style=\"text-align:center\">"
                         "<h2 style=\"font-weight:650\">{title}</h2><p style=\"color:#6b6357\">{msg}</p>"
                         "</div>")
            login = self._codex_login or {}
            if req.query.get("state") != login.get("state"):
                return web.Response(text=done_html.format(title="Sign-in mismatch",
                    msg="Please try connecting again from Sunday."), content_type="text/html", status=400)
            err = req.query.get("error")
            if err:
                login["error"] = req.query.get("error_description") or err
                return web.Response(text=done_html.format(title="Sign-in cancelled",
                    msg="You can close this tab."), content_type="text/html")
            code = req.query.get("code")
            if not code:
                login["error"] = "no authorization code returned"
                return web.Response(text=done_html.format(title="Sign-in failed",
                    msg="No code returned. Close this tab and try again."), content_type="text/html", status=400)
            try:
                tokens = await codex_auth.exchange_code(code, login["verifier"])
                email = codex_auth.write_auth(tokens)
                await self._codex_activate()
                login["connected"] = True
                login["email"] = email
                log.info("codex connected", email=email)
            except Exception as exc:  # noqa: BLE001
                login["error"] = str(exc)
                log.exception("codex code exchange failed")
                return web.Response(text=done_html.format(title="Sign-in failed",
                    msg="Something went wrong. Close this tab and try again."), content_type="text/html", status=500)
            # Tear the callback server down after we've handled the redirect.
            asyncio.create_task(self._stop_codex_callback())
            who = f"Connected as {email}." if email else "Connected."
            return web.Response(text=done_html.format(title="ChatGPT connected",
                msg=f"{who} You can close this tab and return to Sunday."), content_type="text/html")

        await self._stop_codex_callback()
        app = web.Application()
        app.router.add_get("/auth/callback", _cb)
        # The browser sends every localhost cookie back on the redirect — dev
        # machines can carry tens of KB of analytics cookies, which blows past
        # aiohttp's default 8190-byte header limit and rejects the callback
        # before we can read the code. Raise the line/field limits generously.
        runner = web.AppRunner(app, max_line_size=131072, max_field_size=131072)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", codex_auth.REDIRECT_PORT)
        await site.start()
        self._codex_cb_runner = runner

    async def _stop_codex_callback(self) -> None:
        if self._codex_cb_runner is not None:
            try:
                await self._codex_cb_runner.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self._codex_cb_runner = None

    async def _codex_activate(self) -> None:
        """Switch the live config to Codex (gpt-5.2) + rebuild the runtime."""
        from dataclasses import replace
        if not (self.config.model.name or "").startswith("gpt-5"):
            self.config.model = replace(self.config.model, name="gpt-5.2")
        self.config.model = replace(self.config.model, provider="codex")
        from sunday.runtime import build_runtime
        self.runtime = build_runtime(self.config)

    async def _http_memory_facts(self, request: web.Request) -> web.Response:
        """List facts (newest first), or — when `q` is present — FTS-search
        them. Powers the "What Sunday Knows" facts pane. `limit` caps the rows."""
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 2000))
        q = (request.query.get("q") or "").strip()
        if not self.memory.available:
            rows = []
        elif q:
            rows = self.memory.search(q, limit=limit)
        else:
            rows = self.memory.all(limit=limit)
        return web.json_response({
            "available": self.memory.available,
            "total":     self.memory.count() if self.memory.available else 0,
            "facts": [
                {"id": r.id, "content": r.content, "source": r.source, "created_at": r.created_at}
                for r in rows
            ],
        })

    async def _http_memory_facts_add(self, request: web.Request) -> web.Response:
        """Add a fact by hand (the "Add fact" button). Body: {content, source?}.
        Goes through the same store path the auto-extractor uses, so it lands in
        core context + gets background-embedded for recall."""
        if not self.memory.available:
            return web.json_response({"error": "memory unavailable"}, status=503)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        content = (body.get("content") or "").strip()
        if not content:
            return web.json_response({"error": "'content' is required"}, status=400)
        source = (body.get("source") or "chat").strip() or "chat"
        mem_id = await self.memory.store(content, source=source)
        if not mem_id:
            return web.json_response({"error": "store failed"}, status=500)
        return web.json_response({"ok": True, "id": mem_id, "content": content, "source": source})

    async def _http_memory_facts_update(self, request: web.Request) -> web.Response:
        """Edit a fact's text. Body: {content}. Rewrites the row + FTS and drops
        the stale vector so background indexing re-embeds the new text."""
        if not self.memory.available:
            return web.json_response({"error": "memory unavailable"}, status=503)
        try:
            mem_id = int(request.match_info["id"])
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid request"}, status=400)
        content = (body.get("content") or "").strip()
        if not content:
            return web.json_response({"error": "'content' is required"}, status=400)
        if not self.memory.update(mem_id, content):
            return web.json_response({"error": f"no such fact: {mem_id}"}, status=404)
        return web.json_response({"ok": True, "id": mem_id, "content": content})

    async def _http_memory_facts_delete(self, request: web.Request) -> web.Response:
        """Forget a fact (the "Forget" action). DELETE /v1/memory/facts/{id}."""
        if not self.memory.available:
            return web.json_response({"error": "memory unavailable"}, status=503)
        try:
            mem_id = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid id"}, status=400)
        if not self.memory.forget(mem_id):
            return web.json_response({"error": f"no such fact: {mem_id}"}, status=404)
        return web.json_response({"ok": True, "id": mem_id})

    async def _http_memory_graph(self, request: web.Request) -> web.Response:
        from sunday import memory_graph
        try:
            # Build lazily (incrementally) if new facts have landed since last.
            if memory_graph.needs_rebuild():
                await memory_graph.ingest(self.config)
            return web.json_response(memory_graph.graph())
        except Exception as exc:  # noqa: BLE001
            log.warning("memory graph read failed", error=str(exc))
            return web.json_response({"nodes": [], "links": [], "error": str(exc)})

    async def _http_memory_graph_rebuild(self, request: web.Request) -> web.Response:
        from sunday import memory_graph
        try:
            data = await memory_graph.rebuild(self.config, force=True)
            return web.json_response(data)
        except Exception as exc:  # noqa: BLE001
            log.exception("memory graph rebuild failed")
            return web.json_response({"error": str(exc)}, status=500)

    # --- skills ---------------------------------------------------------
    # Reusable procedures under ~/.sunday/skills/. The agent reaches these
    # through the skill tools + the per-turn shelf (sunday.skills); these
    # routes are the Settings UI's read/write surface. AUTH-GATED via the
    # default middleware.

    async def _http_skills_list(self, request: web.Request) -> web.Response:
        from sunday.skills import list_skills
        try:
            out = []
            for s in list_skills():
                try:
                    st = s.path.stat()
                    size, mtime = st.st_size, st.st_mtime
                except OSError:
                    size, mtime = 0, 0.0
                out.append({
                    "slug":        s.slug,
                    "name":        s.name,
                    "description": s.description,
                    "size":        size,
                    "mtime":       mtime,
                })
            return web.json_response({"skills": out})
        except Exception as exc:  # noqa: BLE001
            log.warning("skills list failed", error=str(exc))
            return web.json_response({"skills": [], "error": str(exc)}, status=500)

    async def _http_skill_get(self, request: web.Request) -> web.Response:
        from sunday.skills import load_skill
        slug = request.match_info.get("slug", "")
        skill = load_skill(slug)
        if skill is None:
            return web.json_response({"error": f"no such skill: {slug}"}, status=404)
        return web.json_response({
            "slug":        skill.slug,
            "name":        skill.name,
            "description": skill.description,
            "body":        skill.body(),
        })

    async def _http_skills_save(self, request: web.Request) -> web.Response:
        """Create or overwrite a skill. Body: {slug?, name, body}. When slug is
        omitted it's derived from name. Reusing a slug overwrites (the patch
        path), matching the save_skill tool."""
        from sunday.skills import skills_dir, _slug_safe, load_skill
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)

        markdown = body.get("body")
        if not isinstance(markdown, str) or not markdown.strip():
            return web.json_response({"error": "'body' is required"}, status=400)

        slug = (body.get("slug") or "").strip()
        name = (body.get("name") or "").strip()
        if not slug:
            slug = _slug_safe(name or markdown.lstrip("#").strip().splitlines()[0] if markdown else "")
        else:
            slug = _slug_safe(slug)
        if not slug:
            return web.json_response({"error": "could not derive a slug — pass 'slug' or 'name'"}, status=400)

        # If the caller gave a name and the body has no H1, prepend one so the
        # parsed name matches — keeps the list/editor honest about the title.
        text = markdown
        if name and not text.lstrip().startswith("#"):
            text = f"# {name}\n\n{text.lstrip()}"

        existed = load_skill(slug) is not None
        p = skills_dir() / f"{slug}.md"
        p.write_text(text, encoding="utf-8")
        log.info("skill saved via http", slug=slug, bytes=len(text), overwrote=existed)
        return web.json_response({"ok": True, "slug": slug, "created": not existed})

    async def _http_skill_delete(self, request: web.Request) -> web.Response:
        from sunday.skills import delete_skill
        slug = request.match_info.get("slug", "")
        if delete_skill(slug):
            return web.json_response({"ok": True, "slug": slug})
        return web.json_response({"error": f"no such skill: {slug}"}, status=404)

    async def _http_skills_search(self, request: web.Request) -> web.Response:
        """Search the open skills.sh directory (the 'find skills online'
        surface). Thin wrapper over sunday.skills.search_directory."""
        from sunday.skills import search_directory
        q = (request.query.get("q") or "").strip()
        if not q:
            return web.json_response({"results": []})
        try:
            limit = max(1, min(int(request.query.get("limit", "10")), 25))
        except ValueError:
            limit = 10
        try:
            results = await search_directory(q, limit=limit)
            return web.json_response({"results": results, "directory": "https://skills.sh"})
        except Exception as exc:  # noqa: BLE001
            log.warning("skills search failed", error=str(exc))
            return web.json_response({"results": [], "error": str(exc)}, status=502)

    async def _http_skills_install(self, request: web.Request) -> web.Response:
        """Install a skill from skills.sh by its directory id. Wrapper over
        sunday.skills.install_from_directory."""
        from sunday.skills import install_from_directory
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)
        slug = (body.get("slug") or "").strip()
        if not slug:
            return web.json_response({"error": "'slug' is required"}, status=400)
        try:
            return web.json_response(await install_from_directory(slug))
        except FileNotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:  # noqa: BLE001
            log.warning("skill install failed", slug=slug, error=str(exc))
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)

    # --- cost meter -----------------------------------------------------

    async def _http_cost_log(self, request: web.Request) -> web.Response:
        """Producer-side push (e.g. the bundled observer.py, which talks to
        OpenRouter directly and so can't go through our provider wrapper).

        Body: {kind, purpose, provider, model, prompt_tokens?, completion_tokens?,
               audio_seconds?, latency_ms?}
        """
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        try:
            from sunday.cost import get_store
            store = get_store()
            kind = (payload.get("kind") or "llm").strip()
            if kind == "audio":
                ev = store.log_audio(
                    purpose=payload.get("purpose") or "unknown",
                    provider=payload.get("provider") or "unknown",
                    model=payload.get("model") or "unknown",
                    duration_seconds=float(payload.get("audio_seconds") or 0.0),
                    latency_ms=int(payload.get("latency_ms") or 0),
                )
            else:
                ev = store.log_llm(
                    purpose=payload.get("purpose") or "unknown",
                    provider=payload.get("provider") or "unknown",
                    model=payload.get("model") or "unknown",
                    prompt_tokens=int(payload.get("prompt_tokens") or 0),
                    completion_tokens=int(payload.get("completion_tokens") or 0),
                    latency_ms=int(payload.get("latency_ms") or 0),
                )
            return web.json_response({"ok": True, "cost_usd": ev.cost_usd})
        except Exception as exc:  # noqa: BLE001
            log.warning("cost log write failed", error=str(exc))
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_cost_summary(self, request: web.Request) -> web.Response:
        """Aggregated spend. Query param `since` is unix seconds; defaults
        to 24h ago so the most common 'what did today cost' question is the
        no-arg path."""
        import time
        try:
            since = float(request.query.get("since") or (time.time() - 86400))
        except (TypeError, ValueError):
            since = time.time() - 86400
        try:
            from sunday.cost import get_store
            return web.json_response(get_store().summary(since))
        except Exception as exc:  # noqa: BLE001
            log.warning("cost summary failed", error=str(exc))
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_cost_recent(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        try:
            from sunday.cost import get_store
            return web.json_response({"events": get_store().recent(limit=limit)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_integrations(self, request: web.Request) -> web.Response:
        from sunday.integrations import nango
        if not nango.configured():
            return web.json_response({
                "configured": False,
                "providers": [{"id": k, "label": v["label"], "connected": False} for k, v in nango.PROVIDERS.items()],
            })
        connected = set()
        try:
            for p in nango.PROVIDERS:
                if await nango.is_connected(p):
                    connected.add(p)
        except Exception as exc:  # noqa: BLE001
            log.warning("integrations status failed", error=str(exc))
        return web.json_response({
            "configured": True,
            "providers": [{"id": k, "label": v["label"], "connected": k in connected} for k, v in nango.PROVIDERS.items()],
        })

    async def _http_integrations_connect(self, request: web.Request) -> web.Response:
        from sunday.integrations import nango
        body = await request.json()
        provider = body.get("provider")
        if provider not in nango.PROVIDERS:
            return web.json_response({"error": f"unknown provider: {provider}"}, status=400)
        return web.json_response(await nango.create_connect_session(provider))

    async def _http_integrations_provision(self, request: web.Request) -> web.Response:
        from sunday.integrations import nango
        return web.json_response(await nango.provision_from_env())

    # ─── catalog: dynamic provider browsing + per-provider setup card ─────

    async def _http_integrations_catalog(self, request: web.Request) -> web.Response:
        """Browse the 830 Nango providers, filtered by category/auth_mode/q.
        Returns lean records (name, display_name, categories, auth_mode) for
        the list view — the full setup spec comes from /setup/:name.
        """
        from sunday.integrations import nango
        category = request.query.get("category")
        auth_mode = request.query.get("auth_mode")
        q = (request.query.get("q") or "").strip().lower()
        try:
            items = await nango.list_providers()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)
        out = []
        for p in items:
            cats = p.get("categories") or []
            if category and category not in cats:
                continue
            if auth_mode and p.get("auth_mode") != auth_mode:
                continue
            name = p.get("name") or ""
            display = p.get("display_name") or name
            if q and q not in name.lower() and q not in display.lower():
                continue
            out.append({
                "name": name,
                "display_name": display,
                "categories": cats,
                "auth_mode": p.get("auth_mode") or "INHERITED",
            })
        # Stable sort: popular first, then alphabetical.
        out.sort(key=lambda r: (0 if "popular" in r["categories"] else 1, r["display_name"].lower()))
        return web.json_response({"providers": out, "total": len(out)})

    async def _http_integrations_setup(self, request: web.Request) -> web.Response:
        """The setup card spec for ONE provider — display_name, docs URLs,
        credentials + connection_config schemas, auth_mode resolved through
        the alias chain. Front-end renders a form from this.
        """
        from sunday.integrations import nango
        name = request.match_info.get("name", "")
        try:
            entry = await nango.get_provider(name)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=502)
        if not entry:
            return web.json_response({"error": f"unknown provider: {name}"}, status=404)
        # Keep the payload focused on what the renderer needs.
        slim = {k: entry.get(k) for k in (
            "name", "display_name", "categories", "auth_mode",
            "docs", "docs_connect", "setup_guide_url",
            "authorization_url", "token_url",
            "default_scopes", "credentials", "connection_config",
            "_chain",
        ) if k in entry or k == "name"}
        slim["redirect_uri"] = f"{nango.public_url()}/oauth/callback" if nango.public_url() else None
        return web.json_response(slim)

    # ─── MCP Registry: browse + install + uninstall ────────────────────

    async def _http_mcp_registry(self, request: web.Request) -> web.Response:
        """Browse the official MCP Registry. Query param `q` is a free-text
        search across server name + title + description (server-side). The
        UI uses this to populate the 'Add a connector' search box."""
        from sunday import mcp_registry
        q = request.query.get("q", "")
        try:
            limit = int(request.query.get("limit") or 30)
        except (TypeError, ValueError):
            limit = 30
        items = await mcp_registry.list_servers(q=q, limit=limit)
        return web.json_response({"servers": items, "count": len(items)})

    async def _http_mcp_install(self, request: web.Request) -> web.Response:
        """Install one MCP server from the registry.

        Body: {name, secrets?}.

        First call with just `name` — if the server's auth headers need
        user-supplied values (e.g. an API key for Smithery), we return
        {missing_fields, fields} so the UI can render a form. Second call
        with `secrets: {placeholder_key: value}` does the actual install.
        """
        from sunday import mcp, mcp_registry
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        name = (body.get("name") or "").strip()
        if not name:
            return web.json_response({"error": "name required"}, status=400)
        server = await mcp_registry.get_server(name)
        if not server:
            return web.json_response({"error": f"server '{name}' not found in registry"}, status=404)

        cfg = mcp.load_config()
        out = mcp_registry.install_remote(server, cfg, secrets=body.get("secrets"))
        if out.get("missing_fields"):
            # Tell the UI what to ask for; don't write anything yet.
            return web.json_response({
                "needs_fields": True,
                "fields": out["fields"],
                "title": server.get("title"),
                "description": server.get("description"),
            })
        if out.get("error"):
            return web.json_response(out, status=400)
        mcp.save_config(out["config"])
        try:
            status = await mcp.connect_all(self.registry, self.config)
        except Exception as exc:  # noqa: BLE001
            log.exception("mcp connect failed after install")
            return web.json_response({"installed": out["slug"], "connect_error": str(exc)}, status=502)
        return web.json_response({"ok": True, "slug": out["slug"], "servers": list(status.values())})

    async def _http_mcp_inspect(self, request: web.Request) -> web.Response:
        """Get the field schema for one server without installing — so the
        UI can render its setup form."""
        from sunday import mcp_registry
        name = request.match_info.get("name", "")
        server = await mcp_registry.get_server(name)
        if not server:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({
            "name": server.get("name"),
            "title": server.get("title"),
            "description": server.get("description"),
            "kind": server.get("kind"),
            "fields": mcp_registry.required_fields(server),
            "remotes_count": len(server.get("remotes") or []),
        })

    async def _http_mcp_uninstall(self, request: web.Request) -> web.Response:
        """Remove an installed MCP server by slug + reconnect."""
        from sunday import mcp, mcp_registry
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        slug = (body.get("slug") or "").strip()
        if not slug:
            return web.json_response({"error": "slug required"}, status=400)
        cfg = mcp.load_config()
        out = mcp_registry.uninstall(slug, cfg)
        if out.get("error"):
            return web.json_response(out, status=400)
        mcp.save_config(out["config"])
        try:
            status = await mcp.connect_all(self.registry, self.config)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"removed": slug, "connect_error": str(exc)}, status=502)
        return web.json_response({"ok": True, "slug": slug, "servers": list(status.values())})

    async def _http_connectors_get(self, request: web.Request) -> web.Response:
        """The unified list of the user's connectors, of two kinds:

          • MCP servers (kind="mcp")    — installed from the MCP Registry.
                Their tools are auto-discovered via Sunday's MCP plumbing.
          • Nango providers (kind="nango") — services connected via Nango's
                OAuth proxy. Tools come from hand-written modules (gmail,
                fireflies, …) or the auto-generated use_<provider>_api fallback.

        Each row carries `enabled` (toggled-on for always-on schema),
        `has_tools` (does Sunday have any tool registered for this connector?),
        and a friendly `label`.
        """
        from sunday.integrations import nango
        from sunday import connectors, mcp
        enabled = connectors.load()
        with_tools = set(connectors.providers_with_tools())
        out: list[dict] = []

        # --- MCP servers (installed from the registry) -----------------
        # Read mcp.json directly; cross-reference live connection status so
        # we can flag servers that are configured but failing to connect.
        mcp_cfg = mcp.load_config()
        mcp_servers = (mcp_cfg.get("mcpServers") or {})
        # mcp.STATUS keys by server name; values are dicts holding name/tools.
        for slug, _spec in mcp_servers.items():
            live = mcp.STATUS.get(slug) or {}
            tool_count = len(live.get("tools") or [])
            out.append({
                "kind":      "mcp",
                "provider":  slug,
                "label":     slug.replace("-", " ").title(),
                "connected": bool(live.get("connected") or tool_count > 0),
                "has_tools": tool_count > 0,
                # MCP servers are always-on by design when installed — their
                # tools are already in the registry. The toggle in the popover
                # is informational; uninstall to remove.
                "enabled":   tool_count > 0,
                "tool_count": tool_count,
            })

        # --- Nango providers (per-service OAuth connections) -----------
        try:
            conns = await nango.list_connections()
        except Exception:  # noqa: BLE001
            conns = []
        catalog_labels: dict[str, str] = {}
        try:
            for p in await nango.list_providers():
                if p.get("name"):
                    catalog_labels[p["name"]] = p.get("display_name") or p["name"]
        except Exception:  # noqa: BLE001
            pass
        connected_keys = sorted({c.get("provider_config_key") for c in conns if c.get("provider_config_key")})
        for key in connected_keys:
            out.append({
                "kind":      "nango",
                "provider":  key,
                "label":     catalog_labels.get(key, key),
                "connected": True,
                "has_tools": key in with_tools,
                "enabled":   key in enabled,
            })

        return web.json_response({
            "connectors": out,
            "enabled":   sorted(enabled),
            "with_tools": sorted(with_tools),
        })

    async def _http_connectors_toggle(self, request: web.Request) -> web.Response:
        """Flip one connector on/off. Body: {provider, on}. Returns the new
        enabled set. Effect is immediate on the next chat turn — no restart."""
        from sunday import connectors
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        provider = (body.get("provider") or "").strip()
        if not provider:
            return web.json_response({"error": "provider required"}, status=400)
        if provider not in connectors.PROVIDER_TOOL_PREFIXES:
            return web.json_response({"error": f"no tools shipped for {provider} yet"}, status=400)
        on = bool(body.get("on"))
        new_state = connectors.toggle(provider, on)
        log.info("connector toggled", provider=provider, on=on, total_enabled=len(new_state))
        return web.json_response({"ok": True, "enabled": sorted(new_state)})

    async def _http_integrations_provision_one(self, request: web.Request) -> web.Response:
        """Provision one Nango integration with user-supplied credentials.

        Branches on auth_mode:
          - OAuth-family → store the developer's client_id/secret as
            integration credentials, mint a Connect session URL for the
            user to approve in their browser.
          - API_KEY / BASIC / TWO_STEP → create the integration with no
            credentials, then POST the user's actual key directly to
            /connection. No browser hop.

        Body: {provider, unique_key?, auth_mode, credentials, connection_config?}
        """
        from sunday.integrations import nango
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        provider = (body.get("provider") or "").strip()
        if not provider:
            return web.json_response({"error": "provider required"}, status=400)
        unique_key = (body.get("unique_key") or provider).strip()
        auth_mode = (body.get("auth_mode") or "").upper()
        credentials = body.get("credentials") or {}
        connection_config = body.get("connection_config") or {}

        oauth_modes = {"OAUTH2", "OAUTH2_CC", "OAUTH1", "APP", "MCP_OAUTH2", "MCP_OAUTH2_GENERIC"}
        if auth_mode in oauth_modes:
            # Pass the developer's app creds (client_id/secret/scopes) as
            # integration credentials. Nango ignores 'type' if we omit it.
            integration_creds = {"type": "OAUTH2", **credentials}
            prov = await nango.provision(provider, unique_key, integration_creds, connection_config)
            if prov.get("error"):
                return web.json_response(prov, status=400)
            session = await nango.create_connect_session_for_key(unique_key)
            if session.get("error"):
                return web.json_response({"provisioned": prov, "session_error": session["error"]}, status=502)
            return web.json_response({"provisioned": prov, "connect_url": session["connect_url"], "flow": "oauth"})

        # Non-OAuth: provision empty integration, then POST the user's
        # credential to /connection directly.
        prov = await nango.provision(provider, unique_key, None, None)
        if prov.get("error"):
            return web.json_response(prov, status=400)
        # Always use the daemon-wide NANGO_CONNECTION_ID so every provider's
        # connection lives under the same handle ("sunday"). That's what the
        # per-provider tool modules (fireflies, google, …) send when proxying
        # — if we generate a different id per provider here, the tools point
        # at a connection that doesn't exist.
        cid = body.get("connection_id") or nango.connection_id()
        conn = await nango.create_connection_direct(unique_key, cid, auth_mode, credentials, connection_config)
        if conn.get("error"):
            return web.json_response({"provisioned": prov, "connection_error": conn["error"]}, status=502)
        return web.json_response({"provisioned": prov, "connected": True, "flow": "direct"})

    async def _http_mcp_get(self, request: web.Request) -> web.Response:
        from sunday import mcp
        return web.json_response({
            "config": mcp.load_config(),
            "servers": list(mcp.STATUS.values()),
        })

    async def _http_mcp_post(self, request: web.Request) -> web.Response:
        from sunday import mcp
        body = await request.json()
        raw = body.get("config")
        try:
            mcp.save_config(raw)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"couldn't parse config: {exc}"}, status=400)
        # Reconnect against the new config (tools land on the next turn).
        try:
            status = await mcp.connect_all(self.registry, self.config)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "servers": list(status.values())})

    async def _http_mcp_builtin_get(self, request: web.Request) -> web.Response:
        from sunday import mcp
        return web.json_response({"connectors": mcp.builtin_status(), "node": mcp.node_available()})

    async def _http_mcp_builtin_post(self, request: web.Request) -> web.Response:
        """One-click enable/disable a built-in connector (e.g. Playwright browser):
        merge it into mcp.json + reconnect so its tools land immediately."""
        from sunday import mcp
        body = await request.json()
        bid, enabled = body.get("id"), bool(body.get("enabled"))
        try:
            mcp.set_builtin(bid, enabled, token=body.get("token"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        try:
            await mcp.connect_all(self.registry, self.config)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True, "connectors": mcp.builtin_status(), "node": mcp.node_available()})

    async def _http_voice_session(self, request: web.Request) -> web.Response:
        """Realtime voice mode (foundation): mint an ephemeral session wired to
        Sunday's system prompt + tools, for the renderer to connect directly to
        the realtime provider. The real API key stays on the daemon."""
        from sunday import realtime
        from sunday.prompt import stable_prefix
        # provider: explicit ?provider= wins, else the configured default
        provider = (request.query.get("provider") or getattr(self.config.voice, "provider", "openai")).lower()
        try:
            if provider == "gemini":
                data = await realtime.create_gemini_session(self.config, self.registry, stable_prefix())
            else:
                data = await realtime.create_openai_session(self.config, self.registry, stable_prefix())
                data = dict(data); data.setdefault("provider", "openai")
            return web.json_response(data)
        except RuntimeError as exc:   # missing key / config — clear 400
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("voice session mint failed")
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_voice_tool(self, request: web.Request) -> web.Response:
        """Execute a Sunday tool the realtime voice model asked for — same
        registry + context the chat loop uses, so voice mode is full Sunday."""
        from sunday.tools import ToolContext
        body = await request.json()
        name, args = body.get("name"), body.get("arguments") or {}
        tool = self.registry.get(name)
        if tool is None:
            return web.json_response({"error": f"unknown tool '{name}'"}, status=404)
        ctx = ToolContext(chat=self.chat, config=self.config, modality="voice", extras={
            "broadcast": self._broadcast, "devices": self.devices, "cockpit": self.cockpit,
            "memory": self.memory,
            "runtime": self.runtime, "registry": self.registry, "active_tools": self._active_tools,
            "inject_and_wake": self._inject_and_wake,
        })
        try:
            return web.json_response({"result": await tool.run(args, ctx)})
        except Exception as exc:  # noqa: BLE001
            log.exception("voice tool failed", tool=name)
            return web.json_response({"error": str(exc)}, status=500)

    async def _http_models(self, request: web.Request) -> web.Response:
        """Proxy + trim OpenRouter's public model catalog (cached ~1h) so the
        app can offer a searchable picker with a 'sees images' flag."""
        import time as _t
        cache = getattr(self, "_models_cache", None)
        if cache and (_t.time() - cache[0] < 3600):
            return web.json_response({"models": cache[1]})
        try:
            import httpx
            async with httpx.AsyncClient(timeout=15) as client:
                res = await client.get("https://openrouter.ai/api/v1/models")
            raw = res.json().get("data", [])
            out = []
            for m in raw:
                arch = m.get("architecture") or {}
                ins = arch.get("input_modalities") or ([arch.get("modality", "")] if arch.get("modality") else [])
                vision = any("image" in str(x).lower() for x in ins)
                pr = m.get("pricing") or {}
                out.append({
                    "id": m.get("id"),
                    "name": m.get("name") or m.get("id"),
                    "vision": vision,
                    "context": m.get("context_length"),
                    "prompt_price": pr.get("prompt"),
                })
            out = [m for m in out if m["id"]]
            self._models_cache = (_t.time(), out)
            return web.json_response({"models": out})
        except Exception as exc:  # noqa: BLE001
            log.warning("models fetch failed", error=str(exc))
            return web.json_response({"models": [], "error": str(exc)})

    # ─── rewind (screen history) — routes to the satellite advertising it ──

    def _rewind_device(self) -> str | None:
        for d in self.devices.list_devices():
            if "rewind" in (d.get("capabilities") or []):
                return d["device_id"]
        return None

    async def _rewind_call(self, method: str, params: dict) -> dict:
        did = self._rewind_device()
        if not did:
            return {"error": "no Mac with screen history connected"}
        try:
            return await self.devices.command(did, method, params, timeout=20)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    async def _http_rewind_recent(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500
        return web.json_response(await self._rewind_call("rewind_recent", {"limit": limit}))

    async def _http_rewind_state(self, request: web.Request) -> web.Response:
        return web.json_response(await self._rewind_call("rewind_stats", {}))

    async def _http_rewind_toggle(self, request: web.Request) -> web.Response:
        body = await request.json()
        on = bool(body.get("on"))
        if on:
            interval = body.get("interval_seconds")
            params = {"interval_seconds": interval} if interval else {}
            return web.json_response(await self._rewind_call("rewind_start", params))
        return web.json_response(await self._rewind_call("rewind_stop", {}))

    async def _http_admin_health(self, request: web.Request) -> web.Response:
        """Rich health snapshot for admin UIs — daemon stats, satellites,
        memory growth, skills, recent tool activity. AUTH-GATED (it includes
        memory content) and on its own route: a later trivial _http_health
        used to shadow this method entirely, which is why the Settings System
        panel rendered empty."""
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
        # WebSocket auth: browsers can't set an Authorization header on the
        # handshake, so the token rides as a ?token= query param instead. The
        # middleware exempts /v1/ws; we enforce it here.
        import hmac
        token = request.query.get("token", "")
        if not token or not hmac.compare_digest(token, get_or_create_auth_token()):
            return web.json_response({"error": "unauthorized"}, status=401)
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

    async def _http_health(self, request: web.Request) -> web.Response:
        # Report the version of the APP that spawned us (SUNDAY_APP_VERSION),
        # not the static package version — that's how the app detects a stale
        # daemon left running across an update and restarts it.
        version = os.environ.get("SUNDAY_APP_VERSION") or __version__
        return web.json_response({"ok": True, "version": version})

    async def _http_auth_check(self, request: web.Request) -> web.Response:
        """Verify a presented token without leaking the real one. Returns
        {ok: true} on match, 401 otherwise. Exempt from the auth middleware
        so unauth'd clients can probe."""
        import hmac
        expected = get_or_create_auth_token()
        body = await request.json() if request.body_exists else {}
        presented = (body.get("token") or "").strip()
        if presented and hmac.compare_digest(presented, expected):
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "invalid token"}, status=401)

    # ─── network / channel setup (server-role config surface) ────────────
    # These power the desktop's server-side Network + Texting settings. They're
    # auth-gated /v1 routes, so only a paired client (or the local app) can read
    # the box's path or rewire Funnel — config lives on the brain, by design.

    async def _http_net_status(self, request: web.Request) -> web.Response:
        """Tailscale reachability + the ready-to-paste Sendblue webhook URL."""
        from sunday.net import tailscale
        from sunday.channels.sendblue import webhook_path, public_webhook_url, account_status
        ts = tailscale.status()
        sb = await account_status()
        return web.json_response({
            "tailscale": ts,
            "port": self.config.server.port,
            "sendblue": {
                **sb,
                "webhook_path": webhook_path(),
                "webhook_url": public_webhook_url(ts.get("dns_name")),
            },
        })

    async def _http_net_configure(self, request: web.Request) -> web.Response:
        """Run `tailscale serve`+`funnel` for just the webhook path, giving the
        private box exactly one public ingress. Once Funnel's up, register that
        URL with Sendblue over its API so the owner never pastes a webhook into
        the dashboard. Returns the URL (manual fallback) + the registration
        result, plus manual commands if the Funnel auto path hit a quirk."""
        from sunday.net import tailscale
        from sunday.channels.sendblue import webhook_path, public_webhook_url, register_receive_webhook
        result = tailscale.configure_funnel(self.config.server.port, webhook_path())
        ts = tailscale.status()
        url = public_webhook_url(ts.get("dns_name"))
        # Funnel's up and we have a public URL — point Sendblue at it over the
        # API. Skips silently (ok:false, benign error) if keys aren't set yet.
        sendblue_webhook = None
        if isinstance(result, dict) and result.get("ok") and url:
            sendblue_webhook = await register_receive_webhook(ts.get("dns_name"))
        return web.json_response({
            "result": result,
            "tailscale": ts,
            "webhook_url": url,
            "sendblue_webhook": sendblue_webhook,
        })

    async def _http_sendblue_register_webhook(self, request: web.Request) -> web.Response:
        """Register (or confirm) Sunday's inbound webhook URL on Sendblue over
        the API — the owner never copies it into the dashboard. Called after the
        keys are saved, in case they're entered once Funnel is already up."""
        from sunday.net import tailscale
        from sunday.channels.sendblue import register_receive_webhook
        ts = tailscale.status()
        result = await register_receive_webhook(ts.get("dns_name"))
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def _http_sendblue_test(self, request: web.Request) -> web.Response:
        """Send a test iMessage so the whole texting round-trip can be proven
        from the desktop: Sunday texts you, you reply, it lands in the one chat."""
        from sunday.channels.sendblue import _send_sendblue, _sendblue_headers, normalize_phone
        if _sendblue_headers() is None:
            return web.json_response({"ok": False, "error": "Sendblue keys not set yet"}, status=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        to = normalize_phone((body.get("to") or "").strip())
        if not to:
            return web.json_response({"ok": False, "error": "enter your phone number"}, status=400)
        msg = "Test from Sunday — texting is wired up. Reply to this and it lands in your chat."
        try:
            result = await _send_sendblue(to, msg)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=502)
        ok = bool(result) and not (isinstance(result, dict) and result.get("error"))
        return web.json_response({"ok": ok, "result": result})

    async def _http_agentmail_status(self, request: web.Request) -> web.Response:
        """Whether Sunday's own email is wired up, for the Email card in Settings.
        Shape mirrors Sendblue/VAPI: {configured, connected, address, error?}."""
        from sunday.channels.agentmail import account_status
        return web.json_response(await account_status())

    async def _http_agentmail_test(self, request: web.Request) -> web.Response:
        """Send a test email so the whole email round-trip can be proven from the
        desktop: Sunday emails you, you reply, it lands in the one chat."""
        from sunday.channels.agentmail import _agentmail_headers, _send_agentmail
        if _agentmail_headers() is None:
            return web.json_response({"ok": False, "error": "AgentMail key not set yet"}, status=400)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        to = (body.get("to") or "").strip()
        if not to:
            return web.json_response({"ok": False, "error": "enter an email address"}, status=400)
        subject = "Test from Sunday"
        text = "Test from Sunday — email is wired up. Reply to this and it lands in your chat."
        try:
            result = await _send_agentmail(to, subject, text)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=502)
        ok = bool(result) and not (isinstance(result, dict) and result.get("error"))
        return web.json_response({"ok": ok, "error": result.get("error") if isinstance(result, dict) else None, "result": result})

    def _build_http_app(self) -> web.Application:
        app = web.Application(middlewares=[_auth_middleware])
        app.router.add_post("/v1/say", self._http_say)
        app.router.add_post("/v1/message/edit", self._http_message_edit)
        app.router.add_post("/v1/task/stop", self._http_task_stop)
        app.router.add_post("/v1/task/steer", self._http_task_steer)
        app.router.add_post("/v1/chat/clear", self._http_chat_clear)
        app.router.add_get("/v1/export", self._http_export)
        app.router.add_get("/v1/log", self._http_log)
        app.router.add_get("/v1/threads", self._http_threads_list)
        app.router.add_post("/v1/threads", self._http_thread_create)
        app.router.add_get("/v1/threads/{id:[0-9]+}", self._http_thread_get)
        app.router.add_post("/v1/threads/{id:[0-9]+}/say", self._http_thread_say)
        app.router.add_get("/v1/status", self._http_status)
        app.router.add_get("/v1/health", self._http_health)
        app.router.add_get("/v1/admin/health", self._http_admin_health)   # rich, auth-gated
        app.router.add_post("/v1/auth/check", self._http_auth_check)
        app.router.add_post("/v1/observer/now", self._http_observer_now)
        app.router.add_post("/v1/observer/tick", self._http_observer_tick)
        app.router.add_get("/v1/observer/log", self._http_observer_log)
        app.router.add_get("/v1/observer/buffer", self._http_observer_buffer)
        app.router.add_post("/v1/meetings/finalize", self._http_meeting_finalize)
        app.router.add_post("/v1/meetings/hud", self._http_meeting_hud)
        app.router.add_post("/v1/meetings/done", self._http_meeting_done)
        app.router.add_post("/v1/meetings/stop-request", self._http_meeting_stop_request)
        app.router.add_get("/v1/interjections", self._http_interjections_latest)
        app.router.add_post("/v1/interjections/{id}/engage", self._http_interjection_engage)
        app.router.add_post("/v1/interjections/{id}/dismiss", self._http_interjection_dismiss)
        app.router.add_get("/v1/atoms", self._http_atoms_list)
        app.router.add_post("/v1/atoms", self._http_atoms_add)
        app.router.add_post("/v1/atoms/{id:[0-9]+}", self._http_atoms_update)
        app.router.add_post("/v1/atoms/wipe", self._http_atoms_wipe)
        app.router.add_get("/v1/conversations", self._http_conversations_list)
        app.router.add_get("/v1/conversations/search", self._http_conversations_search)
        app.router.add_get("/v1/conversations/{id:[0-9]+}", self._http_conversations_get)
        app.router.add_post("/v1/conversations", self._http_conversations_add)
        app.router.add_get("/v1/tools", self._http_tools)
        app.router.add_get("/v1/health", self._http_health)
        app.router.add_get("/v1/config", self._http_get_config)
        app.router.add_post("/v1/config", self._http_post_config)
        app.router.add_get("/v1/net/status", self._http_net_status)
        app.router.add_post("/v1/net/configure", self._http_net_configure)
        app.router.add_post("/v1/channels/sendblue/register-webhook", self._http_sendblue_register_webhook)
        app.router.add_post("/v1/channels/sendblue/test", self._http_sendblue_test)
        app.router.add_get("/v1/channels/agentmail/status", self._http_agentmail_status)
        app.router.add_post("/v1/channels/agentmail/test", self._http_agentmail_test)
        app.router.add_post("/v1/codex/login", self._http_codex_login)
        app.router.add_get("/v1/codex/status", self._http_codex_status)
        app.router.add_get("/v1/gmail/status", self._http_gmail_status)
        app.router.add_get("/v1/cockpit/status", self._http_cockpit_status)
        app.router.add_get("/v1/vapi/status", self._http_vapi_status)
        app.router.add_post("/v1/vapi/test", self._http_vapi_test)
        app.router.add_get("/v1/vapi/calls", self._http_vapi_calls)
        app.router.add_get("/v1/vapi/calls/{id}", self._http_vapi_call_get)
        # Unified Inbox: merged activity feed (spec §4b). Voice rows stay live
        # from VAPI; text/email/webhook rows come from the local activity store.
        app.router.add_get("/v1/inbox", self._http_inbox_list)
        app.router.add_get("/v1/inbox/{id}", self._http_inbox_get)
        app.router.add_get("/v1/ollama/models", self._http_ollama_models)
        app.router.add_get("/v1/local/recommend", self._http_local_recommend)
        app.router.add_post("/v1/ollama/start", self._http_ollama_start)
        app.router.add_post("/v1/ollama/pull", self._http_ollama_pull)
        app.router.add_get("/v1/memory/facts", self._http_memory_facts)
        app.router.add_post("/v1/memory/facts", self._http_memory_facts_add)
        app.router.add_put("/v1/memory/facts/{id:[0-9]+}", self._http_memory_facts_update)
        app.router.add_post("/v1/memory/facts/{id:[0-9]+}", self._http_memory_facts_update)
        app.router.add_delete("/v1/memory/facts/{id:[0-9]+}", self._http_memory_facts_delete)
        app.router.add_get("/v1/memory/graph", self._http_memory_graph)
        app.router.add_post("/v1/memory/graph/rebuild", self._http_memory_graph_rebuild)
        app.router.add_get("/v1/skills", self._http_skills_list)
        app.router.add_post("/v1/skills", self._http_skills_save)
        # Literal sub-paths registered before the {slug} catch-all so the slug
        # regex ([A-Za-z0-9_-]+, which would match "search"/"install") can't
        # shadow them — aiohttp matches in registration order.
        app.router.add_get("/v1/skills/search", self._http_skills_search)
        app.router.add_post("/v1/skills/install", self._http_skills_install)
        app.router.add_get("/v1/skills/{slug:[A-Za-z0-9_-]+}", self._http_skill_get)
        app.router.add_delete("/v1/skills/{slug:[A-Za-z0-9_-]+}", self._http_skill_delete)
        app.router.add_post("/v1/cost/log", self._http_cost_log)
        app.router.add_get("/v1/cost/summary", self._http_cost_summary)
        app.router.add_get("/v1/cost/recent", self._http_cost_recent)
        app.router.add_get("/v1/integrations", self._http_integrations)
        app.router.add_post("/v1/integrations/connect", self._http_integrations_connect)
        app.router.add_post("/v1/integrations/provision", self._http_integrations_provision)
        app.router.add_get("/v1/integrations/catalog", self._http_integrations_catalog)
        app.router.add_get("/v1/integrations/setup/{name}", self._http_integrations_setup)
        app.router.add_post("/v1/integrations/provision_one", self._http_integrations_provision_one)
        app.router.add_get("/v1/connectors", self._http_connectors_get)
        app.router.add_post("/v1/connectors/toggle", self._http_connectors_toggle)
        app.router.add_get("/v1/mcp/registry", self._http_mcp_registry)
        app.router.add_get("/v1/mcp/inspect/{name:.+}", self._http_mcp_inspect)
        app.router.add_post("/v1/mcp/install", self._http_mcp_install)
        app.router.add_post("/v1/mcp/uninstall", self._http_mcp_uninstall)
        app.router.add_get("/v1/mcp/builtin", self._http_mcp_builtin_get)
        app.router.add_post("/v1/mcp/builtin", self._http_mcp_builtin_post)
        app.router.add_post("/v1/voice/session", self._http_voice_session)
        app.router.add_post("/v1/voice/tool", self._http_voice_tool)
        app.router.add_get("/v1/mcp", self._http_mcp_get)
        app.router.add_post("/v1/mcp", self._http_mcp_post)
        app.router.add_get("/v1/models", self._http_models)
        app.router.add_get("/v1/rewind/recent", self._http_rewind_recent)
        app.router.add_get("/v1/rewind/state", self._http_rewind_state)
        app.router.add_post("/v1/rewind/toggle", self._http_rewind_toggle)
        app.router.add_get("/v1/checkin/state", self._http_checkin_state)
        app.router.add_post("/v1/checkin/set", self._http_checkin_set)
        app.router.add_get("/v1/ws", self._ws_handler)
        # Satellite devices connect here.
        app.router.add_get("/v1/devices/ws", self.devices.handle_ws)
        # The Cockpit browser extension connects OUT to here (auth via ?token=
        # against COCKPIT_TOKEN — exempt from the bearer middleware).
        app.router.add_get("/v1/cockpit/ws", self.cockpit.handle_ws)
        app.router.add_get("/v1/cockpit/precheck", self.cockpit.handle_precheck)
        # Catch-all webhook dispatcher — modules register paths in _webhooks.
        # `{name:.*}` so multi-segment secret paths (e.g.
        # /webhooks/sendblue/<secret>) route here; _webhook_dispatch then
        # exact-matches request.path against the registry.
        app.router.add_post("/webhooks/{name:.*}", self._webhook_dispatch)
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

        # Declaratively provision Nango integrations from env-held OAuth
        # client creds (GOOGLE_CLIENT_ID/SECRET, …) — no dashboard clicking.
        async def _provision_integrations():
            try:
                from sunday.integrations import nango
                if nango.configured():
                    result = await nango.provision_from_env()
                    log.info("integrations provisioned", result=result)
            except Exception as exc:  # noqa: BLE001
                log.warning("integration provisioning failed", error=str(exc))
        self._bg_tasks.append(asyncio.create_task(_provision_integrations()))

        # Connect configured MCP servers + register their tools (next turn).
        async def _connect_mcp():
            try:
                from sunday import mcp
                if (mcp.load_config().get("mcpServers") or {}):
                    await mcp.connect_all(self.registry, self.config)
            except Exception as exc:  # noqa: BLE001
                log.warning("mcp connect failed", error=str(exc))
        self._bg_tasks.append(asyncio.create_task(_connect_mcp()))

        # Backfill memory vectors (hybrid recall) — embeds any facts without
        # vectors via whatever LOCAL embedder is up; no-op when none is.
        async def _index_memory():
            try:
                await asyncio.sleep(10)        # let the boot settle first
                await self.memory.index_pending()
            except Exception as exc:  # noqa: BLE001
                log.warning("memory vector backfill failed", error=str(exc))
        self._bg_tasks.append(asyncio.create_task(_index_memory()))

        # Proactive check-in worker. The timer is permission to CONSIDER, not an
        # obligation to send — the heavy lifting (quiet hours, jittered gap,
        # cooldown, back-off, and the quality gate) lives in _maybe_check_in.
        # We tick on a coarse, slightly-irregular cadence so even the
        # *evaluation* isn't clockwork. Fully wrapped: a failure logs and
        # retries next cycle, never crashing the daemon.
        # Proactive check-ins were removed — they felt bolted-on and unintentional.
        # The worker is deliberately not started, so Sunday never reaches out
        # unprompted. (checkin.py + the /v1/checkin endpoints remain as inert
        # dead-code for now; nothing calls the worker.)

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
    # Local eval tracing (Raindrop Workshop). No-op unless RAINDROP_LOCAL_DEBUGGER
    # is set, so prod is unaffected.
    from sunday import tracing
    tracing.init()
    # Only render the banner on attached terminals — keeps systemd /
    # launchd logs clean of ANSI gradient escape sequences.
    import sys as _sys
    if _sys.stdout.isatty():
        from sunday.banner import render as _render
        _render(tagline=f"a personal AI you self-host  ·  v{__version__}")
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
