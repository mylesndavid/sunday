"""Satellite token resolution: --token > $SUNDAY_AUTH_TOKEN > local file.

A satellite co-located with the daemon should authenticate with zero config
(reading ~/.sunday/auth.token), while explicit sources win when provided.
"""

from __future__ import annotations

import pytest

from sunday.devices.satellite import _resolve_token


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SUNDAY_HOME", str(tmp_path))
    monkeypatch.delenv("SUNDAY_AUTH_TOKEN", raising=False)
    return tmp_path


def _write_token(home, value):
    from sunday.paths import auth_token_path

    p = auth_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value, encoding="utf-8")
    return p


def test_cli_flag_wins(home, monkeypatch):
    monkeypatch.setenv("SUNDAY_AUTH_TOKEN", "from-env")
    _write_token(home, "from-file")
    assert _resolve_token("from-flag") == "from-flag"


def test_env_beats_file(home, monkeypatch):
    monkeypatch.setenv("SUNDAY_AUTH_TOKEN", "from-env")
    _write_token(home, "from-file")
    assert _resolve_token(None) == "from-env"


def test_falls_back_to_local_file(home):
    _write_token(home, "from-file\n")
    assert _resolve_token(None) == "from-file"


def test_none_when_nothing_available(home):
    assert _resolve_token(None) is None


def test_blank_sources_are_ignored(home, monkeypatch):
    monkeypatch.setenv("SUNDAY_AUTH_TOKEN", "   ")
    _write_token(home, "from-file")
    # whitespace-only flag and env are skipped; the file wins
    assert _resolve_token("   ") == "from-file"
