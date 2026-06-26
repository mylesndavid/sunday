"""Sunday senses how long it's been since you last talked to it.

Covers the pure relative-time formatter at every boundary (minutes / hours /
yesterday / days / weeks), the "stay quiet under ~2 minutes" rule, and the
per-turn line — including that it's omitted when there's no prior message
(the very first message ever) and never crashes on a broken store.
"""

import time

import pytest

from sunday.brain import _relative_since, _since_last_line
from sunday.chat import Chat

MIN = 60
HOUR = 60 * 60
DAY = 24 * HOUR
WEEK = 7 * DAY


# ---- the pure formatter -----------------------------------------------------

@pytest.mark.parametrize("seconds", [0, 1, 30, 60, 119])
def test_under_two_minutes_is_silent(seconds):
    # same active exchange — nothing worth saying
    assert _relative_since(seconds) is None


def test_none_gap_is_silent():
    assert _relative_since(None) is None


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (2 * MIN, "a few minutes"),
        (5 * MIN, "a few minutes"),
        (9 * MIN, "a few minutes"),
        (10 * MIN, "10 minutes"),
        (12 * MIN, "12 minutes"),
        (45 * MIN, "45 minutes"),
        (59 * MIN, "59 minutes"),
        (60 * MIN, "an hour"),
        (90 * MIN, "an hour"),
        (2 * HOUR, "2 hours"),
        (3 * HOUR, "3 hours"),
        (23 * HOUR, "23 hours"),
        (24 * HOUR, "yesterday"),
        (36 * HOUR, "yesterday"),
        (2 * DAY, "2 days"),
        (5 * DAY, "5 days"),
        (13 * DAY, "13 days"),
        (14 * DAY, "2 weeks"),
        (21 * DAY, "3 weeks"),
        (60 * DAY, "9 weeks"),
    ],
)
def test_relative_phrases_at_boundaries(seconds, expected):
    assert _relative_since(seconds) == expected


# ---- the per-turn line over a real chat store -------------------------------

def _chat(tmp_path):
    return Chat(path=tmp_path / "sunday.db")


def test_no_line_on_first_message_ever(tmp_path):
    chat = _chat(tmp_path)
    # nothing before this turn — the store is empty / has only the current turn
    chat.append("user", "hello for the first time", "chat")
    assert chat.previous_message_time() is None
    assert _since_last_line(chat, time.time()) is None


def test_no_line_when_gap_is_short(tmp_path):
    chat = _chat(tmp_path)
    chat.append("user", "earlier", "chat")
    chat.append("sunday", "reply", "chat")
    chat.append("user", "right after", "chat")  # current turn, ~now
    # prior contact is the "sunday" reply, which is essentially now -> silent
    assert _since_last_line(chat, time.time()) is None


def test_line_present_after_a_real_gap(tmp_path):
    chat = _chat(tmp_path)
    old = chat.append("user", "days ago", "chat")
    # backdate that message to 3 days ago
    chat._conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        (time.time() - 3 * DAY, old),
    )
    chat._conn.commit()
    chat.append("user", "i'm back", "chat")  # current turn
    line = _since_last_line(chat, time.time())
    assert line == "It's been 3 days since you last talked to Sunday."


def test_yesterday_phrasing_reads_naturally(tmp_path):
    chat = _chat(tmp_path)
    old = chat.append("sunday", "talk tomorrow", "chat")
    chat._conn.execute(
        "UPDATE messages SET created_at = ? WHERE id = ?",
        (time.time() - 30 * HOUR, old),
    )
    chat._conn.commit()
    chat.append("user", "morning", "chat")
    assert _since_last_line(chat, time.time()) == "You last talked to Sunday yesterday."


def test_previous_message_time_ignores_current_turn(tmp_path):
    chat = _chat(tmp_path)
    chat.append("user", "first", "chat")
    chat.append("user", "second (current turn)", "chat")
    # second-most-recent row's timestamp, not the current turn's
    prev = chat.previous_message_time()
    assert prev is not None


def test_never_crashes_on_broken_store():
    class _BoomChat:
        def previous_message_time(self):
            raise RuntimeError("db is on fire")

    # robustness: a failing store omits the line, never raises
    assert _since_last_line(_BoomChat(), time.time()) is None
