"""Proactive check-in — Sunday occasionally reaches out first.

The owner's one fear: that this feels robotic. So the whole module is built
around restraint, not a timer. The timer firing is *permission to consider*,
never an obligation to send. Three layers of guardrails, in order:

  1. The DECISION (should_check_in) — a pure function. Quiet hours (local
     time, ~09:00–21:00), a jittered gap threshold (so it never lands at the
     same clock time twice), a 24h cooldown, a 30-min "don't ping if they
     were just here" guard, and automatic back-off when recent check-ins go
     ignored. None of it touches the model or the network — fully testable
     with time + last-contact + last-ping + rng injected.

  2. The QUALITY GATE (generate_checkin_text) — even when the decision says
     "you may," we only actually send if the model can come up with something
     genuinely worth saying (a real open thread to follow up, a relevant
     memory, a meaningful daypart moment). If the best it has is a hollow
     "what's up", it returns PASS and we skip this cycle. Under-pinging beats
     filler.

  3. The DELIVERY (in daemon._checkin_worker) — a real macOS notification
     plus a message folded into the one chat, reusing the interjection
     substrate so engagement/dismissal flows the same way everything else does.

State (enabled flag, last check-in time, recent-engagement ledger for the
back-off) lives in ~/.sunday/checkin_state.json — a tiny JSON file so the
cooldown and the "stop checking in" off-switch survive daemon restarts. We
deliberately do NOT use config.yaml: it isn't loaded (config.py defaults are
the real source of truth), so a flag there would be silently ignored.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.checkin")

HOUR = 3600.0
DAY = 24 * HOUR

# ── Defaults (conservative on purpose) ───────────────────────────────────────
# The gap is the *minimum* silence before we'll even consider a ping. Leaned to
# the long end: realistically this should fire at most a few times a WEEK, not
# daily. The effective threshold each evaluation is a random draw in
# [GAP_MIN, GAP_MAX], so it never lands at the same interval or clock time.
DEFAULT_GAP_MIN_HOURS = 10.0
DEFAULT_GAP_MAX_HOURS = 14.0

# Quiet hours: only reach out during waking, sociable hours. Local machine time.
# A check-in is allowed when QUIET_START <= local_hour < QUIET_END.
DEFAULT_QUIET_START_HOUR = 9
DEFAULT_QUIET_END_HOUR = 21

# At most one proactive check-in per this window. A ceiling, not a target.
DEFAULT_COOLDOWN_HOURS = 24.0

# Never ping within this of real user activity — if they were just here, the
# silence isn't real.
RECENT_ACTIVITY_GUARD_SECONDS = 30 * 60

# Back-off: if this many of the most-recent check-ins went unanswered
# (dismissed or ignored, never replied to), stop reaching out for a while —
# Sunday shouldn't nag into the void.
DEFAULT_BACKOFF_AFTER_IGNORED = 3
DEFAULT_BACKOFF_PAUSE_HOURS = 72.0

# How many recent check-in outcomes we keep in the ledger.
LEDGER_CAP = 20


def _state_path() -> Path:
    return sunday_home() / "checkin_state.json"


@dataclass(slots=True)
class CheckinSettings:
    """Tunable knobs. Conservative defaults; overridable from the state file."""
    enabled: bool = True
    gap_min_hours: float = DEFAULT_GAP_MIN_HOURS
    gap_max_hours: float = DEFAULT_GAP_MAX_HOURS
    quiet_start_hour: int = DEFAULT_QUIET_START_HOUR
    quiet_end_hour: int = DEFAULT_QUIET_END_HOUR
    cooldown_hours: float = DEFAULT_COOLDOWN_HOURS
    backoff_after_ignored: int = DEFAULT_BACKOFF_AFTER_IGNORED
    backoff_pause_hours: float = DEFAULT_BACKOFF_PAUSE_HOURS


@dataclass(slots=True)
class CheckinState:
    """Persisted check-in state. last_checkin_at + the engagement ledger are
    what make the cooldown and back-off survive a daemon restart.

    ledger: most-recent-LAST list of outcomes for the check-ins we've sent.
    Each is one of "replied" | "ignored". A sent check-in starts life as
    "ignored" and is upgraded to "replied" if the user engages.
    """
    settings: CheckinSettings = field(default_factory=CheckinSettings)
    last_checkin_at: float = 0.0
    # interjection id -> ledger index, so an engage callback can upgrade the
    # right outcome without scanning.
    last_checkin_iid: int | None = None
    ledger: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Decision:
    """The outcome of should_check_in. `go` is whether the gate is OPEN — the
    caller still has to clear the quality gate before actually sending."""
    go: bool
    reason: str
    threshold_seconds: float = 0.0


# ── persistence ──────────────────────────────────────────────────────────────

def load_state(path: Path | None = None) -> CheckinState:
    """Read state from disk; return conservative defaults if absent/corrupt.
    Never raises — a broken file just means "fresh state"."""
    p = path or _state_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return CheckinState()
    s = raw.get("settings") or {}
    settings = CheckinSettings(
        enabled=bool(s.get("enabled", True)),
        gap_min_hours=float(s.get("gap_min_hours", DEFAULT_GAP_MIN_HOURS)),
        gap_max_hours=float(s.get("gap_max_hours", DEFAULT_GAP_MAX_HOURS)),
        quiet_start_hour=int(s.get("quiet_start_hour", DEFAULT_QUIET_START_HOUR)),
        quiet_end_hour=int(s.get("quiet_end_hour", DEFAULT_QUIET_END_HOUR)),
        cooldown_hours=float(s.get("cooldown_hours", DEFAULT_COOLDOWN_HOURS)),
        backoff_after_ignored=int(s.get("backoff_after_ignored", DEFAULT_BACKOFF_AFTER_IGNORED)),
        backoff_pause_hours=float(s.get("backoff_pause_hours", DEFAULT_BACKOFF_PAUSE_HOURS)),
    )
    ledger = [str(x) for x in (raw.get("ledger") or []) if x in ("replied", "ignored")]
    return CheckinState(
        settings=settings,
        last_checkin_at=float(raw.get("last_checkin_at") or 0.0),
        last_checkin_iid=raw.get("last_checkin_iid"),
        ledger=ledger[-LEDGER_CAP:],
    )


def save_state(state: CheckinState, path: Path | None = None) -> None:
    """Persist state atomically. Never raises — a failed write must not crash
    the daemon, just means the cooldown might not survive a restart this once."""
    p = path or _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "settings": {
                "enabled": state.settings.enabled,
                "gap_min_hours": state.settings.gap_min_hours,
                "gap_max_hours": state.settings.gap_max_hours,
                "quiet_start_hour": state.settings.quiet_start_hour,
                "quiet_end_hour": state.settings.quiet_end_hour,
                "cooldown_hours": state.settings.cooldown_hours,
                "backoff_after_ignored": state.settings.backoff_after_ignored,
                "backoff_pause_hours": state.settings.backoff_pause_hours,
            },
            "last_checkin_at": state.last_checkin_at,
            "last_checkin_iid": state.last_checkin_iid,
            "ledger": state.ledger[-LEDGER_CAP:],
        }
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        log.warning("checkin state save failed", error=str(exc))


def record_checkin(state: CheckinState, when: float, iid: int | None = None) -> CheckinState:
    """Mark that a check-in just went out. Appends an "ignored" outcome to the
    ledger (optimistic-pessimistic: assume ignored until proven replied) and
    stamps the cooldown clock. Mutates + returns the same object."""
    state.last_checkin_at = when
    state.last_checkin_iid = iid
    state.ledger.append("ignored")
    state.ledger = state.ledger[-LEDGER_CAP:]
    return state


def record_engagement(state: CheckinState, iid: int) -> bool:
    """Upgrade the most-recent check-in's outcome to "replied" when the user
    engages with it. Returns True if it matched the tracked check-in."""
    if state.last_checkin_iid is not None and iid == state.last_checkin_iid and state.ledger:
        state.ledger[-1] = "replied"
        return True
    return False


# ── pure helpers (the testable core) ─────────────────────────────────────────

def in_quiet_hours(now: datetime, settings: CheckinSettings) -> bool:
    """True when `now` (local) is OUTSIDE the allowed window — i.e. it's quiet
    hours and we must not ping. The allowed window is
    [quiet_start_hour, quiet_end_hour)."""
    h = now.hour
    return not (settings.quiet_start_hour <= h < settings.quiet_end_hour)


def jittered_threshold(settings: CheckinSettings, rng: random.Random) -> float:
    """A fresh random gap threshold in [gap_min, gap_max], in seconds. Drawn per
    evaluation so the effective interval never repeats — the anti-robotic core."""
    lo = min(settings.gap_min_hours, settings.gap_max_hours)
    hi = max(settings.gap_min_hours, settings.gap_max_hours)
    return rng.uniform(lo, hi) * HOUR


def backoff_active(state: CheckinState, now_ts: float) -> bool:
    """True when the recent check-ins have been ignored enough that we should
    pause. Looks at the last `backoff_after_ignored` outcomes: if ALL of them
    are "ignored" AND the last check-in was within the pause window, hold off.

    The pause is time-boxed off the last check-in, so once the pause elapses we
    try once more — a single reply clears the streak and resets to normal."""
    n = state.settings.backoff_after_ignored
    if n <= 0:
        return False
    if len(state.ledger) < n:
        return False
    recent = state.ledger[-n:]
    if any(o == "replied" for o in recent):
        return False
    # all of the last n were ignored — pause until the pause window elapses
    pause = state.settings.backoff_pause_hours * HOUR
    return (now_ts - state.last_checkin_at) < pause


def should_check_in(
    *,
    now: datetime,
    now_ts: float,
    last_user_activity_ts: float | None,
    state: CheckinState,
    is_active: bool,
    rng: random.Random,
) -> Decision:
    """The whole decision, as a pure function. Every external input is injected
    so this is exhaustively testable with no clock, no db, no model.

    Order matters — cheapest / most-decisive gates first:
      enabled → not currently active → quiet hours → cooldown → back-off →
      recent-activity guard → jittered gap.

    Returns Decision(go=...). go=True means the gate is OPEN — the caller must
    STILL clear the quality gate (generate real content) before sending.
    """
    s = state.settings

    if not s.enabled:
        return Decision(False, "disabled")

    if is_active:
        return Decision(False, "user is active right now")

    if in_quiet_hours(now, s):
        return Decision(False, f"quiet hours (local hour {now.hour})")

    # Cooldown: at most one per ~24h.
    since_last_checkin = now_ts - (state.last_checkin_at or 0.0)
    if state.last_checkin_at and since_last_checkin < s.cooldown_hours * HOUR:
        return Decision(False, f"cooldown ({int(since_last_checkin / 60)}m since last check-in)")

    # Back off when recent check-ins were ignored.
    if backoff_active(state, now_ts):
        return Decision(False, "backing off (recent check-ins ignored)")

    # If we've literally never heard from them, there's no "silence" to break.
    if last_user_activity_ts is None:
        return Decision(False, "no prior user activity")

    gap = now_ts - last_user_activity_ts

    # Never ping right after they were here.
    if gap < RECENT_ACTIVITY_GUARD_SECONDS:
        return Decision(False, f"recent activity ({int(gap / 60)}m ago)")

    threshold = jittered_threshold(s, rng)
    if gap < threshold:
        return Decision(False, f"gap {gap / HOUR:.1f}h < jittered {threshold / HOUR:.1f}h",
                        threshold_seconds=threshold)

    return Decision(True, f"gap {gap / HOUR:.1f}h ≥ jittered {threshold / HOUR:.1f}h",
                    threshold_seconds=threshold)


# ── content generation (the quality gate) ────────────────────────────────────

def daypart(now: datetime) -> str:
    """Mirror brain.py's daypart vocabulary so a contextual ping reads like the
    same person."""
    h = now.hour
    return (
        "the middle of the night" if h < 5 else
        "early morning" if h < 8 else
        "morning" if h < 12 else
        "midday" if h < 14 else
        "afternoon" if h < 18 else
        "evening" if h < 22 else
        "late night"
    )


_CHECKIN_SYSTEM = """You are Sunday, deciding whether to text your person out of the blue — and if so, what to say. You haven't heard from them in a while.

