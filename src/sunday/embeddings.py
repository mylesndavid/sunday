"""Local embeddings for memory recall — strictly on-device, never a cloud call.

Resolution ladder (first that answers wins):
  1. SUNDAY_EMBED_URL            — explicit endpoint (Ollama-style or OpenAI-style)
  2. Ollama at 127.0.0.1:11434   — if the user happens to run it
  3. llama-server at 127.0.0.1:18181 — Sunday's own managed runtime (the same
     one the fully-local chat mode will use; auto-install lands with that)
  4. None — recall silently degrades to FTS-only, which is exactly the old
     behavior. Memory quality scales with what's present; nothing breaks.

Default model: embeddinggemma (Google, ~300M, built for on-device). Benchmarked
on the user's real fact corpus at 100% R@5 with weighted-RRF hybrid recall vs
76.7% for FTS alone (see findings_agentmemory_spike). EmbeddingGemma REQUIRES
its task prefixes — without them retrieval quality drops measurably.

History note: Sunday once embedded every message via OpenAI in the hot path and
it was ripped out (network round-trip per turn). This is different on every
axis: local-only, recall-path-only (a deliberate tool call), and store-side
indexing is background + best-effort.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import structlog

log = structlog.get_logger("sunday.embeddings")

EMBED_MODEL = os.environ.get("SUNDAY_EMBED_MODEL", "embeddinggemma")
_EXPLICIT_URL = (os.environ.get("SUNDAY_EMBED_URL") or "").strip().rstrip("/")
_OLLAMA = "http://127.0.0.1:11434"
_LLAMA_SERVER = "http://127.0.0.1:18181"
_PROBE_TIMEOUT = 1.5
_EMBED_TIMEOUT = 90.0          # first call may cold-load the model
_REPROBE_AFTER = 60.0          # when nothing local is up, don't hammer

# EmbeddingGemma is trained with task prefixes; skipping them costs recall.
_PREFIXES = {
    "embeddinggemma": {"query": "task: search result | query: ", "document": "title: none | text: "},
}


def _prefix(kind: str) -> str:
    base = EMBED_MODEL.split(":")[0]
    return _PREFIXES.get(base, {}).get(kind, "")


class LocalEmbedder:
    """Probes for a local embedding endpoint and embeds through it. All
    failures are soft: embed() returns None and the caller falls back to FTS."""

    def __init__(self) -> None:
        self._provider: tuple[str, str] | None = None   # (base_url, style)
        self._failed_at = 0.0
        self._pull_started = False
        self._lock = asyncio.Lock()

    async def _probe(self) -> tuple[str, str] | None:
        candidates: list[tuple[str, str]] = []
        if _EXPLICIT_URL:
            candidates += [(_EXPLICIT_URL, "ollama"), (_EXPLICIT_URL, "openai")]
        candidates += [(_OLLAMA, "ollama"), (_LLAMA_SERVER, "openai")]
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as c:
            for base, style in candidates:
                try:
                    path = "/api/tags" if style == "ollama" else "/health"
                    r = await c.get(f"{base}{path}")
                    if r.status_code < 500:
                        log.info("embedder resolved", base=base, style=style, model=EMBED_MODEL)
                        return (base, style)
                except Exception:  # noqa: BLE001
                    continue
        return None

    async def _ensure_model(self, base: str) -> None:
        """Ollama-only: if the embed model isn't pulled, fetch it once in the
        background (same silent-bootstrap pattern as whisper.cpp). ~600MB,
        local, free — memory recall upgrades itself when it lands."""
        if self._pull_started:
            return
        self._pull_started = True

        async def _pull() -> None:
            try:
                async with httpx.AsyncClient(timeout=None) as c:
                    log.info("pulling embed model in background", model=EMBED_MODEL)
                    await c.post(f"{base}/api/pull", json={"model": EMBED_MODEL})
                    log.info("embed model ready", model=EMBED_MODEL)
            except Exception as exc:  # noqa: BLE001
                log.warning("embed model pull failed", error=str(exc))
        asyncio.create_task(_pull())

    async def embed(self, texts: list[str], kind: str = "document") -> list[list[float]] | None:
        """Embed texts (with the model's task prefix for `kind`). Returns None
        when no local provider is available — caller falls back to FTS."""
        if not texts:
            return []
        async with self._lock:
            if self._provider is None:
                if time.time() - self._failed_at < _REPROBE_AFTER:
                    return None
                self._provider = await self._probe()
                if self._provider is None:
                    self._failed_at = time.time()
                    return None
        base, style = self._provider
        payload_texts = [_prefix(kind) + t for t in texts]
        try:
            async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as c:
                if style == "ollama":
                    r = await c.post(f"{base}/api/embed",
                                     json={"model": EMBED_MODEL, "input": payload_texts})
                    if r.status_code == 404 or (r.status_code == 400 and "not found" in r.text):
                        await self._ensure_model(base)
                        return None
                    r.raise_for_status()
                    return r.json()["embeddings"]
                r = await c.post(f"{base}/v1/embeddings",
                                 json={"model": EMBED_MODEL, "input": payload_texts})
                r.raise_for_status()
                return [d["embedding"] for d in r.json()["data"]]
        except Exception as exc:  # noqa: BLE001
            log.warning("embed failed; falling back to FTS", error=str(exc)[:120])
            self._provider = None
            self._failed_at = time.time()
            return None


_embedder: LocalEmbedder | None = None


def get_embedder() -> LocalEmbedder:
    global _embedder
    if _embedder is None:
        _embedder = LocalEmbedder()
    return _embedder
