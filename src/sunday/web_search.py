"""web_search — research the web in ONE API call, not by driving a browser.

This is the fix for the "research by clicking through pages" failure mode: the
brain used to open the browser page-by-page to read things, which spawned a
swarm of Chrome instances and hijacked the screen. Research is not interaction;
it's a question that wants a synthesized, cited answer. So we hand the question
to Perplexity Sonar via OpenRouter and get back exactly that — an answer plus
the sources it used — in a single round-trip.

The browser (cockpit / CDP / device_open_url) stays for ACTUALLY doing things
on a site: logging in, filling forms, clicking through a flow. It is not for
bulk reading.

Auth/client style mirrors runtime/providers/openai_compat.py exactly:
OPENROUTER_API_KEY via get_credential, base_url https://openrouter.ai/api/v1,
the HTTP-Referer / X-Title headers OpenRouter wants. We POST raw with httpx
(like embeddings.py) rather than the openai SDK so the non-standard `citations`
array Perplexity returns is trivial — and robust — to read off the response.

Future: a read_url(markdown) tool (e.g. Firecrawl) could complement this for
fetching ONE specific page as clean markdown — deferred, owner said "maybe".
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from sunday.config import SundayConfig
from sunday.credentials import get_credential
from sunday.tools import Tool, ToolContext, ToolRegistry

log = structlog.get_logger("sunday.web_search")

# Cheap default. Swap to "perplexity/sonar-pro" for deeper, pricier research —
# this single constant is the only thing to change.
SONAR_MODEL = "perplexity/sonar"

_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_TIMEOUT = 45.0  # research can take a while; cap it so a turn never hangs

_MISSING_KEY_ERROR = (
    "Research needs OPENROUTER_API_KEY — set it in credentials "
    "(sunday credential set OPENROUTER_API_KEY <key>)."
)


async def _post_chat(payload: dict[str, Any], key: str) -> dict[str, Any]:
    """POST to OpenRouter chat-completions and return the parsed JSON body.

    Isolated so tests can monkeypatch the network call without touching the
    answer/citation parsing below."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.post(
            f"{_OPENROUTER_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "https://sunday.local",
                "X-Title": "Sunday",
            },
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


def _extract_citations(data: dict[str, Any], message: dict[str, Any]) -> list[str]:
    """Pull source URLs out wherever the provider parked them.

    Perplexity-via-OpenRouter returns a top-level `citations` array of URL
    strings; some responses instead (or also) carry `search_results` and/or
    OpenAI-style `annotations` with url_citation objects. Be liberal — surfacing
    a source the user can click is the whole point — and dedupe in order."""
    urls: list[str] = []

    def _add(u: Any) -> None:
        if isinstance(u, str) and u.strip() and u not in urls:
            urls.append(u.strip())

    for c in data.get("citations") or []:
        if isinstance(c, str):
            _add(c)
        elif isinstance(c, dict):
            _add(c.get("url"))

    for r in data.get("search_results") or []:
        if isinstance(r, dict):
            _add(r.get("url"))

    for ann in (message.get("annotations") or []):
        if isinstance(ann, dict):
            cit = ann.get("url_citation") or ann
            if isinstance(cit, dict):
                _add(cit.get("url"))

    return urls


async def _t_web_search(args: dict[str, Any], ctx: ToolContext) -> Any:
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "'query' is required — ask a natural-language question."}

    key = get_credential("OPENROUTER_API_KEY")
    if not key:
        return {"error": _MISSING_KEY_ERROR}

    payload = {
        "model": SONAR_MODEL,
        "messages": [{"role": "user", "content": query}],
    }

    try:
        data = await _post_chat(payload, key)
    except httpx.HTTPStatusError as exc:
        body = ""
        try:
            body = exc.response.text[:300]
        except Exception:  # noqa: BLE001
            pass
        log.warning("web_search HTTP error", status=exc.response.status_code, body=body)
        return {"error": f"Research request failed (HTTP {exc.response.status_code}). {body}".strip()}
    except Exception as exc:  # noqa: BLE001 — never crash the turn over a lookup
        log.warning("web_search failed", error=str(exc)[:200])
        return {"error": f"Research request failed: {type(exc).__name__}: {exc}"}

    try:
        choices = data.get("choices") or []
        message = (choices[0].get("message") if choices else {}) or {}
        answer = (message.get("content") or "").strip()
        citations = _extract_citations(data, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("web_search parse failed", error=str(exc)[:200])
        return {"error": f"Could not read the research response: {type(exc).__name__}: {exc}"}

    if not answer:
        return {"error": "Research returned no answer.", "citations": citations}

    return {"answer": answer, "citations": citations, "model": SONAR_MODEL}


def register(registry: ToolRegistry, config: SundayConfig) -> None:
    registry.register(Tool(
        name="web_search",
        description=(
            "Research the web and get a cited answer — use this for looking "
            "things up / 'find me…' / 'what's the best…' instead of opening a "
            "browser. Pass a natural-language question; returns a synthesized "
            "answer plus the source URLs it cited, in one call. This is the "
            "RIGHT tool for any read-only research ('look up', 'find', "
            "'research', 'reviews of', 'compare', 'what's the best/latest'). Do "
            "NOT open the cockpit, a CDP browser, or device_open_url to read "
            "pages for research — that's slow and clutters the screen. Reserve "
            "the browser for actually interacting with a site (logging in, "
            "filling forms, clicking through a flow)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "A natural-language question to research.",
                },
            },
            "required": ["query"],
        },
        run=_t_web_search,
    ))
