"""Telegram channel — talk to Sunday from the Telegram app.

Inbound: long-polling via the Bot API's getUpdates (no webhook, no public URL).
Telegram holds new messages server-side and hands them out in order; the
`offset` param acknowledges everything up to an update_id, so there's no
dedup bookkeeping to keep (unlike Sendblue's seen-UID set).

Outbound: sendMessage. Replies go back as plain text — Telegram's Markdown
parse modes 400 on unescaped characters, and a delivered plain message beats a
prettier one that sometimes fails.

Setup (all the user does):
  1. Message @BotFather on Telegram → /newbot → copy the token.
  2. Settings → Tools → Telegram (or set the TELEGRAM_BOT_TOKEN credential).
  3. DM the bot. The first chat to message becomes the owner automatically;
     pin specific chat ids with TELEGRAM_ALLOWED_CHAT_IDS to be explicit.

A Telegram bot is PUBLIC — anyone who finds it can message it — so Sunday only
ever answers the owner / allowlist. Randoms get a polite refusal, logged.

Credentials:
  TELEGRAM_BOT_TOKEN          required — from @BotFather
  TELEGRAM_ALLOWED_CHAT_IDS   optional — comma-separated chat ids that may talk
                              to Sunday. Empty = auto-bind to the first sender.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from sunday.brain import respond
from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.telegram")

# Long-poll: the server holds the connection up to POLL_TIMEOUT waiting for an
# update, so there's no busy sleeping. The HTTP client timeout must exceed it.
POLL_TIMEOUT = 25
HTTP_TIMEOUT = POLL_TIMEOUT + 10
MAX_MSG = 4096  # Telegram's hard per-message character cap.


def _token() -> str | None:
    return get_credential("TELEGRAM_BOT_TOKEN")


def _api(method: str, token: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def _allowed_chat_ids() -> set[int]:
    """Explicit allowlist from TELEGRAM_ALLOWED_CHAT_IDS (comma-separated)."""
    raw = get_credential("TELEGRAM_ALLOWED_CHAT_IDS") or ""
    out: set[int] = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            log.warning("telegram: ignoring non-numeric allowed chat id", value=part)
    return out


def _authorized(daemon: Any, chat_id: int, allowed: set[int]) -> bool:
    """A chat may talk to Sunday if it's on the explicit allowlist, or — when
    no allowlist is set — if it's the first chat we ever saw (auto-owner)."""
    if allowed:
        return chat_id in allowed
    owner = getattr(daemon, "_telegram_owner", None)
    if owner is None:
        daemon._telegram_owner = chat_id
        log.info("telegram: auto-bound owner (no allowlist set)", chat_id=chat_id)
        return True
    return chat_id == owner


def _chunks(text: str, n: int = MAX_MSG) -> list[str]:
    text = text or ""
    return [text[i:i + n] for i in range(0, len(text), n)] or [""]


async def _send_telegram(chat_id: int, text: str) -> dict[str, Any]:
    """Send a (possibly chunked) plain-text message. Best-effort."""
    token = _token()
    if not token:
        return {"error": "telegram not configured (TELEGRAM_BOT_TOKEN missing)"}
    last: dict[str, Any] = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            for chunk in _chunks(text):
                if not chunk:
                    continue
                res = await client.post(
                    _api("sendMessage", token),
                    json={"chat_id": chat_id, "text": chunk,
                          "disable_web_page_preview": True},
                )
                last = res.json() if res.headers.get("content-type", "").startswith("application/json") else {}
                if res.status_code != 200:
                    log.warning("telegram sendMessage non-200", status=res.status_code, body=res.text[:200])
                    return {"error": f"telegram send failed: {res.status_code}", "detail": res.text[:200]}
        return {"ok": True, "result": last.get("result")}
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram send failed", error=str(exc))
        return {"error": str(exc)}


async def _send_typing(chat_id: int) -> None:
    token = _token()
    if not token:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(_api("sendChatAction", token),
                              json={"chat_id": chat_id, "action": "typing"})
    except Exception:  # noqa: BLE001 — typing is cosmetic
        pass


