"""Observer brain — daemon-side tick + conversation processing.

The capture (mic → transcript) happens in Sunday.app on the user's Mac so the
macOS permission prompt is attributed to Sunday, not a detached Python child.
The Mac sends each ~30s transcript here; this module turns transcripts into
atoms + a "now" line, and closes conversations on silence.

This is the brain that used to live in electron/build/observer.py's TICK_SYSTEM
loop — moved server-side so (a) the LLM key stays on the daemon and (b) the
atom/conversation stores it writes to are local to it.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from sunday.config import SundayConfig

log = structlog.get_logger("sunday.observer")


TICK_SYSTEM = """You are the observation engine. From recent speech (and, when available, recent activity), maintain TWO distinct stores:

  WORKING (live; decays; can be reinforced; eligible for nudges)
    Kinds: commitment, thread, deadline.

  REFERENCE (immutable; never decays; never in active count; surfaced on request)
    Kinds: decision, fact.

Atomicity (most important rule)
  Each atom is ONE action with ONE completion signal. Multiple actions → SEPARATE atoms. "Review the spec, figure out billing, build a dashboard" → THREE atoms. If you can't write one sentence describing what makes it done, split.

Discriminating commitment from thread (the fuzzy line)
  • commitment — single owner, single observable completion signal. "I'll send the deck Friday."
  • thread     — ongoing multi-party topic with no single done-state. "We've been going back and forth with Manuel about infra." Threads NEVER complete — they only decay (stale → dropped).
  • deadline   — like commitment but with an explicit date.

Ownership (required, never null)
  "you" for first person, a name when someone else committed, "unclear" ONLY when you genuinely can't tell. Unclear-owner working atoms are HELD: no nudges, faster decay, promoted to "you" or a name if a later observation clarifies.

Completion signal
  REQUIRED for commitment and deadline. A concrete observable the system could plausibly see ("Slack DM to Manuel", "PR opened", "calendar event created"). OMIT for thread and REFERENCE.

Text is immutable.
  Once an atom exists, its text doesn't change. Circumstances changed ("actually push to next week")? CREATE a new atom + emit supersede on the old (action=superseded, state→superseded, superseded_by → the new atom or "new:<index>"). Never edit.

Per tick, for each open WORKING atom decide one of:
  • REINFORCED  — current activity is consistent with active work on this atom. Resets decay. State unchanged.
  • CLOSED      — completion signal observed. state → completed. Evidence + confidence REQUIRED.
  • DROPPED     — user abandoned. state → dropped. Evidence + confidence REQUIRED.
  • SUPERSEDED  — replaced by a new atom. state → superseded, superseded_by → new id (or "new:<index>"). Evidence REQUIRED.
  • (no change) — no signal either way.

Confidence (required on closed/dropped/superseded; 0.0–1.0)
  ≥ 0.85: strong direct signal. 0.6–0.85: plausible inference. < 0.6: do NOT close.

You also produce the "now" line for the hub:
  • "now": one short present-tense line, 4–10 words, describing the CURRENT activity. If there is no clear activity — silence, ambient noise, filler — return "" (empty string). NEVER invent an "idle" label; absence is an empty string.

