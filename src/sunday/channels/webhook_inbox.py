"""Arbitrary-webhook ingestion channel — the general unlock (spec §4).

The user creates a NAMED hook in the Inbox UI ("cold-email-replies"), gets a
URL + token, and pastes that URL into ANY tool that can POST: a cold-email
platform, Stripe, GitHub, Zapier, a cron job. When that URL is hit, Sunday's
brain reacts — optionally per a standing instruction the user attached to the
hook ("when this fires, draft a reply and text me").

Unlike a blessed first-party channel (Sendblue, AgentMail), there is no
provider API to call and no poller: a hook is purely an INBOUND seam. Each
hook is one row in ~/.sunday/webhooks.json with its own unguessable token, so
a single leaked hook can be revoked/rotated without touching the others.

Routing note — why per-hook registration instead of one catch-all handler:
  `daemon._webhook_dispatch` is an EXACT-path lookup against the `_webhooks`
  dict (daemon.py:2986). It does NOT pattern-match, so a single registered
  handler can only ever serve one literal path. Slugs are user-minted at
  runtime, so we can't enumerate them at import time. The clean fix that needs
  ZERO daemon.py changes: `register_webhook(path, handler)` is just a dict
  insert we can call whenever — so we register `/webhooks/hook/<slug>` the
  moment a hook is created (and re-register every persisted hook on startup in
  `register()`). `_webhook_dispatch` reads `_webhooks` live per request, so a
  path added after boot is dispatched normally. No `{slug}` route required.

Token transport: the token is carried as the `X-Hook-Token` REQUEST HEADER
  (not a path segment). Rationale — the path is what the user pastes into a
  third-party UI and what tends to leak into logs/referers/screenshots; the
  header keeps the secret out of the URL the user shares. Tools that can't set
  a custom header may pass `?token=` as a documented fallback (see below).
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import structlog
from aiohttp import web

from sunday.brain import respond
from sunday.config import SundayConfig
from sunday.paths import sunday_home
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.channel.webhook_inbox")

# Fixed prefix every named hook lives under. Public form (with relay):
#   https://relay.sunday.xyz/u/<agent-id>/hook/<slug>
# Loopback/local form (what we actually register on the daemon):
#   /webhooks/hook/<slug>
HOOK_PATH_PREFIX = "/webhooks/hook/"

# Body summarisation guardrails. A webhook payload can be anything from a
# 40-byte ping to a 2 MB Stripe event — we hand the brain a COMPACT,
# human-readable digest, not the raw firehose. The brain can always be told
# (via the hook instruction) to ask for more if it needs the full body.
MAX_SUMMARY_CHARS = 1500   # total characters of payload we put in the prompt
MAX_FIELD_CHARS   = 200    # per-value truncation inside a flat dict summary

# slug hygiene: lowercase, digits, dashes — what a tool UI / URL will tolerate
# without escaping, and what reads cleanly in `webhook:<slug>` channel tags.
_SLUG_RE = re.compile(r"[^a-z0-9-]+")


# ─── hook store (mirrors connectors.py load/save style) ───────────────────
# One JSON file, ~/.sunday/webhooks.json, schema:
#   {"hooks": [ {slug, token, instruction, created_at, enabled}, … ]}
# We keep it a list (not a dict keyed by slug) so the file reads naturally and
# round-trips cleanly; lookups are linear over a handful of hooks — cheap.


def _path():
    return sunday_home() / "webhooks.json"


def _load_raw() -> list[dict[str, Any]]:
    p = _path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        hooks = data.get("hooks")
        return list(hooks) if isinstance(hooks, list) else []
    except (json.JSONDecodeError, OSError):
        # A corrupt/half-written file should not take the channel down — treat
        # it as empty (same tolerant posture as connectors.load()).
        return []


def _save_raw(hooks: list[dict[str, Any]]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")


def _slugify(raw: str) -> str:
    """Normalise a user-typed name into a URL-safe slug. 'Cold Email Replies'
    -> 'cold-email-replies'. Collapses runs of junk to a single dash and trims
    leading/trailing dashes so we never mint '/webhooks/hook/-foo-'."""
    s = _SLUG_RE.sub("-", (raw or "").strip().lower())
    return s.strip("-")


def list_hooks() -> list[dict[str, Any]]:
    """Every hook on disk, newest-first. Tokens are included — callers that
    surface this to a UI decide whether to show or mask them (the create flow
    shows the token once; the list view typically masks)."""
    hooks = _load_raw()
    hooks.sort(key=lambda h: h.get("created_at") or "", reverse=True)
    return hooks


def get_hook(slug: str) -> dict[str, Any] | None:
    """Exact-slug lookup. None when unknown."""
    for h in _load_raw():
        if h.get("slug") == slug:
            return h
    return None


def create_hook(slug: str, instruction: str = "") -> dict[str, Any]:
    """Mint a new hook. `slug` is normalised; `instruction` is the optional
    standing order prepended to every event from this hook.

    Idempotent-ish on slug COLLISION: if the slug already exists we return the
    existing hook UNCHANGED (we never silently rotate a live token), flagged
    `already=True` so the caller can tell the user 'that name's taken — here's
    the existing one'. The token is minted with secrets.token_urlsafe(32) =
    256 bits, so the URL is unguessable even before the header token check."""
    norm = _slugify(slug)
    if not norm:
        return {"error": "slug is empty after normalisation — give it a name"}

    existing = get_hook(norm)
    if existing is not None:
        return {**existing, "already": True}

    hook = {
        "slug": norm,
        "token": secrets.token_urlsafe(32),
        "instruction": (instruction or "").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "enabled": True,
    }
    hooks = _load_raw()
    hooks.append(hook)
    _save_raw(hooks)
    # Register the handler NOW so the brand-new URL is live this same daemon
    # run, no restart needed. Safe to call repeatedly — register_webhook is a
    # dict insert keyed by path.
    _register_one(hook["slug"])
    log.info("webhook hook created", slug=norm)
    return {**hook, "already": False}


def revoke_hook(slug: str) -> dict[str, Any]:
    """Permanently delete a hook (kills its URL). We drop it from the store AND
    unregister its loopback path so a stale POST gets a clean 404 instead of
    being silently accepted. Returns {ok, removed} / {error}."""
    norm = _slugify(slug)
    hooks = _load_raw()
    kept = [h for h in hooks if h.get("slug") != norm]
    if len(kept) == len(hooks):
        return {"error": f"no hook named '{norm}'"}
    _save_raw(kept)
    _unregister_one(norm)
    log.info("webhook hook revoked", slug=norm)
    return {"ok": True, "removed": norm}


# ─── payload → compact natural-language event ─────────────────────────────


def _summarize_payload(raw_body: str, content_type: str) -> str:
    """Turn an arbitrary request body into something the brain can read in one
    glance. JSON objects become `key: value` lines (per-value truncated); any
    other body is passed through as trimmed text. The whole thing is hard-
    capped at MAX_SUMMARY_CHARS so a giant payload can't blow the prompt."""
    body = (raw_body or "").strip()
    if not body:
        return "(empty body)"

    # Prefer a structured digest when it parses as a JSON object — that's the
    # overwhelmingly common webhook shape (Stripe, GitHub, Zapier, esp tools).
    if "json" in (content_type or "").lower() or body[:1] in ("{", "["):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            lines: list[str] = []
            for key, value in parsed.items():
                rendered = value if isinstance(value, str) else json.dumps(value, default=str)
                if len(rendered) > MAX_FIELD_CHARS:
                    rendered = rendered[:MAX_FIELD_CHARS] + "…"
                lines.append(f"{key}: {rendered}")
            summary = "\n".join(lines)
            return summary[:MAX_SUMMARY_CHARS] + ("…" if len(summary) > MAX_SUMMARY_CHARS else "")
        if isinstance(parsed, list):
            summary = json.dumps(parsed, default=str)[:MAX_SUMMARY_CHARS]
            return f"(JSON array, {len(parsed)} items)\n{summary}"

    # Non-JSON: form-encoded, plain text, XML — hand it over trimmed.
    return body[:MAX_SUMMARY_CHARS] + ("…" if len(body) > MAX_SUMMARY_CHARS else "")


