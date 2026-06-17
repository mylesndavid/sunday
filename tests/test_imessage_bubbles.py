"""Replies go out as a few natural texts, not one markdown wall.

split_into_bubbles breaks on blank lines (the brain is told to separate beats
that way), caps the count, and strips the markdown artifacts that look broken
in a bubble — while leaving ordinary text (math, single asterisks) alone.
"""

import pytest

from sunday.channels.imessage_watch import MAX_BUBBLES, split_into_bubbles


def test_single_block_stays_one_bubble():
    assert split_into_bubbles("hey what's up") == ["hey what's up"]


def test_blank_lines_split_into_separate_bubbles():
    assert split_into_bubbles("on it\n\ngive me a sec\n\ndone") == [
        "on it", "give me a sec", "done",
    ]


def test_single_line_breaks_stay_in_one_bubble():
    # multi-line content (single \n) is ONE message; only blank lines split
    reply = "here's the plan:\ngrab milk\ncall mom\n\non it now"
    assert split_into_bubbles(reply) == ["here's the plan:\ngrab milk\ncall mom", "on it now"]


def test_overflow_merges_into_last_bubble():
    reply = "\n\n".join(f"line {i}" for i in range(8))
    bubbles = split_into_bubbles(reply)
    assert len(bubbles) == MAX_BUBBLES
    assert bubbles[:3] == ["line 0", "line 1", "line 2"]
    assert "line 7" in bubbles[-1]  # nothing dropped


def test_strips_markdown_artifacts():
    reply = "# Plan\n\n- **first** thing\n- second `cmd` thing"
    bubbles = split_into_bubbles(reply)
    assert bubbles[0] == "Plan"
    assert "**" not in bubbles[1] and "`" not in bubbles[1]
    assert "first thing" in bubbles[1]


def test_leaves_ordinary_text_alone():
    # single asterisks / math must NOT be mangled
    assert split_into_bubbles("2*3 = 6 and the *only* fix") == ["2*3 = 6 and the *only* fix"]


@pytest.mark.parametrize("empty", ["", "   ", "\n\n", None])
def test_empty_reply_no_bubbles(empty):
    assert split_into_bubbles(empty) == []
