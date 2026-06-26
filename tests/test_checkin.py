"""Proactive check-in — the decision logic, the quality gate, and back-off.

The whole point of the feature is restraint, so the tests focus on the gates:
that should_check_in respects quiet hours, the 24h cooldown, the recent-activity
guard, and the jittered gap threshold; that the jitter stays inside its window;
that it backs off after a run of ignored check-ins (and recovers on a reply);
and that the quality gate skips a cycle when the model returns PASS. Every
external input (time, last contact, last ping, rng) is injected — no clock, no
db, no real model.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from sunday import checkin
from sunday.checkin import (
    CheckinSettings,
    CheckinState,
    backoff_active,
    build_context_block,
    in_quiet_hours,
    jittered_threshold,
    load_state,
    record_checkin,
    record_engagement,
    save_state,
    should_check_in,
)

HOUR = 3600.0
DAY = 24 * HOUR


def _dt(hour, minute=0, weekday_monday=True):
    """A local-ish datetime at a given hour. 2024-01-15 is a Monday."""
    base = datetime(2024, 1, 15, hour, minute, tzinfo=timezone.utc)
    return base


def _state(**settings_over):
    s = CheckinSettings(**settings_over)
    return CheckinState(settings=s)


def _fixed_rng(value_hours):
    """An rng whose uniform() always returns value_hours — so the jittered
    threshold is deterministic in a test."""
    class _R(random.Random):
        def uniform(self, a, b):
            return value_hours
    return _R()


# ── quiet hours ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,quiet", [
    (0, True), (5, True), (8, True),      # before 09:00 — quiet
    (9, False), (12, False), (20, False),  # inside the window — allowed
    (21, True), (22, True), (23, True),   # 21:00 and after — quiet
])
def test_quiet_hours_window(hour, quiet):
    s = CheckinSettings()
    assert in_quiet_hours(_dt(hour), s) is quiet


def test_no_ping_during_quiet_hours_even_with_huge_gap():
    state = _state()
    now = _dt(2)  # 2am
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 5 * DAY,  # silent for days
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert not d.go
    assert "quiet hours" in d.reason


# ── jitter window ─────────────────────────────────────────────────────────────

def test_jitter_stays_within_window():
    s = CheckinSettings(gap_min_hours=10, gap_max_hours=14)
    rng = random.Random(1234)
    for _ in range(500):
        thr = jittered_threshold(s, rng)
        assert 10 * HOUR <= thr <= 14 * HOUR


def test_jitter_varies():
    s = CheckinSettings(gap_min_hours=10, gap_max_hours=14)
    rng = random.Random(7)
    draws = {round(jittered_threshold(s, rng), 2) for _ in range(50)}
    assert len(draws) > 10  # not a constant — the anti-robotic core


# ── the gap threshold ─────────────────────────────────────────────────────────

def test_gap_below_jittered_threshold_skips():
    state = _state()
    now = _dt(12)
    # silent 9h, but this evaluation's jittered threshold is 12h -> not yet
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 9 * HOUR,
        state=state, is_active=False, rng=_fixed_rng(12),
    )
    assert not d.go
    assert "<" in d.reason


def test_gap_above_jittered_threshold_opens_gate():
    state = _state()
    now = _dt(12)
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 13 * HOUR,
        state=state, is_active=False, rng=_fixed_rng(12),
    )
    assert d.go


# ── recent activity guard ─────────────────────────────────────────────────────

def test_never_ping_right_after_activity():
    state = _state()
    now = _dt(12)
    # only 10 minutes since they were here — never
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 10 * 60,
        state=state, is_active=False, rng=_fixed_rng(0.001),
    )
    assert not d.go
    assert "recent activity" in d.reason


def test_no_ping_when_user_is_active_now():
    state = _state()
    now = _dt(12)
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 3 * DAY,
        state=state, is_active=True, rng=_fixed_rng(10),
    )
    assert not d.go
    assert "active" in d.reason


# ── cooldown ──────────────────────────────────────────────────────────────────

def test_cooldown_blocks_a_second_ping_same_day():
    now = _dt(12)
    state = _state()
    state.last_checkin_at = now.timestamp() - 3 * HOUR  # checked in 3h ago
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 2 * DAY,
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert not d.go
    assert "cooldown" in d.reason


def test_cooldown_clears_after_24h():
    now = _dt(12)
    state = _state()
    state.last_checkin_at = now.timestamp() - 25 * HOUR
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 2 * DAY,
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert d.go


# ── enabled / disabled ────────────────────────────────────────────────────────

def test_disabled_never_fires():
    now = _dt(12)
    state = _state(enabled=False)
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 5 * DAY,
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert not d.go
    assert d.reason == "disabled"


def test_no_prior_activity_skips():
    now = _dt(12)
    state = _state()
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=None,
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert not d.go
    assert "no prior" in d.reason


# ── back-off when ignored ─────────────────────────────────────────────────────

def test_backoff_after_n_ignored():
    now = _dt(12)
    state = _state(backoff_after_ignored=3, backoff_pause_hours=72)
    state.ledger = ["ignored", "ignored", "ignored"]
    state.last_checkin_at = now.timestamp() - 30 * HOUR  # within the 72h pause
    assert backoff_active(state, now.timestamp())
    d = should_check_in(
        now=now, now_ts=now.timestamp(),
        last_user_activity_ts=now.timestamp() - 5 * DAY,
        state=state, is_active=False, rng=_fixed_rng(10),
    )
    assert not d.go
    assert "backing off" in d.reason


def test_backoff_recovers_after_pause_window():
    now = _dt(12)
    state = _state(backoff_after_ignored=3, backoff_pause_hours=72)
    state.ledger = ["ignored", "ignored", "ignored"]
    state.last_checkin_at = now.timestamp() - 80 * HOUR  # pause window elapsed
    assert not backoff_active(state, now.timestamp())


def test_a_reply_clears_the_backoff_streak():
    now = _dt(12)
    state = _state(backoff_after_ignored=3)
    # one of the last 3 was a reply -> not backing off
    state.ledger = ["ignored", "replied", "ignored"]
    state.last_checkin_at = now.timestamp() - 30 * HOUR
    assert not backoff_active(state, now.timestamp())


def test_backoff_needs_full_window_of_ignores():
    now = _dt(12)
    state = _state(backoff_after_ignored=3)
    state.ledger = ["ignored", "ignored"]  # only 2 so far
    state.last_checkin_at = now.timestamp() - 30 * HOUR
    assert not backoff_active(state, now.timestamp())


# ── ledger bookkeeping ────────────────────────────────────────────────────────

def test_record_checkin_appends_ignored_and_stamps_cooldown():
    state = _state()
    record_checkin(state, when=1000.0, iid=42)
    assert state.last_checkin_at == 1000.0
    assert state.last_checkin_iid == 42
    assert state.ledger[-1] == "ignored"


def test_record_engagement_upgrades_matching_checkin():
    state = _state()
    record_checkin(state, when=1000.0, iid=42)
    assert record_engagement(state, 42) is True
    assert state.ledger[-1] == "replied"


def test_record_engagement_ignores_mismatched_id():
    state = _state()
    record_checkin(state, when=1000.0, iid=42)
    assert record_engagement(state, 99) is False
    assert state.ledger[-1] == "ignored"


# ── persistence (cooldown survives restart) ───────────────────────────────────

def test_state_round_trips_through_disk(tmp_path):
    p = tmp_path / "checkin_state.json"
    state = _state(enabled=False, gap_min_hours=11)
    record_checkin(state, when=12345.0, iid=7)
    record_engagement(state, 7)
    save_state(state, path=p)
    loaded = load_state(path=p)
    assert loaded.settings.enabled is False
    assert loaded.settings.gap_min_hours == 11
    assert loaded.last_checkin_at == 12345.0
    assert loaded.last_checkin_iid == 7
    assert loaded.ledger[-1] == "replied"


def test_load_missing_file_is_defaults(tmp_path):
    loaded = load_state(path=tmp_path / "nope.json")
    assert loaded.settings.enabled is True
    assert loaded.last_checkin_at == 0.0


def test_load_corrupt_file_is_defaults(tmp_path):
    p = tmp_path / "checkin_state.json"
    p.write_text("not json {{{", encoding="utf-8")
    loaded = load_state(path=p)
    assert loaded.settings.enabled is True


# ── the quality gate (generate_checkin_text) ──────────────────────────────────

class _FakeResult:
    def __init__(self, content):
        self.content = content


class _FakeRuntime:
    def __init__(self, content):
        self._content = content
    async def complete(self, **kwargs):
        return _FakeResult(self._content)


@pytest.fixture
def patch_runtime(monkeypatch):
    def _install(content):
        import sunday.runtime as rt
        monkeypatch.setattr(rt, "build_runtime", lambda config: _FakeRuntime(content))
    return _install


async def test_quality_gate_skips_on_pass(patch_runtime):
    patch_runtime("PASS")
    out = await checkin.generate_checkin_text(
        config=object(), now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=[], memory_notes=[],
    )
    assert out is None


async def test_quality_gate_skips_on_quoted_pass(patch_runtime):
    patch_runtime('"PASS."')
    out = await checkin.generate_checkin_text(
        config=object(), now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=[], memory_notes=[],
    )
    assert out is None


async def test_quality_gate_skips_on_empty(patch_runtime):
    patch_runtime("   ")
    out = await checkin.generate_checkin_text(
        config=object(), now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=["the deck for Manuel"], memory_notes=[],
    )
    assert out is None


async def test_quality_gate_returns_real_message(patch_runtime):
    patch_runtime("how'd the deck for Manuel turn out?")
    out = await checkin.generate_checkin_text(
        config=object(), now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=["the deck for Manuel"], memory_notes=[],
    )
    assert out == "how'd the deck for Manuel turn out?"


async def test_generation_never_raises_on_runtime_error(monkeypatch):
    import sunday.runtime as rt
    def _boom(config):
        raise RuntimeError("no model")
    monkeypatch.setattr(rt, "build_runtime", _boom)
    out = await checkin.generate_checkin_text(
        config=object(), now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=[], memory_notes=[],
    )
    assert out is None


# ── context block (the model's hooks) ─────────────────────────────────────────

def test_context_block_includes_threads_and_notes():
    block = build_context_block(
        now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=["the deck for Manuel"],
        memory_notes=["lives in Brooklyn"],
    )
    assert "the deck for Manuel" in block
    assert "lives in Brooklyn" in block
    assert "13 hours" in block


def test_context_block_nudges_pass_when_no_hooks():
    block = build_context_block(
        now=_dt(10), gap_seconds=13 * HOUR,
        open_threads=[], memory_notes=[],
    )
    assert "PASS" in block


# ── the conversational off-switch matcher ─────────────────────────────────────

@pytest.mark.parametrize("text", [
    "stop checking in",
    "please stop checking in on me",
    "can you stop reaching out unprompted",
    "stop messaging me first",
    "turn off the check-ins",
    "knock it off with the check-ins",
])
def test_stop_phrases_match(text):
    from sunday.daemon import _wants_to_stop_checkins
    assert _wants_to_stop_checkins(text) is True


@pytest.mark.parametrize("text", [
    "checking in on the deploy",          # the user checking in, not Sunday
    "can you check in with the team?",
    "stop the build",                      # stop, but nothing to do with check-ins
    "hey what's up",
    "reach out to Manuel for me",          # asking Sunday to reach out to someone
])
def test_non_stop_phrases_dont_match(text):
    from sunday.daemon import _wants_to_stop_checkins
    assert _wants_to_stop_checkins(text) is False
