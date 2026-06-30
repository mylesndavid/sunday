"""`notify_user` — the one way an EMAIL turn reaches the main chat.

Email is handled QUIETLY: inbound mail drives the brain under the
`email_agentmail:*` modality, which the desktop app hides from the main chat
timeline (it lives in the Inbox instead). That keeps routine email back-and-
forth out of the user's face. But sometimes Sunday genuinely needs the user
mid-email — a decision, a missing fact she can't supply, a judgment call. For
exactly those moments she calls `notify_user`, which posts ONE assistant
message onto the MAIN chat timeline (thread_id = None) under a normal modality
so the main-chat email filter shows it, and broadcasts it so the open desktop
app renders it immediately.

It needs the persistent chat + the broadcast callback. The email pipeline
threads both through `respond(...)`'s extras dict (`chat` + `broadcast`), so
this tool reads them off `ctx.extras`. Outside that wiring (a subagent's
ephemeral chat, a test) it degrades gracefully rather than erroring.
"""

from __future__ import annotations

from typing import Any

import structlog

from sunday.config import SundayConfig
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.notify")

# The modality the notify message records under. Deliberately NOT
# `email_agentmail:*` — that prefix is exactly what the desktop hides — so this
# message lands in (and stays in) the MAIN chat. "notify" reads cleanly in the
# log and never trips the email filter.
NOTIFY_MODALITY = "notify"


async def _t_notify_user(args: dict[str, Any], ctx: ToolContext) -> Any:
    message = (args.get("message") or "").strip()
    if not message:
        return {"error": "'message' is required"}

    # The persistent chat to append to. The email extras threads `chat` (the
    # daemon's canonical chat) through; fall back to the ToolContext's own chat
    # so this still works on the main desktop path if ever called there.
    chat = ctx.extras.get("chat") or getattr(ctx, "chat", None)
    if chat is None:
        return {"error": "no chat handle available to notify the user"}

    # Land it on the MAIN timeline: thread_id=None, normal modality. This is the
    # ONLY place an email-driven turn writes a user-visible main-chat message.
    try:
        chat.append("sunday", message, NOTIFY_MODALITY, thread_id=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_user chat append failed", error=str(exc))
        return {"error": f"failed to record notification: {exc}"}

    # Broadcast so an open desktop app shows it immediately. A "reply" event
    # with thread_id=None is exactly what the main-chat live path listens for
    # (it triggers refreshLog), and the normal modality means the email filter
    # won't suppress it.
    bc = ctx.extras.get("broadcast")
    if bc is not None:
        try:
            await bc({
                "type": "reply",
                "modality": NOTIFY_MODALITY,
                "content": message,
                "thread_id": None,
            })
        except Exception:  # noqa: BLE001 — a broadcast hiccup must not fail the tool
            log.warning("notify_user broadcast failed")

    return {"ok": True, "notified": True}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="notify_user",
        description=(
            "Surface a message to the user in the MAIN chat — use this ONLY when "
            "you're working an email in the background and genuinely need the "
            "user: a decision, a fact you can't supply, or a judgment call "
            "(e.g. 'Itani needs your DOB + insurance to hold tomorrow 3pm — want "
            "me to send it?'). Email is otherwise handled QUIETLY and never "
            "narrated in the main chat, so do NOT use this to report routine "
            "email back-and-forth or to say you replied. One message, only when "
            "you actually need them."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What to tell the user, written the way you'd text them.",
                },
            },
            "required": ["message"],
        },
        run=_t_notify_user,
    ))
    log.info("notify_user tool registered")
