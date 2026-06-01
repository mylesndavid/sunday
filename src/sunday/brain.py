"""Sunday's brain — chat-completion + tool-call loop.

The harness owns the loop. The Runtime owns the LLM call. They cooperate:
the harness keeps appending messages to the chat, calls Runtime.complete,
dispatches any tool calls, and repeats until the model returns a plain reply.

The runtime can be Hermes (subprocess, dcharness-style) or direct
OpenAI-compatible (DeepSeek by default). Same loop either way.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

import structlog

from sunday.chat import Chat
from sunday.compaction import summary_block as conversation_summary_block
from sunday.compaction import tail_messages
from sunday.config import SundayConfig
from sunday.prompt import stable_prefix
from sunday.runtime import (
    IterationBudget,
    Runtime,
    build_runtime,
    repair_message_sequence,
    repair_tool_call_arguments,
    sanitize_messages_surrogates,
)
from sunday.tools import ToolContext, ToolRegistry

# How much recent history goes to the model is now token-budgeted, not a
# fixed message count — see sunday.compaction.tail_messages (TAIL_TOKEN_BUDGET).
# Per-turn iteration budget. Same shape as Hermes's --max-iterations.
# Raised 30→60: the agent reported getting clipped mid-flow on genuine
# multi-step browser/research work. Still bounded so a runaway loop bails,
# but heavy grunt work should go to a sub-agent (its own budget) via delegate.
MAX_TOOL_ITERATIONS = 60

# Cap how much of a single tool result enters the context. One huge result
# (e.g. agentos_integration_list came back 91k chars / ~23k tokens) otherwise
# blows the whole token-budgeted tail — evicting the user's actual question —
# and the model, left with only a giant dump, produces garbage.
#
# Modelled on Hermes's tool_output cap, scaled to Sunday's smaller tail: when a
# result exceeds the cap, keep the first 40% AND the last 60% (the end of a
# result usually holds the answer/error/summary) with a marker between, rather
# than truncating head-only. ~8k chars (~2k tokens) fits Sunday's 6k-token tail
# with room for the question. (Hermes defaults to 50k chars for big-context
# models — too large here.)
MAX_TOOL_RESULT_CHARS = 8000

# Tools that have no shared mutable state and are safe to run concurrently
# in the same iteration. Port of Hermes's _PARALLEL_SAFE_TOOLS list
# (run_agent.py:340), adapted to Sunday's tool surface.
PARALLEL_SAFE_TOOLS = frozenset({
    # Reads
    "recall", "list_skills", "load_skill",
    "imessage_list_threads", "imessage_read_thread", "imessage_read_recent", "imessage_search",
    "browser_markdown", "browser_screenshot", "browser_scrape",
    "device_screenshot",
    # Pure: no side effects on Sunday's state
    "delegate", "delegate_to_hermes",
})

log = structlog.get_logger("sunday.brain")


def _context_messages(chat: Chat, memory_block: str = "") -> list[dict]:
    """Build the messages list for the next provider call.

    The sent context is [rolling summary] + [token-budgeted tail] — the
    Hermes-style compaction shape (see sunday.compaction). The tail is the
    recent messages that fit the token budget, boundary-aligned to a user
    turn; everything older is represented by the rolling summary, which gets
    folded in the background by compaction.maybe_compact.

    Both context blocks — the conversation summary AND the memory recall — are
    prepended to the LATEST user message, never injected into the system
    prompt. That keeps the system prefix byte-stable across turns so providers'
    prompt cache fires on every turn after the first (60-90% cheaper + faster
    TTFT), exactly like Hermes keeps its breakpoint-1 system prompt immutable.

    Same defensive belts Hermes runs before every API call follow: surrogate
    sanitization + role-alternation repair. Both are no-ops on healthy
    histories and silent fixes when something's off.
    """
    messages = [m.to_llm() for m in tail_messages(chat)]

    # Compose the prepended context block: the rolling conversation summary
    # (the THREAD) on top, then memory recall (durable FACTS), then the
    # user's actual words. The summary belongs to THE chat — subagents run on
    # ephemeral in-memory chats and must not inherit the main thread's
    # summary, so only the persistent canonical chat gets it.
    from sunday.paths import db_path
    summary = conversation_summary_block() if chat.path == db_path() else ""
    prefix_parts = [summary, memory_block]
    prefix = "\n\n".join(p for p in prefix_parts if p).strip()

    if prefix:
        # Sunday's running summary + recalled memory are HER OWN context, not
        # the user's words. Glomming them onto the latest user message (the old
        # behavior) made the model read its own second-person summary as user
        # input — hence "that summary vomit was meant for me, not you." Carry
        # them as a distinct, clearly-framed system message at the top instead.
        # Bonus: this block is stable until the summary re-folds, so it caches
        # better than re-prepending to a fresh user turn every time.
        messages.insert(0, {
            "role": "system",
            "content": (
                "Sunday's own working context for this conversation — your running "
                "summary and recalled memory about the user. This is NOT a message "
                "from the user: don't reply to it, quote it, or repeat it back. Use "
                "it silently for continuity.\n\n" + prefix
            ),
        })

    if sanitize_messages_surrogates(messages):
        log.info("sanitized surrogate code points in messages")
    repaired = repair_message_sequence(messages)
    if repaired:
        log.info("repaired role-alternation violations", count=repaired)
    return messages


async def respond(
    chat: Chat,
    user_text: str,
    modality: str,
    config: SundayConfig,
    registry: ToolRegistry | None = None,
    runtime: Runtime | None = None,
    attachments: list[dict] | None = None,
    extras: dict | None = None,
    system_prompt: str | None = None,
    user_metadata: dict | None = None,
    timings: dict | None = None,
    max_iterations: int | None = None,
) -> str:
    """Take a user message, drive the tool-call loop, return the final reply.

    `attachments` is a list of Attachment-shaped dicts (see sunday.attachments)
    that get stored on the user message metadata and forwarded to the model
    via Message.to_llm()'s multipart handling.

    `extras` is forwarded to ToolContext — the daemon uses this to hand tools
    a broadcast callback (live-view, Electron events, etc.).

    `user_metadata` merges extra fields onto the stored user message — used to
    mark injected, model-only messages (e.g. a sub-agent result) hidden from
    the UI while the model still sees them as ordinary user input.
    """
    user_meta: dict = dict(user_metadata or {})
    if attachments:
        user_meta["attachments"] = attachments
    chat.append("user", user_text, modality, metadata=user_meta or None)

    rt = runtime or build_runtime(config)
    ctx = ToolContext(chat=chat, config=config, modality=modality, extras=extras or {})

    # Tiered tools: when the caller opts in (extras['active_tools'] is a set),
    # send only the lean core + whatever find_tools has activated — keeps the
    # per-turn schema small no matter how many MCP servers are connected.
    # Subagents / other callers (no active_tools) get the full schema.
    from sunday.tools import CORE_TOOLS
    from sunday import connectors as _connectors
    _active = (extras or {}).get("active_tools")

    def _schema():
        if not (registry and registry.list_tools()):
            return None
        if _active is None:
            return registry.as_openai_schema()
        # Connector toggles: every provider the user has pinned in
        # Settings → Connections has its tools promoted into the always-on
        # set, alongside CORE_TOOLS and whatever find_tools has surfaced
        # this turn. Re-read each turn — cheap, and lets a flip take effect
        # on the very next message without restart.
        pinned = _connectors.active_tool_names(registry)
        return registry.as_openai_schema(names=set(CORE_TOOLS) | set(_active) | pinned)

    tool_schema = _schema()

    broadcast = (extras or {}).get("broadcast")
    memory    = (extras or {}).get("memory")
    # Steer/stop: the daemon hands us a TurnControl so the user can grab the
    # wheel mid-task. We check it at each step boundary — steering messages get
    # appended to the chat (so the next provider call sees them like any user
    # turn), and a stop request breaks the loop cleanly with what we have.
    control   = (extras or {}).get("control")
    stream_id = uuid.uuid4().hex[:12]

    # Pull relevant memories ONCE per turn (not per tool iteration). The
    # block gets PREPENDED to the user's latest message inside
    # _context_messages — NOT injected into the system prompt — so the
    # system prefix stays byte-stable across turns and providers' prompt
    # caches actually fire.
    # Phase timing — populated into `timings` (if provided) so callers
    # (sendblue, spikes) can see exactly where a turn spends its wall-clock.
    T = timings if timings is not None else {}
    t_turn0 = time.perf_counter()
    T.setdefault("llm_calls_ms", [])
    T.setdefault("tools_ms", 0.0)
    T["memory_ms"] = 0.0

    # Always tell the model the current date/time so it never burns a tool
    # call (shell `date`, etc.) just to find out what day it is. Computed once
    # per turn and carried in the per-turn context block (not the cached
    # system prompt). UTC — the model converts to the user's tz from memory.
    now_line = "Right now it's " + datetime.now(timezone.utc).strftime("%A, %B %d, %Y, %H:%M UTC") + "."

    memory_block = now_line
    if memory is not None and getattr(memory, "available", False):
        _t = time.perf_counter()
        try:
            # Always-in-context core: all durable facts, read locally (no
            # network, no embedding). The agent sees everything and uses what
            # fits — see sunday.memory for why this beats per-turn retrieval.
            core = memory.core_block()
            if core:
                memory_block = now_line + "\n\n" + core
            T["memory_ms"] = (time.perf_counter() - _t) * 1000
            log.info("memory core injected", facts=memory.count(), ms=round(T["memory_ms"]))
        except Exception as exc:  # noqa: BLE001
            T["memory_ms"] = (time.perf_counter() - _t) * 1000
            log.warning("memory core block failed", error=str(exc))

    async def _emit_delta(piece: str) -> None:
        if broadcast is not None:
            await broadcast({
                "type": "stream_delta",
                "stream_id": stream_id,
                "modality": modality,
                "content": piece,
            })

    async def _emit_reasoning(piece: str) -> None:
        # Live "thinking" feed — streams the model's reasoning tokens as they
        # arrive so the user sees activity during a slow turn.
        if broadcast is not None:
            await broadcast({
                "type": "reasoning_delta",
                "stream_id": stream_id,
                "modality": modality,
                "content": piece,
            })

    if broadcast is not None:
        await broadcast({
            "type": "stream_start",
            "stream_id": stream_id,
            "modality": modality,
        })

    # Local eval tracing — opens an interaction for this turn (inert in prod).
    from sunday import tracing
    _trace = tracing.begin_turn(user_text, modality)

    budget = IterationBudget(max_iterations or MAX_TOOL_ITERATIONS)
    iteration = 0
    while budget.consume():
        iteration += 1
        # Steer/stop check at the step boundary (between tool calls).
        if control is not None:
            for steer_text in control.drain_steering():
                # Land it in the log as a user turn so the next provider call
                # picks it up via _context_messages — no manual splicing.
                chat.append("user", steer_text, modality, metadata={"steer": True})
                log.info("turn steered", text=steer_text[:120])
                if broadcast is not None:
                    await broadcast({"type": "steered", "stream_id": stream_id, "text": steer_text})
            if control.should_stop():
                stopped = "Stopped — tell me how you want to pick it back up."
                chat.append("sunday", stopped, modality, metadata={"stopped": True, "budget_used": budget.used})
                log.info("turn stopped by user", iteration=iteration)
                tracing.finish_turn(_trace, stopped)
                if broadcast is not None:
                    await broadcast({"type": "stream_end", "stream_id": stream_id,
                                     "modality": modality, "content_full": stopped, "stopped": True})
                return stopped
        # Step callback — observability event before each provider call.
        # Lets the Electron app render "thinking", "calling tool X", etc.
        if broadcast is not None:
            await broadcast({
                "type": "agent_step",
                "stream_id": stream_id,
                "iteration": iteration,
                "budget_used": budget.used,
                "budget_max": budget.max_total,
            })

        _t_llm = time.perf_counter()
        result = await rt.complete(
            system_prompt=system_prompt or stable_prefix(),
            messages=_context_messages(chat, memory_block=memory_block),
            tools_schema=_schema(),   # rebuilt each iteration — find_tools grows it mid-turn
            on_delta=_emit_delta,
            on_reasoning=_emit_reasoning,
            purpose="chat",
        )
        T["llm_calls_ms"].append(round((time.perf_counter() - _t_llm) * 1000))

        if result.tool_calls:
            # Repair any malformed JSON argument blobs before persisting +
            # dispatching — covers models that emit trailing commas,
            # unclosed structures, unescaped control chars, etc.
            sanitized = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": repair_tool_call_arguments(tc.arguments, tc.name),
                }
                for tc in result.tool_calls
            ]
            meta: dict = {"runtime": rt.name, "tool_calls": sanitized}
            # Preserve reasoning content across turns when the model emitted it.
            if result.raw.get("reasoning_content"):
                meta["reasoning_content"] = result.raw["reasoning_content"]
            chat.append(
                "sunday",
                result.content,
                modality,
                metadata=meta,
            )

            assert registry is not None

            async def _execute_one(tc):
                if broadcast is not None:
                    await broadcast({
                        "type": "tool_call",
                        "stream_id": stream_id,
                        "tool_name": tc["name"],
                        "tool_call_id": tc["id"],
                        "args_preview": (tc["arguments"] or "")[:200],
                    })
                with tracing.tool_span(tc["name"]):
                    tool_result = await registry.execute(tc["name"], tc["arguments"], ctx)
                content = (
                    tool_result
                    if isinstance(tool_result, str)
                    else json.dumps(tool_result, default=str)
                )
                if broadcast is not None:
                    await broadcast({
                        "type": "tool_result",
                        "stream_id": stream_id,
                        "tool_call_id": tc["id"],
                        "tool_name": tc["name"],
                    })
                return tc, content

            # Parallel-safe batch: gather. Otherwise: serial.
            # We persist tool results to chat in the same order the model
            # called them, regardless of completion order, so the next
            # iteration sees them in a deterministic position.
            _t_tools = time.perf_counter()
            if all(tc["name"] in PARALLEL_SAFE_TOOLS for tc in sanitized) and len(sanitized) > 1:
                log.info("parallel tool batch", count=len(sanitized), names=[tc["name"] for tc in sanitized])
                results = await asyncio.gather(*(_execute_one(tc) for tc in sanitized))
            else:
                results = []
                for tc in sanitized:
                    results.append(await _execute_one(tc))
            T["tools_ms"] += round((time.perf_counter() - _t_tools) * 1000)
            T.setdefault("tool_names", []).extend(tc["name"] for tc in sanitized)

            for tc, content in results:
                if len(content) > MAX_TOOL_RESULT_CHARS:
                    head = MAX_TOOL_RESULT_CHARS * 4 // 10   # first 40%
                    tail = MAX_TOOL_RESULT_CHARS - head      # last 60% (where results/errors usually are)
                    dropped = len(content) - MAX_TOOL_RESULT_CHARS
                    content = (
                        content[:head]
                        + f"\n\n…[tool output truncated — {dropped} chars omitted from the middle; "
                          f"re-run with a narrower query/filter if you need it]…\n\n"
                        + content[-tail:]
                    )
                chat.append(
                    "tool",
                    content,
                    modality,
                    metadata={"tool_call_id": tc["id"], "tool_name": tc["name"]},
                )
            continue

        reply = result.content.strip()
        final_meta: dict = {"runtime": rt.name}
        if result.raw.get("reasoning_content"):
            final_meta["reasoning_content"] = result.raw["reasoning_content"]
        chat.append("sunday", reply, modality, metadata=final_meta)
        T["total_ms"] = round((time.perf_counter() - t_turn0) * 1000)
        T["iterations"] = iteration
        log.info(
            "turn timing",
            total_ms=T["total_ms"], memory_ms=round(T["memory_ms"]),
            llm_calls_ms=T["llm_calls_ms"], tools_ms=round(T["tools_ms"]),
            iterations=iteration, tools=T.get("tool_names", []),
        )
        if broadcast is not None:
            await broadcast({
                "type": "stream_end",
                "stream_id": stream_id,
                "modality": modality,
                "content_full": reply,
            })
        tracing.finish_turn(_trace, reply)
        return reply

    truncated = "I hit my tool-call ceiling. Let me know if you want me to keep going."
    chat.append("sunday", truncated, modality, metadata={"truncated": True, "budget_used": budget.used})
    log.warning("tool loop ceiling reached", budget_used=budget.used, budget_max=budget.max_total)
    if broadcast is not None:
        await broadcast({
            "type": "stream_end",
            "stream_id": stream_id,
            "modality": modality,
            "content_full": truncated,
        })
    tracing.finish_turn(_trace, truncated)
    return truncated
