"""Context compaction — modelled on Hermes's ContextCompressor.

Sunday is one infinite chat. Without compaction the model only ever sees the
last N messages; everything older silently falls off the edge (only whatever
got extracted into the memory DB survives). Hermes (our golden standard,
NousResearch/hermes-agent `agent/context_compressor.py`) solves the same
problem by splitting history into three zones and summarizing the middle:

    head   — protected, never summarized (system + opening framing)
    middle — folded into a structured LLM summary
    tail   — protected by a token budget, kept verbatim

Sunday's adaptation (and where we deliberately diverge from Hermes):

  - System prompt is already a separate, byte-stable prefix (cached), so we
    don't carry a head zone — the personality + operational rules play that
    role and never mutate mid-conversation. Memory recall covers durable
    facts. So Sunday's sent context is:  [summary] + [token-budgeted tail].
  - Hermes compresses *synchronously* inside the loop at 50% context. Sunday
    is conversational (voice / iMessage) where turn latency matters, so we
    fold the middle in the BACKGROUND after a turn (same fire-and-forget
    pattern as memory extraction) and reuse the persisted summary until the
    next fold. The summary is always ready; we never block a reply on it.
  - We keep Hermes's good parts verbatim: structured summary template
    (Goal / Decisions / Progress / Open threads / Key context), update-don't-
    redo on re-compression, user-message boundary alignment so tool-call
    pairs are never split, and a token-budgeted tail.

Two layers, distinct jobs (same split Hermes draws):
  - memory.py  → durable FACTS about the user (persist forever, recalled).
  - compaction → the THREAD of the conversation so the model isn't amnesiac
                 past the tail.

State (summary text + the id we've summarized through) lives in
~/.sunday/conversation.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.paths import sunday_home

log = structlog.get_logger("sunday.compaction")

# --- tail sizing (token-budgeted, like Hermes's protect-last) --------------
# Rough char→token ratio. Hermes calls this estimate_messages_tokens_rough();
# ~4 chars/token is the standard fallback when API usage isn't available.
CHARS_PER_TOKEN = 4
TAIL_TOKEN_BUDGET = 6000      # recent messages kept verbatim each turn
TAIL_MAX_MESSAGES = 80        # hard ceiling so a few huge messages can't blow up

# --- folding (summarizing the middle) --------------------------------------
MIN_FOLD = 12                 # don't summarize until at least this many aged out
FOLD_BATCH_MAX = 120          # cap per fold; a cold backlog catches up over a few turns
SUMMARY_MAX_CHARS = 6000

# Structured summary, ported from Hermes's compaction prompt. Adapted from a
# pure task framing to Sunday's conversational one — it isn't always a "task".
_SYSTEM = """You maintain Sunday's running summary of her conversation with the user. New messages have aged out of the live window; fold them into the existing summary so continuity holds.

Update the existing summary in place — do not start from scratch and do not lose anything load-bearing that's still relevant. Drop small talk, resolved tangents, and anything already captured.

Write tight prose under these headings (omit a heading if empty):

What we're working on — the current thread(s), goals, what you want.
Decisions & preferences — choices made, opinions stated, how you like things done.
Done — what's finished or established.
Open threads — unfinished work, things waiting, questions not yet answered.
Key context — names, projects, files, links, constraints worth remembering. ALWAYS preserve the user's name(s) — their primary name and any alternate names that show up in their own messages or apps (e.g. workspace display names like "Manuel David"). These anchor "this person == the user" across surfaces.

Voice: second person ("you", "your") when referring to the user — this is Sunday's summary FOR HERSELF, not a third-party report. Be specific (names, files, numbers, URLs) over vague. Under 400 words total. Output only the summary."""


def estimate_tokens(text: str) -> int:
    return (len(text or "") // CHARS_PER_TOKEN) + 1


def _state_path() -> Path:
    return sunday_home() / "conversation.json"


def load_state() -> dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"summary": "", "through_id": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"summary": "", "through_id": 0}


def _save_state(summary: str, through_id: int) -> None:
    _state_path().write_text(
        json.dumps({"summary": summary, "through_id": through_id}),
        encoding="utf-8",
    )


def reset_state() -> None:
    """Forget the rolling summary — used when the conversation is cleared so a
    fresh chat doesn't inherit the old thread's summary."""
    _save_state("", 0)


def summary_block() -> str:
    """The rolling summary as a prependable context block (empty until one
    exists). Prepended to the latest user message — never to the system
    prompt — so the cached system prefix stays byte-stable, exactly like
    Hermes keeps breakpoint-1 immutable."""
    s = (load_state().get("summary") or "").strip()
    return f"Summary of earlier in our conversation (for continuity):\n{s}" if s else ""


