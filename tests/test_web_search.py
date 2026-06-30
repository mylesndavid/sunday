"""web_search — Perplexity Sonar research via OpenRouter.

Covers the two things that matter: it surfaces the synthesized answer PLUS the
source citations, and a missing OPENROUTER_API_KEY returns a clear, actionable
error instead of crashing the turn. The network call is mocked at _post_chat so
these tests never touch OpenRouter.
"""

from __future__ import annotations

import pytest

from sunday import web_search
from sunday.config import SundayConfig
from sunday.tools import CORE_TOOLS, default_registry


_FAKE_OPENROUTER_RESPONSE = {
    "id": "gen-xyz",
    "model": "perplexity/sonar",
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "The best lightweight tent is the Zpacks Duplex.",
            }
        }
    ],
    # Perplexity-via-OpenRouter parks sources in a top-level citations array.
    "citations": [
        "https://example.com/tent-reviews",
        "https://example.com/zpacks-duplex",
    ],
}


async def test_web_search_returns_answer_and_citations(monkeypatch):
    monkeypatch.setattr(web_search, "get_credential", lambda name: "sk-fake-key")

    captured = {}

    async def _fake_post(payload, key):
        captured["payload"] = payload
        captured["key"] = key
        return _FAKE_OPENROUTER_RESPONSE

    monkeypatch.setattr(web_search, "_post_chat", _fake_post)

    result = await web_search._t_web_search({"query": "best lightweight tent"}, None)

    assert "error" not in result
    assert result["answer"] == "The best lightweight tent is the Zpacks Duplex."
    assert result["citations"] == [
        "https://example.com/tent-reviews",
        "https://example.com/zpacks-duplex",
    ]
    assert result["model"] == web_search.SONAR_MODEL
    # It actually asked Sonar the user's question.
    assert captured["key"] == "sk-fake-key"
    assert captured["payload"]["model"] == web_search.SONAR_MODEL
    assert captured["payload"]["messages"][0]["content"] == "best lightweight tent"


async def test_web_search_missing_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(web_search, "get_credential", lambda name: None)

    async def _must_not_call(payload, key):  # pragma: no cover - asserts no network
        raise AssertionError("_post_chat must not run without a key")

    monkeypatch.setattr(web_search, "_post_chat", _must_not_call)

    result = await web_search._t_web_search({"query": "anything"}, None)

    assert "OPENROUTER_API_KEY" in result["error"]
    assert "answer" not in result


async def test_web_search_extracts_citations_from_annotations(monkeypatch):
    """Some responses carry url_citation annotations instead of a top-level
    citations array — surface those too."""
    monkeypatch.setattr(web_search, "get_credential", lambda name: "sk-fake-key")

    async def _fake_post(payload, key):
        return {
            "choices": [
                {
                    "message": {
                        "content": "Answer.",
                        "annotations": [
                            {"type": "url_citation", "url_citation": {"url": "https://src.example/a"}},
                        ],
                    }
                }
            ],
        }

    monkeypatch.setattr(web_search, "_post_chat", _fake_post)

    result = await web_search._t_web_search({"query": "q"}, None)
    assert result["citations"] == ["https://src.example/a"]


async def test_web_search_empty_query_errors(monkeypatch):
    result = await web_search._t_web_search({"query": "   "}, None)
    assert "error" in result


def test_web_search_is_registered_and_core():
    # Always-on so the agent reaches for it instead of driving a browser.
    assert "web_search" in CORE_TOOLS
    reg = default_registry(SundayConfig())
    assert reg.get("web_search") is not None