async def _process_inbound(daemon: Any, chat_id: int, text: str) -> None:
    """Drive the brain + send the reply. Mirrors the Sendblue channel's timing
    logs so wall-clock is visible per phase."""
    t0 = time.perf_counter()
    asyncio.create_task(_send_typing(chat_id))

    timings: dict[str, Any] = {}
    t_respond = time.perf_counter()
    try:
        reply = await respond(
            daemon.chat,
            text,
            "telegram",
            daemon.config,
            daemon.registry,
            runtime=getattr(daemon, "runtime", None),
            extras={"broadcast": daemon._broadcast, "devices": daemon.devices,
                    "memory": daemon.memory, "runtime": getattr(daemon, "runtime", None)},
            timings=timings,
        )
    except Exception:  # noqa: BLE001
        log.exception("telegram inbound brain failed", chat_id=chat_id)
        await _send_telegram(chat_id, "Something went wrong on my end handling that — try again?")
        return
    respond_ms = round((time.perf_counter() - t_respond) * 1000)

    t_send = time.perf_counter()
    await _send_telegram(chat_id, reply)
    send_ms = round((time.perf_counter() - t_send) * 1000)

    log.info(
        "telegram turn timing",
        total_ms=round((time.perf_counter() - t0) * 1000),
        respond_ms=respond_ms,
        send_ms=send_ms,
        llm_calls_ms=timings.get("llm_calls_ms"),
        memory_ms=round(timings.get("memory_ms", 0)),
        tools_ms=round(timings.get("tools_ms", 0)),
        iterations=timings.get("iterations"),
        tools=timings.get("tool_names", []),
        reply_chars=len(reply or ""),
    )


async def _get_updates(token: str, offset: int, timeout: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        res = await client.get(
            _api("getUpdates", token),
            params={"offset": offset, "timeout": timeout,
                    "allowed_updates": '["message"]'},
        )
    if res.status_code == 409:
        # Another poller (or a set webhook) owns this bot. Common when the bot
        # token is configured on two daemons at once.
        log.warning("telegram getUpdates 409 conflict — another poller/webhook owns this bot")
        return []
    if res.status_code != 200:
        log.warning("telegram getUpdates non-200", status=res.status_code)
        return []
    data = res.json()
    return data.get("result", []) if data.get("ok") else []


async def start_poller(daemon: Any) -> None:
    """Long-poll getUpdates forever, routing authorized messages to the brain."""
    token = _token()
    if not token:
        log.info("telegram poller disabled — TELEGRAM_BOT_TOKEN missing")
        return

    allowed = _allowed_chat_ids()
    log.info("telegram poller starting", allowlist=sorted(allowed) or "auto-bind first sender")

    # Seed past the backlog: grab the latest update and start after it, so a
    # (re)start doesn't replay messages from before the daemon came up.
    offset = 0
    try:
        seed = await _get_updates(token, offset=-1, timeout=0)
        if seed:
            offset = seed[-1]["update_id"] + 1
            log.info("telegram poller seeded past backlog", next_offset=offset)
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram seed failed", error=str(exc))

    while True:
        try:
            updates = await _get_updates(token, offset, POLL_TIMEOUT)
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = msg.get("text") or ""
                if chat_id is None or not text:
                    continue

                if not _authorized(daemon, int(chat_id), allowed):
                    log.info("telegram: ignoring unauthorized chat", chat_id=chat_id)
                    await _send_telegram(int(chat_id),
                                         "This is a private assistant and isn't set up to talk with you.")
                    continue

                if text.strip() == "/start":
                    await _send_telegram(int(chat_id),
                                         f"Hi — I'm Sunday. I'm wired up and listening. "
                                         f"(your chat id is {chat_id})")
                    continue

                log.info("telegram message", chat_id=chat_id, chars=len(text))
                await _process_inbound(daemon, int(chat_id), text)
        except asyncio.CancelledError:
            log.info("telegram poller cancelled")
            return
        except Exception as exc:  # noqa: BLE001 — the loop must never die
            log.warning("telegram poll iteration failed", error=str(exc))
            await asyncio.sleep(3)


# ─── outbound tool (brain-callable) ──────────────────────────────────────────

_SEND_PARAMS = {
    "type": "object",
    "properties": {
        "chat_id": {"type": "integer", "description": "Telegram chat id to send to (numeric)."},
        "text": {"type": "string", "description": "Message text."},
    },
    "required": ["chat_id", "text"],
}


async def _t_telegram_send(args: dict[str, Any], ctx: ToolContext) -> Any:
    chat_id = args.get("chat_id")
    text = args.get("text")
    if chat_id is None or not text:
        return {"error": "'chat_id' and 'text' are required"}
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return {"error": "chat_id must be a number"}
    return await _send_telegram(cid, str(text))


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    from sunday.daemon import register_background_task
    register_background_task(start_poller)

    registry.register(Tool(
        name="telegram_send",
        description=(
            "Send a Telegram message from Sunday's bot to a chat id. Use this to "
            "proactively reach the user on Telegram (chat ids come from people "
            "who've messaged the bot). Replies to an incoming Telegram message "
            "are sent automatically — you don't need this tool for those."
        ),
        parameters=_SEND_PARAMS,
        run=_t_telegram_send,
    ))