def tail_messages(chat) -> list:
    """The protected tail: the most recent messages that fit TAIL_TOKEN_BUDGET,
    aligned forward to start on a user message so we never hand a provider an
    orphan tool result or a tool_call whose result we dropped (Hermes's
    _align_boundary_backward, done as a stricter user-boundary cut).

    Returns Message objects, oldest first. Everything older is the 'middle'
    that compaction folds into the summary."""
    recent = chat.recent(limit=TAIL_MAX_MESSAGES)
    if not recent:
        return []

    # Walk backward accumulating until the token budget is spent.
    budget = TAIL_TOKEN_BUDGET
    start = len(recent)  # index of oldest kept (exclusive walk leftward)
    for i in range(len(recent) - 1, -1, -1):
        budget -= estimate_tokens(recent[i].content)
        start = i
        if budget <= 0:
            break

    # Boundary alignment: a valid sequence must begin at a user turn. Align
    # BACKWARD — to the user message that STARTS the turn the budget cut landed
    # in — not forward to the next one. Forward alignment drops a whole recent
    # assistant turn when the cut lands inside it (e.g. a 14-iteration tool turn
    # with no user message and big tool blobs that eat the budget). That strips
    # the assistant's OWN just-produced conclusion from the next turn's context —
    # the agent gives you guest emails right after it said it lacked them and has
    # no memory of the event it just made. Backward alignment keeps the most
    # recent turn whole (worst case ~one extra turn over budget, which is fine).
    for i in range(start, -1, -1):
        if recent[i].role == "user":
            return recent[i:]
    # No user message at all in the recent window — return what we budgeted.
    return recent[start:]


async def maybe_compact(chat, config: SundayConfig) -> None:
    """Fold messages that have aged out of the tail into the running summary.

    Cheap + idempotent: finds the gap between what we've already summarized
    (through_id) and where the live tail now begins, and if enough has
    accumulated there, summarizes it (updating the prior summary, not redoing
    it). Safe to fire-and-forget after every turn — it no-ops until a real
    batch exists."""
    state = load_state()
    through_id = int(state.get("through_id") or 0)

    tail = tail_messages(chat)
    if not tail:
        return
    # Hold off compacting while a tool-call window is hot. Compacting the
    # middle while the model is mid-task chops identity anchors and tool
    # results out of context exactly when they matter most. If the most
    # recent message is a tool result or its assistant author is within the
    # last 60s, defer — wait for the conversation to breathe.
    import time as _t
    last = tail[-1]
    if last.role == "tool":
        return
    if (_t.time() - (last.created_at or 0)) < 60:
        return
    tail_start_id = tail[0].id  # everything strictly below this is the middle
    batch = chat.range(through_id, tail_start_id - 1, limit=FOLD_BATCH_MAX)
    if len(batch) < MIN_FOLD:
        return

    transcript = "\n".join(
        f"{('Sunday' if m.role == 'sunday' else m.role)}: {(m.content or '').strip()[:800]}"
        for m in batch
        if (m.content or "").strip()
    )
    if not transcript:
        # Nothing summarizable (e.g. all tool-result rows) — still advance the
        # cursor so we don't re-scan this range forever.
        _save_state(state.get("summary", ""), batch[-1].id)
        return

    prior = (state.get("summary") or "").strip()
    user = (
        (f"Existing summary:\n{prior}\n\n" if prior else "")
        + f"New messages to fold in:\n{transcript}\n\nUpdated summary:"
    )
    try:
        from sunday.runtime import build_runtime

        rt = build_runtime(config)
        result = await rt.complete(
            system_prompt=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            tools_schema=None,
            purpose="compaction",
        )
        new_summary = (result.content or "").strip()[:SUMMARY_MAX_CHARS]
        if new_summary:
            _save_state(new_summary, batch[-1].id)
            log.info(
                "conversation compacted",
                folded=len(batch),
                through_id=batch[-1].id,
                tail_start_id=tail_start_id,
                summary_chars=len(new_summary),
            )
            # Piggyback the graph rebuild here: we just paid for one LLM call
            # because the conversation moved meaningfully, so this is a sane
            # cadence for refreshing the derived knowledge graph too. The
            # `needs_rebuild()` short-circuit means we only do real work when
            # new facts have actually landed since the last build — most
            # compactions will skip the second call entirely.
            try:
                from sunday import memory_graph
                if memory_graph.needs_rebuild():
                    # Incremental: only the facts added since last pass get an
                    # LLM call + merged in. No full re-extraction of the store.
                    await memory_graph.ingest(config)
                    log.info("memory graph refreshed via compaction")
            except Exception as graph_exc:  # noqa: BLE001
                log.warning("graph refresh during compaction failed", error=str(graph_exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("compaction failed", error=str(exc))