This is the hard part: only reach out if you genuinely have something worth saying. A real, specific hook — an open thread to follow up on, a relevant thing you remember about them, a meaningful moment in their day. NOT a hollow "hey what's up" with nothing behind it. If the best you've got is empty filler, you MUST decline.

You are given context: how long it's been, the time of day, and any open threads / things you remember. Use them. The best check-ins reference something real ("how'd the deck for Manuel turn out?", "you mentioned that flight today — go ok?"). A plain warm check-in is fine ONLY occasionally and ONLY when there's a natural reason (e.g. it's been days and a new morning is a real moment).

Voice: a close friend texting. Lowercase-casual is fine. Short — one line, occasionally two. No greeting boilerplate, no "I was just thinking", no "I noticed it's been". Just say the thing.

Decline by returning the single word PASS (nothing else) when:
  - you have no real hook and would just be filling silence
  - anything you'd say feels generic, needy, or bot-like
  - in doubt — under-reaching is always better than nagging

Otherwise return ONLY the message text. Nothing else."""


def build_context_block(
    *,
    now: datetime,
    gap_seconds: float,
    open_threads: list[str],
    memory_notes: list[str],
) -> str:
    """Assemble the human-readable context the model uses to decide + phrase.
    Pure string building so it's easy to eyeball in tests/logs."""
    hours = gap_seconds / HOUR
    if hours < 24:
        gap_phrase = f"about {int(round(hours))} hours"
    else:
        gap_phrase = f"about {int(round(hours / 24))} days"

    lines = [
        f"It's {now.strftime('%A')} {daypart(now)} ({now.strftime('%-I:%M %p')}).",
        f"You last heard from them {gap_phrase} ago.",
    ]
    if open_threads:
        lines.append("")
        lines.append("Open threads / things they're in the middle of:")
        lines += [f"  - {t}" for t in open_threads[:5]]
    if memory_notes:
        lines.append("")
        lines.append("A few things you remember about them:")
        lines += [f"  - {m}" for m in memory_notes[:5]]
    if not open_threads and not memory_notes:
        lines.append("")
        lines.append("(No specific open threads surfaced — only reach out if a plain, warm "
                     "check-in genuinely fits this moment; otherwise PASS.)")
    return "\n".join(lines)


