"""Gemini vision backend for the Timeline summarizer.

An alternative to the local codex/claude CLI: send screenshots to Google's
Gemini Flash — cheap, fast, generous free tier. It's the "I don't want to burn
my whole Codex subscription on a 900-frame backlog" option: per-token, but
pennies (or free) for this workload.

Unlike the CLI path, images DO leave the Mac (to Google), so it's opt-in — the
user picks it in Settings › Timeline and supplies a key. Same return shape as
`chat_cli.run` so the summarizer can treat all backends uniformly.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger("sunday.devices.gemini_vision")

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.0-flash"


def available() -> bool:
    from sunday.credentials import get_credential
    return bool(get_credential("GEMINI_API_KEY"))


async def run(
    prompt: str,
    image_paths: list[str] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """One-shot generation with optional screenshots. Returns
    {"ok": bool, "text": str, "tool": "gemini", "error": str|None}."""
    if not api_key:
        return {"ok": False, "text": "", "tool": "gemini", "error": "no GEMINI_API_KEY set"}
    model = (model or DEFAULT_MODEL).strip()
    parts: list[dict[str, Any]] = [{"text": prompt}]
    for p in (image_paths or []):
        try:
            data = base64.b64encode(Path(p).read_bytes()).decode("ascii")
        except Exception:  # noqa: BLE001 — skip an unreadable frame, keep the rest
            continue
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": data}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.2},
    }
    url = f"{API_BASE}/{model}:generateContent?key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.post(url, json=body)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "tool": "gemini", "error": f"gemini request failed: {exc}"}
    if res.status_code != 200:
        # Surface the API's own error message (bad key, quota, bad model) trimmed.
        return {"ok": False, "text": "", "tool": "gemini",
                "error": f"gemini HTTP {res.status_code}: {res.text[:200]}"}
    try:
        data = res.json()
        cands = data.get("candidates") or []
        out_parts = (cands[0].get("content") or {}).get("parts") or [] if cands else []
        text = "".join(p.get("text", "") for p in out_parts if isinstance(p, dict)).strip()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "tool": "gemini", "error": f"gemini parse failed: {exc}"}
    return {"ok": True, "text": text, "tool": "gemini", "error": None}