Continuity (CRITICAL — this is the #1 failure mode):
  You are given your PREVIOUS `now` line. A person's real activity changes SLOWLY — over many minutes, not every 30 seconds. Default HARD to continuing the previous line: set same_as_last=true and repeat the previous `now` verbatim. Only emit a NEW `now` when there is a clear, sustained shift in what THEY are actually doing.

  Media is ONE activity, not many, with a GENERIC label. Default to plain "Watching something" or "Listening to something". Be more specific only when the user has clearly chosen and stayed with one thing (then "Watching a YouTube video", "Watching a show"). NEVER label media by its content topic ("Watching vaccine info in multiple languages", "Watching a settings tutorial", "Moving a patient") — that just narrates whatever shoutiest dialogue arrived in this 30s chunk, not what the user is doing. The user's activity is "watching", not "vaccine info".

  Telltale media signs: dialogue that jumps scene-to-scene, multiple characters, a narrative/scripted feel, instructional voiceover, dubbing/subtitles in multiple languages, references that don't involve the user, sentences that name the user-as-character in third person ("I'm going to grab the other leg" said by an unnamed character is TV, not the user).

  When the activity is media, produce NO new_atoms and NO atom_updates from its content. A character on screen saying "my doctor says I can't drive for a week" is NOT the user's commitment — it's dialogue. Atoms come only from the USER's own real speech about their own life, never from media they're consuming.

Proactive interjection (rare; high bar):
  If the user just expressed a clear knowledge gap — "I don't even know what X is", "what is X?", "I forget what X means", "never heard of X" — and X is a real term/person/concept Sunday could plausibly answer in one sentence, emit a `proac` block. The bar is HIGH: clear gap, real-life context (not a media character asking), short factual answer plausible. Otherwise `proac: null`.

  proac: {
    "trigger": "knowledge_gap",
    "evidence": "exact quoted phrase from transcript that warranted the interjection",
    "ask":      "the question to answer, phrased to a researcher (e.g. 'what is GPC in software/business context')",
    "confidence": 0.0–1.0   // ≥0.85 to actually fire
  }

  Never proac during media. Never proac on rhetorical questions ("what even is life"). Never proac more than once per genuine gap.

  • "same_as_last": TRUE whenever this continues the previous activity (the common case). FALSE only on a genuine, sustained shift.

Be calm and sparing. Chit-chat, pleasantries, filler → no atoms. Silence/noise → empty arrays.

Return ONLY JSON:
{
  "now": "...",
  "same_as_last": true|false,
  "atom_updates": [{"id": <int>, "action": "reinforced"|"closed"|"dropped"|"superseded", "state": "...", "evidence": "...", "confidence": 0.0, "superseded_by": <id>|"new:<index>", "why": "..."}],
  "new_atoms": [{"text": "<one atomic action>", "kind": "commitment|thread|deadline|decision|fact", "owner": "you"|"<name>"|"unclear", "completion_signal": "<observable>"}],
  "nudge": null
}"""


CONV_SYSTEM = """Summarize a conversation transcript captured ambiently from a mic (single channel; the user is the dominant voice, others may come through speakers, and some chunks may be video/podcast bleed).

Return ONLY JSON:
{
  "title": "<5-8 word title>",
  "summary": "<2-4 sentence summary of what was discussed + any decisions/outcomes>",
  "category": "meeting|call|personal|media|unclear",
  "participants": ["<name>", ...],
  "significant": true|false
}

significant — the most important field. TRUE only if this is worth keeping a permanent record of: real work, a meeting, decisions, plans, commitments, a meaningful personal moment, something the user would plausibly want to look back on. FALSE for idle banter, chess/game play-by-play, food/takeout orders, sports chatter, ambient TV/media bleed, casual back-and-forth with no outcome, or anything fragmentary. When in doubt, FALSE — a sparse, high-signal record beats a pile of noise. Most ambient chatter is NOT significant.

If it's clearly not a real conversation (TV, music, silence, ambient noise), set category="media" and significant=false."""


def _parse_json(content: str) -> dict[str, Any]:
    """Tolerate code fences + leading/trailing prose around the JSON."""
    content = (content or "").strip()
    if content.startswith("```"):
        first = content.find("\n")
        last = content.rfind("```")
        if first > 0 and last > first:
            content = content[first + 1:last].strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        a, b = content.find("{"), content.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(content[a:b + 1])
            except json.JSONDecodeError:
                pass
    return {}


async def run_tick(transcript: str, open_atoms: list[dict], config: SundayConfig,
                   current_now: str | None = None) -> dict[str, Any]:
    """One observation tick. Returns {now, same_as_last, atom_updates, new_atoms}.

    `current_now` is the activity line from the last tick — passed back in so
    the (otherwise stateless) brain can keep the activity sticky and recognize
    media instead of re-narrating every 30s scene change.
    """
    from sunday.runtime import build_runtime

    # Give the model the currently-open working atoms so it can reinforce/close
    # them by id. Keep it lean — id, kind, text, owner.
    atoms_blob = "\n".join(
        f"  [{a['id']}] ({a.get('kind')}) {a.get('text')} — owner: {a.get('owner')}"
        for a in open_atoms
    ) or "  (none open)"
    prev = (current_now or "").strip() or "(none yet)"
    user = (
        f"Your PREVIOUS now line: {prev}\n\n"
        f"Open working atoms:\n{atoms_blob}\n\n"
        f"Recent speech (last ~30s):\n{transcript.strip()}\n\nJSON:"
    )

    rt = build_runtime(config)
    result = await rt.complete(
        system_prompt=TICK_SYSTEM,
        messages=[{"role": "user", "content": user}],
        tools_schema=None,
        purpose="observer_tick",
    )
    return _parse_json(result.content or "")


_PROAC_FORMULATE_SYSTEM = """You are Sunday, formulating a short proactive interjection. The user just said something that revealed a knowledge gap — you have the chance to help in one sentence.

Bar: terse. Conversational. One fact / one definition / one specific. End with a light "want more?" hook ONLY if there's clearly more worth saying.

Style: how a friend with the answer would say it on the spot. No throat-clearing. No "Great question". No "Based on what I know". Just the answer.

Length: 1 sentence, occasionally 2. Never more.

If you genuinely don't know or can't be useful, return the literal string "PASS" and nothing else."""


async def formulate_proac(ask: str, evidence: str, config: SundayConfig) -> str | None:
    """Turn a knowledge-gap ask into the one-line interjection to surface."""
    from sunday.runtime import build_runtime

    rt = build_runtime(config)
    result = await rt.complete(
        system_prompt=_PROAC_FORMULATE_SYSTEM,
        messages=[{"role": "user", "content": f"Heard: \"{evidence}\"\n\nQuestion to answer: {ask}\n\nYour one-liner:"}],
        tools_schema=None,
        purpose="proac_formulate",
    )
    text = (result.content or "").strip()
    if not text or text.upper().startswith("PASS"):
        return None
    return text
async def summarize_conversation(transcript: str, config: SundayConfig) -> dict[str, Any]:
    """Close a conversation: produce title/summary/category/participants."""
    from sunday.runtime import build_runtime

    rt = build_runtime(config)
    result = await rt.complete(
        system_prompt=CONV_SYSTEM,
        messages=[{"role": "user", "content": f"Transcript:\n\n{transcript.strip()}\n\nSummary JSON:"}],
        tools_schema=None,
        purpose="observer_conv_close",
    )
    out = _parse_json(result.content or "")
    return {
        "title": out.get("title") or "Untitled conversation",
        "summary": out.get("summary") or "",
        "category": out.get("category") or "unclear",
        "participants": out.get("participants") or [],
        "significant": bool(out.get("significant", False)),
    }