def _build_event(slug: str, instruction: str, summary: str) -> str:
    """Compose the message handed to respond(). The standing instruction (if
    any) leads, so it frames the payload that follows — the brain reads 'here's
    what to do' before 'here's what happened'."""
    parts: list[str] = []
    if instruction:
        parts.append(f"Standing instruction for this hook: {instruction}")
    parts.append(f"Webhook '{slug}' fired:\n{summary}")
    return "\n\n".join(parts)


# ─── inbound handler ──────────────────────────────────────────────────────


async def _hook_handler(request: web.Request, daemon: Any) -> web.Response:
    """One shared handler bound to every `/webhooks/hook/<slug>` path. Resolves
    the slug from the request path (not match_info — we register literal paths,
    so `request.path` is the source of truth) and the token from the header.

    Fast and forgiving by design: a webhook caller wants a quick 2xx/4xx, not
    to wait on the brain. We validate, kick the brain off in the BACKGROUND,
    and return {"ok": true} immediately — the provider's retry logic should
    never see a timeout just because the agent loop is slow."""
    path = request.path
    if not path.startswith(HOOK_PATH_PREFIX):
        # Shouldn't happen (we only register under the prefix) but be explicit.
        return web.json_response({"error": "not a hook path"}, status=404)
    slug = path[len(HOOK_PATH_PREFIX):].strip("/")

    hook = get_hook(slug)
    # 404 for both unknown AND disabled — we don't leak "this hook exists but
    # is off" to an unauthenticated caller; it just looks gone.
    if hook is None or not hook.get("enabled", True):
        return web.json_response({"error": "no such hook"}, status=404)

    # Token check. Header is the documented transport; ?token= is a fallback
    # for callers that can't set custom headers. Constant-time compare so we
    # don't leak the token length/prefix via timing.
    import hmac
    presented = request.headers.get("X-Hook-Token") or request.query.get("token") or ""
    expected = hook.get("token") or ""
    if not presented or not expected or not hmac.compare_digest(presented, expected):
        return web.json_response({"error": "bad or missing hook token"}, status=401)

    # Read the body as raw text — we never assume a content-type. _summarize
    # decides how to render it.
    try:
        raw_body = await request.text()
    except Exception:  # noqa: BLE001
        raw_body = ""
    content_type = request.headers.get("Content-Type", "")
    summary = _summarize_payload(raw_body, content_type)
    event_text = _build_event(slug, hook.get("instruction") or "", summary)

    log.info("webhook hook fired", slug=slug, body_chars=len(raw_body))

    # Drive the brain off the request path so the HTTP response is instant.
    import asyncio
    asyncio.create_task(_process_event(daemon, slug, event_text))
    return web.json_response({"ok": True})


