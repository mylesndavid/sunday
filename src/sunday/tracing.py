"""Raindrop Workshop tracing — a LOCAL eval/debug hook, off in production.

Entirely inert unless RAINDROP_LOCAL_DEBUGGER is set (which only happens when
you run Sunday's brain locally against a running Workshop). In prod the env var
is unset, so every function here is a cheap no-op and the raindrop SDK is never
even imported. All hooks are wrapped so tracing can never break a turn.

Wiring (see brain.respond + daemon.main):
  init()                         once at daemon startup
  h = begin_turn(text)           at the top of a turn
  with tool_span(name): ...      around each tool execution
  finish_turn(h, reply)          when the turn returns
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

import structlog

log = structlog.get_logger("sunday.tracing")

ENABLED = bool(os.environ.get("RAINDROP_LOCAL_DEBUGGER"))
_USER = os.environ.get("RAINDROP_TRACE_USER", "local-eval")


def init() -> None:
    if not ENABLED:
        return
    try:
        import raindrop.analytics as r

        r.init(
            local_workshop_url=os.environ["RAINDROP_LOCAL_DEBUGGER"],
            tracing_enabled=True,
            auto_instrument=True,   # auto-captures the OpenAI SDK calls as LLM spans
        )
        log.info("raindrop tracing on", workshop=os.environ["RAINDROP_LOCAL_DEBUGGER"])
    except Exception as exc:  # noqa: BLE001
        log.warning("raindrop init failed (tracing off)", error=str(exc))


def begin_turn(text: str, modality: str = "chat") -> Any:
    """Open an interaction for one turn. Returns an opaque handle (or None)."""
    if not ENABLED:
        return None
    try:
        import raindrop.analytics as r

        return r.begin(user_id=_USER, event="chat_turn", input=text,
                       properties={"modality": modality})
    except Exception as exc:  # noqa: BLE001
        log.warning("begin_turn failed", error=str(exc))
        return None


def finish_turn(handle: Any, output: str) -> None:
    if handle is None:
        return
    try:
        try:
            handle.finish(output=output)
        except TypeError:
            handle.set_property("output", output[:2000])
            handle.finish()
        import raindrop.analytics as r
        r.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning("finish_turn failed", error=str(exc))


@contextlib.contextmanager
def tool_span(name: str):
    """Wrap a real tool execution so it shows as a tool span in Workshop."""
    if not ENABLED:
        yield
        return
    try:
        import raindrop.analytics as r
        with r.tool_span(name):
            yield
    except Exception:  # noqa: BLE001
        # Never let tracing break a tool call.
        yield
