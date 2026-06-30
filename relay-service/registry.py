"""Agent registry — the relay's only persistent state, and it's deliberately
thin: just `agent_id -> {token, per-slug tokens}`. Nothing here is agent data;
it's pure routing credentials (spec §3).

Two separate credentials live here (spec §3.2), and keeping them separate is the
whole security story:

  1. The *socket token* (`token`) authenticates the daemon's WebSocket — it
     proves "I am agent X's daemon." Verified on `hello`.
  2. The *webhook authorization* is the unguessable `agent_id` in the public URL
     plus an OPTIONAL per-slug token (and an optional HMAC for providers that
     sign). It proves "this POST is allowed to reach agent X."

Compromising one never grants the other: a leaked public URL doesn't give you
the socket, and the socket token never appears in any URL.

How an agent gets here (spec §2 "Identity & registration"):
  - The DAEMON mints both credentials on first relay-enable: a 256-bit
    `RELAY_AGENT_ID` and a 256-bit `RELAY_TOKEN`, persisted to the user's
    `~/.sunday/credentials.env`. The relay never mints them — that keeps secret
    generation on the user's machine, not the shared box.
  - The daemon then registers `(agent_id, token)` with the relay. For v1 we
    support two registration mechanisms, both file-backed and dead simple:
      a) a persisted `agents.json` store the operator edits / the daemon writes
         via the admin route, and
      b) an `ADMIN_TOKEN`-gated POST so a daemon can self-register idempotently
         (mirrors Sendblue's GET-then-POST idempotency).

This file stays storage-dumb on purpose: a flat JSON file is enough for the
single-small-box deploy the spec leans toward (§8), and it's trivially swappable
for Redis/Postgres later without touching server.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from typing import Any

import structlog

log = structlog.get_logger("relay.registry")

# Where the flat-file registry lives. Override for a mounted volume in Docker.
AGENTS_FILE = os.environ.get("RELAY_AGENTS_FILE", "agents.json")


class AgentRegistry:
    """In-memory registry backed by a JSON file. Thread-safe enough for the
    aiohttp single-loop model plus the rare admin write.

    File shape (agents.json):
      {
        "agents": {
          "<agent_id>": {
            "token": "<socket-token>",
            "slugs": {                    # optional per-slug authorization
              "stripe":   {"hmac_secret": "whsec_..."},
              "coldhook": {"token": "..."}
            }
          }
        }
      }
    """

    def __init__(self, agents: dict[str, Any], path: str | None) -> None:
        self._agents = agents
        self._path = path
        self._lock = threading.Lock()

    # ─── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> "AgentRegistry":
        """Load from AGENTS_FILE if present; start empty otherwise. A missing
        file is normal on first boot — daemons self-register into it."""
        path = AGENTS_FILE
        agents: dict[str, Any] = {}
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                agents = data.get("agents", {}) if isinstance(data, dict) else {}
                log.info("registry loaded", path=path, agents=len(agents))
            except (OSError, ValueError) as exc:
                # A corrupt registry shouldn't take the service down — start
                # empty and let daemons re-register. Loud, but non-fatal.
                log.error("registry load failed, starting empty", path=path, error=str(exc))
        else:
            log.info("registry file absent, starting empty", path=path)
        return cls(agents, path)

    # ─── socket auth (spec §3.2, credential #1) ──────────────────────────────

    def has_agent(self, agent_id: str) -> bool:
        """Is this agent_id already enrolled? Lets the socket handler tell a
        brand-new daemon (enroll it — trust-on-first-use) apart from a known
        one presenting the wrong token (reject). The agent_id is an unguessable
        256-bit value, so first-write-wins enrollment is safe: nobody can claim
        an id they can't guess, and the bound token gates every later connect."""
        return agent_id in self._agents

    def verify_token(self, agent_id: str, token: str) -> bool:
        """Authenticate a daemon's `hello`. Constant-time compare so the relay
        doesn't leak token bytes via timing. Unknown agent -> False."""
        record = self._agents.get(agent_id)
        if not record:
            return False
        expected = str(record.get("token") or "")
        if not expected:
            return False
        return hmac.compare_digest(expected, token)

    # ─── webhook authorization (spec §3.2/§3.3, credential #2) ────────────────

    def authorize_webhook(
        self,
        agent_id: str,
        slug: str,
        *,
        query_token: str | None,
        header_token: str | None,
        body: bytes,
        headers: Any,
    ) -> bool:
        """Fine-grained gate on top of the coarse unguessable-agent_id gate.

        Resolution, per slug:
          - No per-slug config  -> ALLOW. The unguessable path is the gate
            (spec §3.3, "for those that don't sign, the path is the gate").
          - `token` configured   -> require an exact match from ?token= or the
            X-Relay-Token header (constant-time). This is the per-hook token in
            the arbitrary-webhook story (spec §4): rotate/kill one hook without
            touching others.
          - `hmac_secret` configured -> verify the provider's signature header
            (HMAC-SHA256 hex) over the raw body. Wired below for the common
            shape; left as a clearly-marked hook for per-provider quirks.

        Returns True to forward, False to 403.
        """
        record = self._agents.get(agent_id)
        if not record:
            # Shouldn't happen (the caller already found a live socket, which
            # implies a registered agent), but fail closed.
            return False
        slug_cfg = (record.get("slugs") or {}).get(slug)
        if not slug_cfg:
            # Identity slug, no extra gate — the agent_id was the gate.
            return True

        # ── per-slug shared token ────────────────────────────────────────────
        expected_token = str(slug_cfg.get("token") or "")
        if expected_token:
            supplied = query_token or header_token or ""
            if not hmac.compare_digest(expected_token, supplied):
                return False
            return True

        # ── per-slug HMAC (providers that sign: Stripe, AgentMail, …) ────────
        hmac_secret = str(slug_cfg.get("hmac_secret") or "")
        if hmac_secret:
            return self._verify_hmac(slug_cfg, hmac_secret, body, headers)

        # slug_cfg present but empty -> treat as no extra gate.
        return True

    def _verify_hmac(self, slug_cfg: dict[str, Any], secret: str, body: bytes, headers: Any) -> bool:
        """Verify an HMAC-SHA256 signature over the raw body.

        This is the spec §3.3 edge-verify hook. The COMMON shape is implemented:
        a hex HMAC-SHA256 of the raw body in a configurable header (default
        `X-Signature`). Providers with bespoke canonicalization (Stripe prefixes
        a timestamp: `t=...,v1=...`; some base64-encode) need their own
        canonicalizer — add it here, keyed off the provider, rather than bending
        the generic path. We compute over the OPAQUE raw bytes precisely so the
        relay still never has to parse the payload.
        """
        sig_header = str(slug_cfg.get("hmac_header") or "X-Signature")
        provided = ""
        # Case-insensitive header lookup (aiohttp's CIMultiDict already is, but
        # this also works for a plain dict in tests).
        try:
            provided = headers.get(sig_header) or ""
        except Exception:  # noqa: BLE001
            provided = ""
        if not provided:
            return False

        computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

        # ── PER-PROVIDER CANONICALIZATION HOOK ───────────────────────────────
        # Uncomment / extend for providers whose signature isn't a bare hex
        # HMAC of the raw body. Examples:
        #
        #   # Stripe: header is "t=<ts>,v1=<hex>"; sign "<ts>.<body>".
        #   if slug_cfg.get("provider") == "stripe":
        #       parts = dict(p.split("=", 1) for p in provided.split(","))
        #       signed = f"{parts['t']}.".encode() + body
        #       computed = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
        #       provided = parts.get("v1", "")
        #
        #   # AgentMail: confirm header name + encoding against their docs and
        #   # set hmac_header / a base64 flag here.
        # ─────────────────────────────────────────────────────────────────────

        return hmac.compare_digest(computed, provided)

    # ─── slug -> local path (spec §2; stays dumb) ────────────────────────────

    def local_path(self, slug: str) -> str:
        """Map a public slug to the local /webhooks path the daemon delivers to.

        Default is IDENTITY (`slug` -> `/webhooks/<slug>`) — the relay stays
        dumb and lets the daemon do any real remap (spec §2). A registry-wide
        override map exists only for the blessed exception (e.g. baking a
        sendblue secret into the path) and is intentionally minimal. Most
        deploys never touch it.
        """
        overrides = self._agents.get("_paths") or {}
        if isinstance(overrides, dict) and slug in overrides:
            return str(overrides[slug])
        # Identity. Strip leading slashes so "/foo" can't escape the prefix.
        clean = slug.strip("/")
        return f"/webhooks/{clean}"

    # ─── registration (spec §2 "Identity & registration") ────────────────────

    def register(self, agent_id: str, token: str, slugs: dict[str, Any] | None = None) -> None:
        """Idempotent upsert of an agent. Called by the admin route when a
        daemon self-registers. Persists to AGENTS_FILE so a restart keeps the
        mapping (the relay holds no other state, so losing this would orphan
        every daemon until they reconnect-and-reregister)."""
        with self._lock:
            record = self._agents.get(agent_id) or {}
            record["token"] = token
            if slugs is not None:
                record["slugs"] = slugs
            self._agents[agent_id] = record
            self._persist_locked()
        log.info("agent registered", agent_id=(agent_id[:8] + "…"))

    def _persist_locked(self) -> None:
        """Write the registry to disk. Caller holds the lock. Atomic-ish via a
        temp-file rename so a crash mid-write can't corrupt the live file."""
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"agents": self._agents}, fh, indent=2)
        os.replace(tmp, self._path)
