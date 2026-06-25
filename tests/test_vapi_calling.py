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


# ─── list_calls / get_call: trimmed shape + missing-key error ──────────────


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient that returns a canned payload."""

    def __init__(self, payload, status_code=200, *a, **k):
        self._payload = payload
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **k):
        return _FakeResponse(self._payload, self._status)


def _with_key(monkeypatch):
    monkeypatch.setattr(vapi, "get_credential", lambda name, *a, **k: "sk-test")


def _stub_httpx(monkeypatch, payload, status_code=200):
    monkeypatch.setattr(
        vapi.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(payload, status_code),
    )


def test_list_calls_missing_key(monkeypatch):
    _no_creds(monkeypatch)
    out = asyncio.run(vapi.list_calls())
    assert "error" in out
    assert "VAPI_API_KEY" in out["error"]


def test_list_calls_trims_and_sorts(monkeypatch):
    _with_key(monkeypatch)
    payload = [
        {
            "id": "call_old",
            "createdAt": "2026-06-01T10:00:00.000Z",
            "customer": {"number": "+15550000001"},
            "status": "ended",
            "endedReason": "customer-ended-call",
            "durationSeconds": 42,
            "assistant": {"name": "Sunday"},
            "recordingUrl": "https://rec/old.wav",
        },
        {
            "id": "call_new",
            "createdAt": "2026-06-20T10:00:00.000Z",
            "customer": {"number": "+15550000002"},
            "status": "ended",
            "endedReason": "no-answer",
            # no duration, no recording
        },
    ]
    _stub_httpx(monkeypatch, payload)
    out = asyncio.run(vapi.list_calls())
    assert "error" not in out
    rows = out["calls"]
    assert [r["id"] for r in rows] == ["call_new", "call_old"]  # newest first
    new, old = rows
    assert set(new.keys()) == {
        "id", "createdAt", "to", "status", "endedReason",
        "durationSeconds", "assistantName", "hasRecording",
    }
    assert new["to"] == "+15550000002"
    assert new["hasRecording"] is False
    assert new["durationSeconds"] is None
    assert old["hasRecording"] is True
    assert old["durationSeconds"] == 42.0
    assert old["assistantName"] == "Sunday"


def test_list_calls_handles_results_envelope(monkeypatch):
    _with_key(monkeypatch)
    _stub_httpx(monkeypatch, {"results": [{"id": "c1", "customer": {"number": "+1"}}]})
    out = asyncio.run(vapi.list_calls())
    assert [r["id"] for r in out["calls"]] == ["c1"]


def test_list_calls_propagates_vapi_error(monkeypatch):
    _with_key(monkeypatch)
    _stub_httpx(monkeypatch, {"message": "bad key"}, status_code=401)
    out = asyncio.run(vapi.list_calls())
    assert "error" in out
    assert "401" in out["error"]


def test_get_call_missing_key(monkeypatch):
    _no_creds(monkeypatch)
    out = asyncio.run(vapi.get_call("call_x"))
    assert "error" in out
    assert "VAPI_API_KEY" in out["error"]


def test_get_call_trims_detail(monkeypatch):
    _with_key(monkeypatch)
    payload = {
        "id": "call_x",
        "createdAt": "2026-06-20T10:00:00.000Z",
        "customer": {"number": "+15551112222"},
        "status": "ended",
        "endedReason": "assistant-ended-call",
        "durationSeconds": 73,
        "summary": "Booked the table.",
        "transcript": "AI: Hi\nUser: Hello",
        "artifact": {"recordingUrl": "https://rec/x.wav"},
        "messages": [{"role": "bot", "message": "Hi"}],
    }
    _stub_httpx(monkeypatch, payload)
    out = asyncio.run(vapi.get_call("call_x"))
    assert "error" not in out
    assert out["id"] == "call_x"
    assert out["to"] == "+15551112222"
    assert out["summary"] == "Booked the table."
    assert out["transcript"] == "AI: Hi\nUser: Hello"
    assert out["recordingUrl"] == "https://rec/x.wav"   # from artifact.recordingUrl
    assert out["durationSeconds"] == 73.0
    assert out["messages"] == [{"role": "bot", "message": "Hi"}]


def test_get_call_propagates_vapi_error(monkeypatch):
    _with_key(monkeypatch)
    _stub_httpx(monkeypatch, {"message": "not found"}, status_code=404)
    out = asyncio.run(vapi.get_call("nope"))
    assert "error" in out
    assert "404" in out["error"]


# ─── daemon handlers wrap the helpers and shape the HTTP response ──────────


class _CallsRequest:
    def __init__(self, call_id=None):
        self.match_info = {"id": call_id} if call_id is not None else {}


def test_http_vapi_calls_ok(monkeypatch):
    from sunday import daemon as daemon_mod
    from sunday.channels import vapi as vapi_mod

    async def _fake_list(limit=50):
        return {"calls": [{"id": "c1"}]}

    monkeypatch.setattr(vapi_mod, "list_calls", _fake_list)
    resp = asyncio.run(
        daemon_mod.Daemon._http_vapi_calls(_StubDaemon(), _CallsRequest())
    )
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 200
    assert body["calls"][0]["id"] == "c1"


def test_http_vapi_calls_missing_key_is_400(monkeypatch):
    from sunday import daemon as daemon_mod
    from sunday.channels import vapi as vapi_mod

    async def _fake_list(limit=50):
        return {"error": "VAPI_API_KEY is missing."}

    monkeypatch.setattr(vapi_mod, "list_calls", _fake_list)
    resp = asyncio.run(
        daemon_mod.Daemon._http_vapi_calls(_StubDaemon(), _CallsRequest())
    )
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 400
    assert "VAPI_API_KEY" in body["error"]


def test_http_vapi_call_get_ok(monkeypatch):
    from sunday import daemon as daemon_mod
    from sunday.channels import vapi as vapi_mod

    async def _fake_get(call_id):
        assert call_id == "c9"
        return {"id": "c9", "transcript": "hi", "recordingUrl": "https://r/x.wav"}

    monkeypatch.setattr(vapi_mod, "get_call", _fake_get)
    resp = asyncio.run(
        daemon_mod.Daemon._http_vapi_call_get(_StubDaemon(), _CallsRequest("c9"))
    )
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 200
    assert body["id"] == "c9"
    assert body["recordingUrl"] == "https://r/x.wav"


def test_http_vapi_call_get_missing_id_is_400(monkeypatch):
    from sunday import daemon as daemon_mod
    resp = asyncio.run(
        daemon_mod.Daemon._http_vapi_call_get(_StubDaemon(), _CallsRequest(""))
    )
    body = json.loads(resp.body.decode("utf-8"))
    assert resp.status == 400
    assert "id" in body["error"]
