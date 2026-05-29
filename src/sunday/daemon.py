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
        self._unix_server: asyncio.Server | None = None
        self._http_runner: web.AppRunner | None = None
        self._ws_clients: set[web.WebSocketResponse] = set()
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
        # Interjections — Sunday's proactive notes + (later) nudges. Shared
        # substrate; the consumer is the notch flare. Engagement converts to
        # a real chat message; dismissal extends cooldown.
        from sunday.interjections import InterjectionStore
        self.interjections = InterjectionStore()
        # Observer conversation buffer — transcripts accumulate here across
        # ticks; on a run of silent ticks we close + summarize the window.
        # (Capture happens in Sunday.app on the Mac; transcripts arrive via
        # /v1/observer/tick.)
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
        # Serialize turns: a user turn and a sub-agent wake turn must not run
        # concurrently or they'd interleave messages in the single chat.
        async with self._turn_lock:
            reply = await self._run_turn(text, modality, attachments=attachments)
        return {"reply": reply}

    async def _run_turn(
        self,
        text: str,
        modality: str,
        attachments: list[dict] | None = None,
        user_metadata: dict[str, Any] | None = None,
    ) -> str:
        """One agent turn: drive the loop, broadcast the reply, kick off the
        background memory + compaction passes. Caller MUST hold _turn_lock."""
        reply = await respond(
            self.chat, text, modality, self.config, self.registry,
            runtime=self.runtime,
            attachments=attachments,
            user_metadata=user_metadata,
            extras={
                "broadcast": self._broadcast,
                "devices":   self.devices,
                "memory":    self.memory,
                "runtime":       self.runtime,
                # tiered tools: lean core + whatever find_tools has pulled in
                # this session (persists across turns until the daemon restarts)
                "registry":      self.registry,
                "active_tools":  self._active_tools,
                # async sub-agents: tools hand a finished result back here to
                # be injected as a hidden message + folded in on a wake turn.
                "inject_and_wake": self._inject_and_wake,
            },
        )
        await self._broadcast({"type": "reply", "modality": modality, "content": reply})
        # Fire-and-forget memory extraction so the brain returns immediately.
        if self.memory.available:
            asyncio.create_task(self._extract_memories(text, reply))
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
        try:
            from sunday.compaction import maybe_compact
            await maybe_compact(self.chat, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("compaction task failed", error=str(exc))

    async def _extract_memories(self, user_text: str, sunday_reply: str) -> None:
        try:
            facts = await extract_facts(user_text, sunday_reply, self.config)
            if facts:
                await self.memory.store_many(facts, source="auto")
                log.info("memory extracted", new_facts=len(facts))
                # NB: the memory graph used to be rebuilt here on every turn.
                # That paid for two LLM calls per turn and an O(n) rebuild over
                # all facts. Graph refresh now piggybacks compaction (see
                # sunday/compaction.py), which already fires an LLM call when
                # the conversation has moved enough — natural cadence, no
                # extra spend. The /v1/memory/graph endpoint still rebuilds
                # on-demand if the UI explicitly asks for a fresh view.
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

    async def _http_log(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        return web.json_response(
            {"messages": [m.to_json() for m in self.chat.recent(limit=limit)
                          if not (m.metadata or {}).get("hidden")]}
        )

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
        return web.json_response({"conversations": self.conversations.list(
            limit=limit,
            since=float(since) if since else None,
            category=request.query.get("category"),
        )})

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
            asyncio.create_task(self._say(reply, "proac",
                                          user_metadata={"proac_reply_to": iid}))

        return web.json_response({"ok": True, "engaged": True, "ran_turn": bool(reply)})

    async def _http_interjection_dismiss(self, request: web.Request) -> web.Response:
        try:
            iid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return web.json_response({"error": "bad id"}, status=400)
        self.interjections.mark_dismissed(iid)
        return web.json_response({"ok": True})

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
        try:
            summary = await obs.summarize_conversation(transcript, self.config)
        except Exception as exc:  # noqa: BLE001
            log.warning("observer conversation summary failed", error=str(exc))
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

        return web.json_response({"applied": applied, "ok": True})

    async def _http_memory_facts(self, request: web.Request) -> web.Response:
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500
        rows = self.memory.all(limit=limit) if self.memory.available else []
        return web.json_response({
            "available": self.memory.available,
            "facts": [
                {"id": r.id, "content": r.content, "source": r.source, "created_at": r.created_at}
                for r in rows
            ],
        })

    async def _http_memory_graph(self, request: web.Request) -> web.Response:
        from sunday import memory_graph
        try:
            # Build lazily if facts exist but the graph hasn't been built yet.
            if memory_graph.needs_rebuild():
                await memory_graph.rebuild(self.config)
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
        app.router.add_post("/v1/observer/now", self._http_observer_now)
        app.router.add_post("/v1/observer/tick", self._http_observer_tick)
        app.router.add_get("/v1/observer/log", self._http_observer_log)
        app.router.add_get("/v1/observer/buffer", self._http_observer_buffer)
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
        app.router.add_get("/v1/memory/facts", self._http_memory_facts)
        app.router.add_get("/v1/memory/graph", self._http_memory_graph)
        app.router.add_post("/v1/memory/graph/rebuild", self._http_memory_graph_rebuild)
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
        app.router.add_get("/v1/mcp", self._http_mcp_get)
        app.router.add_post("/v1/mcp", self._http_mcp_post)
        app.router.add_get("/v1/models", self._http_models)
        app.router.add_get("/v1/rewind/recent", self._http_rewind_recent)
        app.router.add_get("/v1/rewind/state", self._http_rewind_state)
        app.router.add_post("/v1/rewind/toggle", self._http_rewind_toggle)
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