async def _process_event(daemon: Any, slug: str, event_text: str) -> None:
    """Run the brain on a fired hook. Mirrors sendblue._process_inbound's
    respond() call exactly — same extras dict shape (broadcast/devices/memory/
    runtime/registry/active_tools tiered-tools wiring) so a webhook turn behaves
    like every other channel's turn. Channel tag is `webhook:<slug>`."""
    timings: dict[str, Any] = {}
    t0 = time.perf_counter()
    try:
        reply = await respond(
            daemon.chat,
            event_text,
            f"webhook:{slug}",
            daemon.config,
            daemon.registry,
            runtime=getattr(daemon, "runtime", None),
            extras={"broadcast": daemon._broadcast, "devices": daemon.devices,
                    "memory": daemon.memory, "runtime": getattr(daemon, "runtime", None),
                    # Tiered tools, same as the desktop chat + Sendblue paths:
                    # send the lean core schema and let find_tools surface the
                    # rest on demand, sharing the daemon's session-wide active
                    # set. A webhook event often needs a long-tail tool (draft
                    # an email, text the user) — find_tools reaches those.
                    "registry": daemon.registry,
                    "active_tools": daemon._active_tools},
            timings=timings,
        )
    except Exception:  # noqa: BLE001
        log.exception("webhook brain failed", slug=slug)
        return
    log.info(
        "webhook turn timing",
        slug=slug,
        total_ms=round((time.perf_counter() - t0) * 1000),
        llm_calls_ms=timings.get("llm_calls_ms"),
        tools=timings.get("tool_names", []),
        reply_chars=len(reply or ""),
    )


# ─── per-hook route (un)registration ──────────────────────────────────────


def _register_one(slug: str) -> None:
    """Bind `/webhooks/hook/<slug>` to the shared handler. Idempotent: a repeat
    call just rewrites the same dict entry."""
    from sunday.daemon import register_webhook
    register_webhook(f"{HOOK_PATH_PREFIX}{slug}", _hook_handler)


def _unregister_one(slug: str) -> None:
    """Drop a hook's loopback path so a revoked URL 404s cleanly. Reaches into
    the daemon's `_webhooks` dict directly because there's no public
    unregister API — and we must NOT edit daemon.py to add one."""
    from sunday.daemon import _webhooks
    _webhooks.pop(f"{HOOK_PATH_PREFIX}{slug}", None)


# ─── public URL building ──────────────────────────────────────────────────


def _public_base() -> str | None:
    """The public origin a hook URL hangs off, if Sunday knows one.

    The relay (spec §2/§9) is the eventual source of this — a hosted URL like
    `https://relay.sunday.xyz/u/<agent-id>`. That config (RelayConfig) doesn't
    exist yet, so we probe it defensively: if a future RelayConfig with an
    agent_id shows up we'll use it; otherwise we return None and the tool
    returns a LOCAL placeholder path the UI fills in once ingress is set up
    (Funnel DNS name, relay agent-id, or a VPS host)."""
    from sunday.config import load_config
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001
        return None
    relay = getattr(cfg, "relay", None)
    if relay is not None and getattr(relay, "enabled", False):
        # Relay public form: <https-origin>/u/<agent-id>. The configured url is
        # a wss:// socket origin; the inbound HTTP origin is the same host over
        # https. Best-effort translation; the UI can always correct it.
        url = (getattr(relay, "url", "") or "").strip()
        agent_id = (getattr(relay, "agent_id", "") or "").strip()
        if url and agent_id:
            https = url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")
            return f"{https}/u/{agent_id}"
    return None