async def generate_checkin_text(
    *,
    config: Any,
    now: datetime,
    gap_seconds: float,
    open_threads: list[str],
    memory_notes: list[str],
) -> str | None:
    """One-shot model generation of the check-in line — and the quality gate.
    Returns the message, or None if the model declined (PASS) / produced
    nothing usable. Never raises: any failure is treated as "skip this cycle"."""
    from sunday.runtime import build_runtime

    context = build_context_block(
        now=now, gap_seconds=gap_seconds,
        open_threads=open_threads, memory_notes=memory_notes,
    )
    try:
        rt = build_runtime(config)
        result = await rt.complete(
            system_prompt=_CHECKIN_SYSTEM,
            messages=[{"role": "user", "content": context + "\n\nYour message (or PASS):"}],
            tools_schema=None,
            purpose="proactive_checkin",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("checkin generation failed", error=str(exc))
        return None

    text = (result.content or "").strip()
    if not text:
        return None
    # The quality gate: an explicit PASS, or anything that's effectively PASS.
    stripped = text.strip().strip('".').upper()
    if stripped == "PASS" or stripped.startswith("PASS"):
        log.info("checkin quality-gated (model passed)")
        return None
    # Guard against the model wrapping a refusal in prose.
    if "PASS" == text.strip().upper():
        return None
    return text
