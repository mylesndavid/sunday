"""First-party VAPI phone calling: the call_phone tool's missing-credential
errors and the /v1/vapi/status endpoint shape the Settings card reads."""

import asyncio
import json

import pytest

from sunday.channels import vapi
from sunday.config import SundayConfig
from sunday.tools import ToolContext


class _NoopChat:
    def append(self, *a, **k):  # pragma: no cover - unused here
        return None


def _ctx() -> ToolContext:
    return ToolContext(chat=_NoopChat(), config=SundayConfig(), modality="vapi")


def _no_creds(monkeypatch):
    """No VAPI credentials present (and no env leakage)."""
    monkeypatch.setattr(vapi, "get_credential", lambda name, *a, **k: None)


# ─── call_phone tool: clear errors when creds are missing ──────────────────


def test_call_phone_requires_to_and_purpose(monkeypatch):
    _no_creds(monkeypatch)
    out = asyncio.run(vapi._t_call_phone({"to": "+15551234567"}, _ctx()))
    assert "error" in out and "purpose" in out["error"]


def test_call_phone_errors_when_api_key_missing(monkeypatch):
    _no_creds(monkeypatch)
    out = asyncio.run(
        vapi._t_call_phone({"to": "+15551234567", "purpose": "say hi"}, _ctx())
    )
    assert "error" in out
    assert "VAPI_API_KEY" in out["error"]


def test_call_phone_errors_when_from_number_missing(monkeypatch):
    # API key present, but no from-number id → a distinct, actionable error.
    creds = {"VAPI_API_KEY": "sk-test"}
    monkeypatch.setattr(vapi, "get_credential", lambda name, *a, **k: creds.get(name))
    out = asyncio.run(
        vapi._t_call_phone({"to": "+15551234567", "purpose": "say hi"}, _ctx())
    )
    assert "error" in out
    assert "VAPI_PHONE_NUMBER_ID" in out["error"]


# ─── default on-call model is VAPI-supported, not the cheap utility model ───


def test_default_call_model_is_gpt_4o():
    assert SundayConfig().vapi.model_name == "gpt-4o"


# ─── /v1/vapi/status endpoint shape ────────────────────────────────────────


class _FakeRequest:
    pass


class _StubDaemon:
    """Just enough of Daemon to drive the unbound status handler."""

    def __init__(self):
        self.config = SundayConfig()


def _status_body(monkeypatch, creds: dict):
    from sunday import daemon as daemon_mod

    # The handler imports get_credential locally from sunday.credentials.
    from sunday import credentials as cred_mod

    monkeypatch.setattr(cred_mod, "get_credential", lambda name, *a, **k: creds.get(name))

    resp = asyncio.run(
        daemon_mod.Daemon._http_vapi_status(_StubDaemon(), _FakeRequest())
    )
    return json.loads(resp.body.decode("utf-8"))


def test_vapi_status_not_configured(monkeypatch):
    body = _status_body(monkeypatch, {})
    assert body["configured"] is False
    assert body["has_api_key"] is False
    assert body["has_from_number"] is False
    assert body["from_number_id"] is None
    assert body["model"] == "gpt-4o"


def test_vapi_status_configured(monkeypatch):
    body = _status_body(
        monkeypatch, {"VAPI_API_KEY": "sk-test", "VAPI_PHONE_NUMBER_ID": "pn_123"}
    )
    assert body["configured"] is True
    assert body["has_api_key"] is True
    assert body["has_from_number"] is True
    assert body["from_number_id"] == "pn_123"


def test_vapi_status_partial_is_not_configured(monkeypatch):
    body = _status_body(monkeypatch, {"VAPI_API_KEY": "sk-test"})
    assert body["configured"] is False
    assert body["has_api_key"] is True
    assert body["has_from_number"] is False
