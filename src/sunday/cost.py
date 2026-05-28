"""Cost meter — per-call LLM + transcription spend tracking.

We can't tune what we can't see. Every LLM completion and every Whisper
transcription writes one row here with (purpose, model, tokens, est cost,
latency). Aggregated views power a CLI + an HTTP endpoint, so it's obvious
which subsystem is actually expensive before anyone "optimizes" the wrong one.

Design notes:
- SQLite at ~/.sunday/cost.db. Append-only; no rollups (cheap to compute on read).
- Pricing is a static table keyed by model substring — providers rename models
  frequently, so unknowns degrade to $0 estimate rather than crash. The token
  counts remain accurate either way.
- `purpose` is a free-text tag set by the caller (e.g. "chat", "extract_facts",
  "graph_rebuild", "observer_tick"). This is the lens we group spend by.
- Local-only: cost data never leaves the user's machine.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from sunday.paths import sunday_home

log = structlog.get_logger("sunday.cost")


# ---------------------------------------------------------------------------
# Pricing table. USD per 1M tokens (input, output). Whisper is per minute.
# Values are best-effort from each provider's published pricing as of model
# launch; refresh when a model changes. Unknown models log with cost=0 — the
# token counts remain accurate, so you can spot a missing entry by seeing
# many tokens flow against a $0 model and add it here.
# ---------------------------------------------------------------------------
PRICING_PER_M: dict[str, tuple[float, float]] = {
    # DeepSeek (via OpenRouter or direct)
    "deepseek-chat":              (0.27, 1.10),
    "deepseek-v3.2-exp":          (0.27, 0.40),
    "deepseek-v3.1-terminus":     (0.27, 0.40),
    "deepseek-v4-flash":          (0.05, 0.40),
    "deepseek-v4":                (0.27, 1.10),
    "deepseek-reasoner":          (0.55, 2.19),
    "deepseek-r1":                (0.55, 2.19),
    # xAI Grok via OpenRouter
    "grok-4-fast":                (0.20, 0.50),
    "grok-4":                     (3.00, 15.00),
    # OpenAI
    "gpt-4o-mini":                (0.15, 0.60),
    "gpt-4o":                     (2.50, 10.00),
    "gpt-4.1-mini":               (0.40, 1.60),
    "gpt-4.1":                    (2.00, 8.00),
    "gpt-5":                      (1.25, 10.00),
    "gpt-5-mini":                 (0.25, 2.00),
    # Anthropic via OpenRouter
    "claude-3.5-haiku":           (0.80, 4.00),
    "claude-3.5-sonnet":          (3.00, 15.00),
    "claude-sonnet-4":            (3.00, 15.00),
    # Google
    "gemini-2.0-flash":           (0.10, 0.40),
    "gemini-2.5-flash":           (0.30, 2.50),
    # Kimi
    "kimi-k2":                    (0.15, 2.50),
}

# Whisper / transcription: USD per minute of audio
WHISPER_PER_MIN: dict[str, float] = {
    "whisper-1":    0.006,
    "whisper-large": 0.006,
    "gpt-4o-transcribe": 0.006,
    "gpt-4o-mini-transcribe": 0.003,
}


def _match(model: str, table: dict[str, Any]) -> Any | None:
    """Case-insensitive substring match. Provider prefixes (e.g. 'deepseek/')
    get stripped before matching against the table keys."""
    if not model:
        return None
    key = model.lower().split("/")[-1]
    if key in table:
        return table[key]
    for k, v in table.items():
        if k in key:
            return v
    return None


def estimate_llm_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    rate = _match(model, PRICING_PER_M)
    if rate is None:
        return 0.0
    in_rate, out_rate = rate
    return (prompt_tokens / 1_000_000) * in_rate + (completion_tokens / 1_000_000) * out_rate


def estimate_audio_cost(model: str, duration_seconds: float) -> float:
    rate = _match(model, WHISPER_PER_MIN)
    if rate is None:
        return 0.0
    return (duration_seconds / 60.0) * rate


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass
class CostEvent:
    ts: float
    kind: str             # "llm" | "audio"
    purpose: str          # caller-supplied tag
    provider: str         # "openrouter" | "openai" | "deepseek-direct" | "observer" ...
    model: str
    prompt_tokens: int    # 0 for audio
    completion_tokens: int  # 0 for audio
    audio_seconds: float  # 0.0 for llm
    cost_usd: float
    latency_ms: int


class CostStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or (sunday_home() / "cost.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()

    def _init(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS cost_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                purpose TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                audio_seconds REAL NOT NULL DEFAULT 0.0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                latency_ms INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_cost_ts ON cost_events(ts);
            CREATE INDEX IF NOT EXISTS idx_cost_purpose ON cost_events(purpose);
        """)
        self._conn.commit()

    def log(self, event: CostEvent) -> None:
        self._conn.execute(
            """INSERT INTO cost_events
               (ts, kind, purpose, provider, model, prompt_tokens, completion_tokens,
                audio_seconds, cost_usd, latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (event.ts, event.kind, event.purpose, event.provider, event.model,
             event.prompt_tokens, event.completion_tokens, event.audio_seconds,
             event.cost_usd, event.latency_ms),
        )
        self._conn.commit()

    def log_llm(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
    ) -> CostEvent:
        ev = CostEvent(
            ts=time.time(),
            kind="llm",
            purpose=purpose or "unknown",
            provider=provider or "unknown",
            model=model or "unknown",
            prompt_tokens=int(prompt_tokens or 0),
            completion_tokens=int(completion_tokens or 0),
            audio_seconds=0.0,
            cost_usd=estimate_llm_cost(model, prompt_tokens or 0, completion_tokens or 0),
            latency_ms=int(latency_ms or 0),
        )
        self.log(ev)
        return ev

    def log_audio(
        self,
        *,
        purpose: str,
        provider: str,
        model: str,
        duration_seconds: float,
        latency_ms: int,
    ) -> CostEvent:
        ev = CostEvent(
            ts=time.time(),
            kind="audio",
            purpose=purpose or "unknown",
            provider=provider or "unknown",
            model=model or "unknown",
            prompt_tokens=0,
            completion_tokens=0,
            audio_seconds=float(duration_seconds or 0.0),
            cost_usd=estimate_audio_cost(model, duration_seconds or 0.0),
            latency_ms=int(latency_ms or 0),
        )
        self.log(ev)
        return ev

    # ------------------------------------------------------------------ reads

    def summary(self, since: float) -> dict[str, Any]:
        """Aggregate spend since `since` (unix seconds).

        Returns totals plus three breakdowns: by purpose, by model, and by
        hour. Cheap — single SQLite scan; no caching needed at our scale.
        """
        rows = self._conn.execute(
            "SELECT ts, kind, purpose, provider, model, prompt_tokens, "
            "completion_tokens, audio_seconds, cost_usd, latency_ms "
            "FROM cost_events WHERE ts >= ?",
            (since,),
        ).fetchall()

        total_cost = 0.0
        total_calls = 0
        by_purpose: dict[str, dict[str, Any]] = {}
        by_model: dict[str, dict[str, Any]] = {}
        for ts, kind, purpose, provider, model, pt, ct, aud, cost, lat in rows:
            total_cost += cost
            total_calls += 1
            p = by_purpose.setdefault(purpose, {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "audio_seconds": 0.0})
            p["calls"] += 1
            p["cost_usd"] += cost
            p["prompt_tokens"] += pt
            p["completion_tokens"] += ct
            p["audio_seconds"] += aud
            m = by_model.setdefault(model, {"calls": 0, "cost_usd": 0.0})
            m["calls"] += 1
            m["cost_usd"] += cost

        return {
            "since": since,
            "total_cost_usd": round(total_cost, 6),
            "total_calls": total_calls,
            "by_purpose": dict(sorted(by_purpose.items(), key=lambda kv: -kv[1]["cost_usd"])),
            "by_model":   dict(sorted(by_model.items(),   key=lambda kv: -kv[1]["cost_usd"])),
        }

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, kind, purpose, provider, model, prompt_tokens, "
            "completion_tokens, audio_seconds, cost_usd, latency_ms "
            "FROM cost_events ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "ts": ts, "kind": kind, "purpose": purpose, "provider": provider,
                "model": model, "prompt_tokens": pt, "completion_tokens": ct,
                "audio_seconds": aud, "cost_usd": cost, "latency_ms": lat,
            }
            for ts, kind, purpose, provider, model, pt, ct, aud, cost, lat in rows
        ]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass


# Single process-wide instance — the daemon holds it, providers reach for it.
_STORE: CostStore | None = None


def get_store() -> CostStore:
    global _STORE
    if _STORE is None:
        _STORE = CostStore()
    return _STORE