def hook_url(slug: str) -> str:
    """The URL the user pastes into their tool. Public relay form when ingress
    is configured; otherwise a local placeholder the Inbox UI rewrites once the
    user picks an ingress (it still carries the right `/hook/<slug>` suffix)."""
    base = _public_base()
    if base:
        return f"{base}/hook/{slug}"
    # Placeholder: real path, no host. The UI substitutes the public origin
    # (relay agent URL / Funnel DNS / VPS host) it knows about.
    return f"<your-sunday-public-url>{HOOK_PATH_PREFIX}{slug}"


# ─── brain-callable tools ─────────────────────────────────────────────────


_CREATE_PARAMS = {
    "type": "object",
    "properties": {
        "slug": {
            "type": "string",
            "description": "Short name for the hook, e.g. 'cold-email-replies'. "
                           "Lowercased and dash-normalised into a URL-safe slug.",
        },
        "instruction": {
            "type": "string",
            "description": "Optional standing instruction prepended to every "
                           "event from this hook, e.g. 'when this fires, draft a "
                           "reply and text me'. Leave empty for a bare event.",
        },
    },
    "required": ["slug"],
}

_REVOKE_PARAMS = {
    "type": "object",
    "properties": {
        "slug": {"type": "string", "description": "The slug of the hook to permanently delete."},
    },
    "required": ["slug"],
}


async def _t_webhook_create(args: dict[str, Any], ctx: ToolContext) -> Any:
    slug = args.get("slug")
    if not slug:
        return {"error": "'slug' is required"}
    out = create_hook(str(slug), str(args.get("instruction") or ""))
    if out.get("error"):
        return out
    # Surface the URL + token together — this is the one moment the user sees
    # the token, and the URL is the whole point of the tool.
    return {
        "ok": True,
        "slug": out["slug"],
        "url": hook_url(out["slug"]),
        "token": out["token"],
        "header": "X-Hook-Token",
        "instruction": out.get("instruction") or "",
        "already_existed": out.get("already", False),
        "note": "Paste the URL into any tool that can POST. Send the token in "
                "the X-Hook-Token header (or ?token= if the tool can't set "
                "headers).",
    }


async def _t_webhook_list(args: dict[str, Any], ctx: ToolContext) -> Any:
    hooks = list_hooks()
    return {
        "hooks": [
            {
                "slug": h.get("slug"),
                "url": hook_url(h.get("slug", "")),
                "instruction": h.get("instruction") or "",
                "enabled": h.get("enabled", True),
                "created_at": h.get("created_at"),
                # Token deliberately masked here — it was shown once at create.
                "token_set": bool(h.get("token")),
            }
            for h in hooks
        ],
        "count": len(hooks),
    }


async def _t_webhook_revoke(args: dict[str, Any], ctx: ToolContext) -> Any:
    slug = args.get("slug")
    if not slug:
        return {"error": "'slug' is required"}
    return revoke_hook(str(slug))


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    """Register every persisted hook's handler + the three brain tools.

    On startup we re-bind a loopback path for each hook already on disk, so
    URLs minted in a previous daemon run keep working after a restart. New
    hooks register their path inline at create_hook() time."""
    for hook in _load_raw():
        slug = hook.get("slug")
        if slug and hook.get("enabled", True):
            _register_one(slug)
    log.info("webhook_inbox registered", hooks=len(_load_raw()))

    registry.register(Tool(
        name="webhook_create",
        description=(
            "Create a named inbound webhook for Sunday. Returns a URL the user "
            "pastes into ANY tool that can POST (a cold-email platform, Stripe, "
            "GitHub, Zapier, a cron job). When that URL is hit, Sunday reacts — "
            "optionally per an 'instruction' you attach ('when this fires, draft "
            "a reply and text me'). Use this when the user wants their agent to "
            "respond to an external event with no deploy or automation framework."
        ),
        parameters=_CREATE_PARAMS,
        run=_t_webhook_create,
    ))
    registry.register(Tool(
        name="webhook_list",
        description=(
            "List the user's existing inbound webhooks — slug, paste URL, "
            "standing instruction, enabled state. Tokens are masked (shown only "
            "once at creation)."
        ),
        parameters={"type": "object", "properties": {}},
        run=_t_webhook_list,
    ))
    registry.register(Tool(
        name="webhook_revoke",
        description=(
            "Permanently delete an inbound webhook by slug — kills its URL so "
            "any further POSTs 404. Use to rotate a leaked hook or retire one "
            "that's no longer needed; other hooks are untouched."
        ),
        parameters=_REVOKE_PARAMS,
        run=_t_webhook_revoke,
    ))
