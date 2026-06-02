"""Argus agent observability — opt-in.

Set SUNDAY_ARGUS_URL to your Argus ingest (e.g. http://localhost:4317) and
Sunday emits a trace per turn with an LLM span for every model call (and tool
spans where wired). Disabled + entirely no-op when the env var is unset; the
vendored SDK swallows network errors, so this never affects a turn.
"""
from __future__ import annotations

import os

from sunday.vendor.argus import Argus, _current  # _current = the SDK's nesting contextvar

_client: Argus | None = None
_checked = False


def _argus() -> Argus | None:
    global _client, _checked
    if not _checked:
        _checked = True
        url = (os.environ.get("SUNDAY_ARGUS_URL") or "").strip()
        _client = Argus(url=url, service="sunday") if url else None
    return _client


def start_turn(name: str, session: str | None = None, user: str | None = None):
    """Begin a per-turn trace and make it current so LLM/tool spans nest under
    it. Returns (trace, token); both None when disabled."""
    a = _argus()
    if a is None:
        return None, None
    t = a.trace(name, session=session, user=user)
    return t, _current.set(t)


def end_turn(trace, token, error: str | None = None) -> None:
    if trace is None:
        return
    try:
        trace.end(error=error)
    finally:
        if token is not None:
            _current.reset(token)


def llm_span(name: str, model: str | None = None, purpose: str | None = None, inp=None):
    """Open an LLM span on the current trace (if any)."""
    t = _current.get()
    if t is None:
        return None
    return t.span(name, kind="llm", model=model, input=inp,
                  attributes={"purpose": purpose} if purpose else None)


def tool_span(name: str, tool: str | None = None, inp=None):
    t = _current.get()
    if t is None:
        return None
    return t.span(name, kind="tool", tool=tool, input=inp)
