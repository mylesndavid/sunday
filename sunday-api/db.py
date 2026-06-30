"""SQLite store — the source of truth for Sunday accounts and free-tier usage.

This is the entire persistent state of the hosted backend. Like the relay's
registry it is deliberately thin: it maps a WorkOS user to a stable Sunday
account (an `agent_id` + a `relay_token` + a `sunday_key`) and keeps a per-period
running tally of metered model tokens. Nothing here is agent data or message
content — it's identity + counters.

Why these three minted secrets, and why STABLE (the load-bearing reason):

  - `agent_id`   — the relay identity. Today the daemon mints this per-install,
    so a reinstall / new machine → new id → new relay URLs → the provider
    webhooks the user pasted break. Issuing it from the account instead makes the
    identity recoverable: sign in again on a fresh machine and you get the SAME
    `agent_id` back, so your relay URLs keep working.
  - `relay_token` — the daemon's WebSocket credential to the relay (what the
    relay's registry calls the "socket token"). Bound to the account so the relay
    can replace trust-on-first-use with account-gating.
  - `sunday_key`  — the bearer credential for THIS service: the model gateway and
    `/account`. Metered against the free-tier budget.

All three are minted ONCE on first sign-in and reused forever after — that reuse
is exactly what makes identity stable across reinstalls. We never re-mint for a
known WorkOS user.

Concurrency model (mirrors `relay-service/registry.py`'s discipline, adapted for
SQLite): aiohttp runs one event loop, but `sqlite3` calls block, so every DB
touch goes through `asyncio.to_thread` and is serialized under a single
`threading.Lock`. SQLite's own locking would mostly cover us, but the explicit
lock keeps read-modify-write sequences (mint-or-reuse, usage increment) atomic at
the application layer and free of `database is locked` surprises on the one small
box this is meant to run on. A flat JSON file (the relay's choice) isn't enough
here because we have counters that need atomic increments and a real uniqueness
constraint on `sunday_key`; SQLite on a Fly volume is the next rung up and still
"one small file on one small box."
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger("sunday-api.db")

# Where the SQLite file lives. Defaults to the Fly volume mount (/data) so the
# accounts DB survives restarts/redeploys; override for local dev. Mirrors the
# relay's RELAY_AGENTS_FILE-on-a-volume pattern.
SUNDAY_DB = os.environ.get("SUNDAY_DB", "/data/sunday.db")

# Secret strength for every minted credential. 32 bytes = 256 bits of entropy,
# url-safe base64 — the same "unguessable 256-bit value" posture the relay relies
# on for agent_id, applied to all three minted secrets here.
_SECRET_BYTES = 32


def _now_iso() -> str:
    """UTC timestamp for `created_at`. A normal service (not the workflow
    sandbox), so wall-clock time is fine and expected here."""
    return datetime.now(timezone.utc).isoformat()


def _mint() -> str:
    """One 256-bit url-safe secret. Used for agent_id, relay_token, sunday_key."""
    return secrets.token_urlsafe(_SECRET_BYTES)


class AccountStore:
    """SQLite-backed account + usage store. One instance per process, stashed on
    the aiohttp app. Async-safe via a lock + `asyncio.to_thread` (see module
    docstring): callers `await` the public methods, the blocking sqlite3 work
    runs off the event loop.

    Schema (created on first open, idempotent):

      accounts(
        workos_user_id TEXT PRIMARY KEY,   -- the WorkOS identity (who you are)
        email          TEXT,
        agent_id       TEXT UNIQUE,         -- stable relay identity
        relay_token    TEXT,                -- daemon's relay socket credential
        sunday_key     TEXT UNIQUE,         -- bearer for this service's gateway
        created_at     TEXT
      )
      usage(
        sunday_key TEXT,                    -- whose budget
        period     TEXT,                    -- "YYYY-MM" bucket
        tokens_in  INTEGER,
        tokens_out INTEGER,
        PRIMARY KEY(sunday_key, period)     -- one row per key per month
      )
    """

    def __init__(self, path: str) -> None:
        self._path = path
        # check_same_thread=False because `asyncio.to_thread` hands the work to
        # arbitrary worker threads; the explicit _lock (not sqlite's threading)
        # is what actually serializes access, so this is safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    # ─── construction ────────────────────────────────────────────────────────

    @classmethod
    def open(cls, path: str | None = None) -> "AccountStore":
        """Open (creating the file + parent dir if needed) the store at `path`
        or $SUNDAY_DB. A missing DB is normal on first boot — we create it."""
        path = path or SUNDAY_DB
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        store = cls(path)
        log.info("account store opened", path=path)
        return store

    def _init_schema(self) -> None:
        """Create the two tables if absent. Synchronous; only called from
        __init__ before the store is shared, so no lock needed yet."""
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                workos_user_id TEXT PRIMARY KEY,
                email          TEXT,
                agent_id       TEXT UNIQUE,
                relay_token    TEXT,
                sunday_key     TEXT UNIQUE,
                created_at     TEXT
            );
            CREATE TABLE IF NOT EXISTS usage (
                sunday_key TEXT,
                period     TEXT,
                tokens_in  INTEGER NOT NULL DEFAULT 0,
                tokens_out INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (sunday_key, period)
            );
            -- Indexes for the two hot lookup paths (gateway: by sunday_key;
            -- relay validation: by agent_id). PRIMARY KEY already indexes
            -- workos_user_id; UNIQUE already indexes agent_id/sunday_key, so
            -- these are mostly belt-and-suspenders / intent documentation.
            CREATE INDEX IF NOT EXISTS idx_accounts_sunday_key ON accounts(sunday_key);
            CREATE INDEX IF NOT EXISTS idx_accounts_agent_id   ON accounts(agent_id);
            """
        )
        self._conn.commit()

    # ─── public async API (everything below offloads the blocking sqlite work) ─

    async def upsert_account(self, workos_user_id: str, email: str) -> dict[str, Any]:
        """Mint-or-reuse the account for a WorkOS user. THE identity primitive.

        First sign-in for a `workos_user_id`: mint a fresh agent_id/relay_token/
        sunday_key, insert, return them. Every later sign-in (reinstall, new
        machine, new browser): we find the existing row and return the SAME
        secrets — which is what makes the Sunday identity stable/recoverable.
        Email is refreshed in case it changed upstream.

        Returns the account dict (workos_user_id, email, agent_id, relay_token,
        sunday_key, created_at).
        """
        return await asyncio.to_thread(self._upsert_account_sync, workos_user_id, email)

    def _upsert_account_sync(self, workos_user_id: str, email: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE workos_user_id = ?",
                (workos_user_id,),
            ).fetchone()
            if row is not None:
                # Known user → reuse the stable secrets. Only refresh the email
                # (it can change upstream) — never re-mint, or we'd break every
                # relay URL the user already pasted into a provider.
                if email and email != row["email"]:
                    self._conn.execute(
                        "UPDATE accounts SET email = ? WHERE workos_user_id = ?",
                        (email, workos_user_id),
                    )
                    self._conn.commit()
                account = dict(row)
                account["email"] = email or row["email"]
                log.info("account reused", workos_user_id=_redact(workos_user_id))
                return account

            # First sign-in → mint all three secrets. On the astronomically
            # unlikely UNIQUE collision (256-bit space), retry the whole mint.
            for _ in range(5):
                account = {
                    "workos_user_id": workos_user_id,
                    "email": email,
                    "agent_id": _mint(),
                    "relay_token": _mint(),
                    "sunday_key": _mint(),
                    "created_at": _now_iso(),
                }
                try:
                    self._conn.execute(
                        "INSERT INTO accounts "
                        "(workos_user_id, email, agent_id, relay_token, sunday_key, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            account["workos_user_id"],
                            account["email"],
                            account["agent_id"],
                            account["relay_token"],
                            account["sunday_key"],
                            account["created_at"],
                        ),
                    )
                    self._conn.commit()
                    log.info("account minted", workos_user_id=_redact(workos_user_id))
                    return account
                except sqlite3.IntegrityError:
                    # A concurrent insert for the SAME workos_user_id (PK clash)
                    # or a freak secret collision. Re-read: if the row now
                    # exists, return it (reuse wins); otherwise re-mint.
                    existing = self._conn.execute(
                        "SELECT * FROM accounts WHERE workos_user_id = ?",
                        (workos_user_id,),
                    ).fetchone()
                    if existing is not None:
                        return dict(existing)
                    continue
            raise RuntimeError("could not mint a unique account after retries")

    async def account_by_sunday_key(self, key: str) -> dict[str, Any] | None:
        """Look up an account by its `sunday_key` bearer credential. The gateway
        and `/account` hot path. Returns None for an unknown key (caller 401s)."""
        return await asyncio.to_thread(self._account_by_sync, "sunday_key", key)

    async def account_by_agent_id(self, agent_id: str) -> dict[str, Any] | None:
        """Look up an account by its stable `agent_id`. Used by the relay's
        /internal/validate-agent gate. None for unknown (caller 404s)."""
        return await asyncio.to_thread(self._account_by_sync, "agent_id", agent_id)

    def _account_by_sync(self, column: str, value: str) -> dict[str, Any] | None:
        # `column` is never user-supplied (only the two literals above), so the
        # f-string is safe; the VALUE is always parameterized.
        with self._lock:
            row = self._conn.execute(
                f"SELECT * FROM accounts WHERE {column} = ?",
                (value,),
            ).fetchone()
            return dict(row) if row is not None else None

    async def add_usage(self, sunday_key: str, period: str, tin: int, tout: int) -> None:
        """Atomically add metered tokens to this key's bucket for `period`
        ("YYYY-MM"). Upsert: the first call this month creates the row, later
        calls accumulate. Called after a successful gateway proxy."""
        await asyncio.to_thread(self._add_usage_sync, sunday_key, period, tin, tout)

    def _add_usage_sync(self, sunday_key: str, period: str, tin: int, tout: int) -> None:
        with self._lock:
            # INSERT … ON CONFLICT DO UPDATE = atomic upsert/increment in one
            # statement, so two near-simultaneous proxies can't lose a count.
            self._conn.execute(
                "INSERT INTO usage (sunday_key, period, tokens_in, tokens_out) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(sunday_key, period) DO UPDATE SET "
                "tokens_in = tokens_in + excluded.tokens_in, "
                "tokens_out = tokens_out + excluded.tokens_out",
                (sunday_key, period, int(tin), int(tout)),
            )
            self._conn.commit()

    async def usage_for(self, sunday_key: str, period: str) -> tuple[int, int]:
        """Return (tokens_in, tokens_out) for this key in `period`. (0, 0) if the
        key has no usage this month yet. Drives the free-tier gate + /account."""
        return await asyncio.to_thread(self._usage_for_sync, sunday_key, period)

    def _usage_for_sync(self, sunday_key: str, period: str) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT tokens_in, tokens_out FROM usage WHERE sunday_key = ? AND period = ?",
                (sunday_key, period),
            ).fetchone()
            if row is None:
                return (0, 0)
            return (int(row["tokens_in"]), int(row["tokens_out"]))


def _redact(value: str) -> str:
    """Log a prefix only — workos_user_id / secrets are sensitive, keep the full
    value out of logs that might ship off-box (same discipline as the relay)."""
    return (value[:8] + "…") if len(value) > 8 else value
