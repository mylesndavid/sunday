"""Model / provider / thinking choices survive a daemon restart.

Model and provider used to live only in the daemon's memory, so every restart
silently snapped back to the built-in default — you'd pick a model, use it, and
find it reverted later with no indication anything had happened.

The store must also never be able to break startup: a corrupt file, or a
provider string that isn't valid any more, has to degrade to the defaults
rather than raising out of load_config().
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    """A config module rooted at a throwaway SUNDAY_HOME."""
    monkeypatch.setenv("SUNDAY_HOME", str(tmp_path))
    import sunday.config as C
    importlib.reload(C)
    yield C
    importlib.reload(C)


def test_saved_model_is_restored(cfg):
    assert cfg.load_config().model.name == "gpt-5.5"        # built-in default
    cfg.save_model_prefs(name="gpt-5.6-luna")
    assert cfg.load_config().model.name == "gpt-5.6-luna"


def test_saved_thinking_is_restored(cfg):
    cfg.save_model_prefs(reasoning=True, effort="high")
    model = cfg.load_config().model
    assert (model.reasoning, model.reasoning_effort) == (True, "high")


def test_thinking_off_is_restored_as_off(cfg):
    """`reasoning=False` is falsy — it must still round-trip, not be skipped."""
    cfg.save_model_prefs(reasoning=False)
    assert cfg.load_config().model.reasoning is False


def test_recents_are_most_recent_first_and_deduped(cfg):
    for name in ("a/one", "b/two", "a/one", "c/three"):
        cfg.save_model_prefs(name=name)
    assert cfg.load_model_prefs()["recent"] == ["c/three", "a/one", "b/two"]


def test_recents_are_capped(cfg):
    for i in range(12):
        cfg.save_model_prefs(name=f"m/{i}")
    assert len(cfg.load_model_prefs()["recent"]) == cfg._RECENT_MAX


def test_valid_provider_is_restored(cfg):
    cfg.save_model_prefs(provider="openrouter")
    assert cfg.load_config().model.provider == "openrouter"


def test_bogus_provider_is_ignored(cfg):
    """A provider we no longer support must not be adopted — building a runtime
    for it would fail, taking the whole daemon down on boot."""
    cfg.save_model_prefs(provider="not-a-provider")
    assert cfg.load_config().model.provider == "openai"


def test_codex_is_skipped_when_not_signed_in_on_this_host(cfg, monkeypatch):
    """Codex needs a ~/.codex login on THIS machine. Restoring it on a box that
    isn't signed in would boot straight into a broken runtime."""
    monkeypatch.setattr("sunday.runtime.providers.codex.codex_available", lambda: False)
    cfg.save_model_prefs(provider="codex")
    assert cfg.load_config().model.provider == "openai"


def test_corrupt_store_falls_back_to_defaults(cfg, tmp_path):
    (tmp_path / "model.json").write_text("{{{ not json", encoding="utf-8")
    assert cfg.load_config().model.name == "gpt-5.5"          # no raise


def test_legacy_thinking_store_is_still_read(cfg, tmp_path):
    """Boxes set up before model.json existed keep their thinking level."""
    (tmp_path / "thinking.json").write_text('{"reasoning": true, "effort": "high"}', encoding="utf-8")
    assert cfg.load_config().model.reasoning_effort == "high"
