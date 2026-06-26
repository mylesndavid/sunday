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


# ─── call-completion polling: learn the outcome without a webhook ──────────


class _RecordingChat:
    """Captures chat.append calls so tests can assert what got surfaced."""

    def __init__(self):
        self.appends = []

    def append(self, role, content, modality, metadata=None):
        self.appends.append(
            {"role": role, "content": content, "modality": modality, "metadata": metadata}
        )
        return len(self.appends)


class _FakeDaemon:
    def __init__(self):
        self.chat = _RecordingChat()


def _instant_sleep(monkeypatch):
    """Make asyncio.sleep a no-op so the poll loop runs without wall-clock waits."""
    async def _noop(_secs):
        return None
    monkeypatch.setattr(vapi.asyncio, "sleep", _noop)


def test_handle_call_completed_builds_report_shape():
    daemon = _FakeDaemon()
    asyncio.run(vapi.handle_call_completed(daemon, {
        "id": "call_1",
        "to": "+15551112222",
        "endedReason": "customer-ended-call",
        "durationSeconds": 73,
        "summary": "Booked the table.",
        "transcript": "AI: Hi\nUser: Hello",
    }))
    assert len(daemon.chat.appends) == 1
    rec = daemon.chat.appends[0]
    assert rec["role"] == "sunday"
    assert rec["modality"] == "vapi"
    assert "Call to +15551112222 ended (customer-ended-call, 73s)." in rec["content"]
    assert "Summary: Booked the table." in rec["content"]
    assert "Transcript:\nAI: Hi\nUser: Hello" in rec["content"]
    assert rec["metadata"] == {
        "call_id": "call_1",
        "ended_reason": "customer-ended-call",
        "duration": 73,
        "to": "+15551112222",
    }


def test_handle_call_completed_never_raises(monkeypatch):
    class _BoomChat:
        def append(self, *a, **k):
            raise RuntimeError("db down")

    class _BoomDaemon:
        chat = _BoomChat()

    # Must swallow the failure rather than propagate it to the caller/task.
    asyncio.run(vapi.handle_call_completed(_BoomDaemon(), {"id": "x", "to": "+1"}))


def test_poll_reaches_terminal_and_handles_once(monkeypatch):
    _instant_sleep(monkeypatch)
    daemon = _FakeDaemon()

    # in-progress for the first two polls, then "ended".
    seq = iter([
        {"id": "c1", "status": "in-progress"},
        {"id": "c1", "status": "ringing"},
        {"id": "c1", "status": "ended", "to": "+15550001111",
         "endedReason": "assistant-ended-call", "durationSeconds": 30,
         "summary": "Done.", "transcript": "AI: hi"},
    ])

    calls = {"n": 0}

    async def _fake_get(call_id):
        calls["n"] += 1
        return next(seq)

    monkeypatch.setattr(vapi, "get_call", _fake_get)
    asyncio.run(vapi.poll_call_until_done(daemon, "c1", interval=0.0, max_seconds=60))

    # surfaced exactly once, with the terminal call's content.
    assert len(daemon.chat.appends) == 1
    assert "Call to +15550001111 ended (assistant-ended-call, 30s)." in daemon.chat.appends[0]["content"]
    # stopped polling at the terminal read — didn't keep going.
    assert calls["n"] == 3


def test_poll_timeout_surfaces_note(monkeypatch):
    _instant_sleep(monkeypatch)
    daemon = _FakeDaemon()

    # A monotonically advancing clock so max_seconds is exceeded quickly.
    ticks = iter([0.0, 0.0, 5.0, 10.0, 100.0, 200.0, 999.0])

    class _Loop:
        def time(self):
            try:
                return next(ticks)
            except StopIteration:
                return 9999.0

    monkeypatch.setattr(vapi.asyncio, "get_event_loop", lambda: _Loop())

    async def _always_in_progress(call_id):
        return {"id": call_id, "status": "in-progress"}

    monkeypatch.setattr(vapi, "get_call", _always_in_progress)
    asyncio.run(vapi.poll_call_until_done(daemon, "c1", interval=0.0, max_seconds=20))

    assert len(daemon.chat.appends) == 1
    note = daemon.chat.appends[0]
    assert "didn't complete" in note["content"]
    assert note["metadata"]["poll"] == "timeout"


def test_poll_gives_up_after_repeated_api_errors(monkeypatch):
    _instant_sleep(monkeypatch)
    daemon = _FakeDaemon()

    calls = {"n": 0}

    async def _always_error(call_id):
        calls["n"] += 1
        return {"error": "vapi 500: boom"}

    monkeypatch.setattr(vapi, "get_call", _always_error)
    asyncio.run(vapi.poll_call_until_done(daemon, "c1", interval=0.0, max_seconds=60))

    # capped at the consecutive-error limit; nothing surfaced.
    assert daemon.chat.appends == []
    assert calls["n"] == 5


def test_poll_noop_on_empty_call_id(monkeypatch):
    daemon = _FakeDaemon()

    async def _should_not_run(call_id):  # pragma: no cover - must not be reached
        raise AssertionError("get_call should not be called for an empty id")

    monkeypatch.setattr(vapi, "get_call", _should_not_run)
    asyncio.run(vapi.poll_call_until_done(daemon, "", interval=0.0, max_seconds=60))
    assert daemon.chat.appends == []


def test_call_phone_returns_pending_and_spawns_poller(monkeypatch):
    # _create_call succeeds with a non-terminal status → tool returns "pending"
    # guidance and a background poller is started.
    async def _fake_create(to, purpose, config, ctx=None):
        return {"ok": True, "call_id": "c1", "status": "queued", "data": {}}

    monkeypatch.setattr(vapi, "_create_call", _fake_create)

    spawned = {}

    def _fake_spawn(daemon, call_id, status):
        spawned["call_id"] = call_id
        spawned["status"] = status

    monkeypatch.setattr(vapi, "spawn_call_poller", _fake_spawn)

    daemon = _FakeDaemon()
    ctx = ToolContext(
        chat=_NoopChat(), config=SundayConfig(), modality="chat",
        extras={"daemon": daemon},
    )
    out = asyncio.run(
        vapi._t_call_phone({"to": "+15551234567", "purpose": "say hi"}, ctx)
    )
    assert out["ok"] is True
    assert out["call_id"] == "c1"
    assert out["outcome"] == "pending"
    assert "automatically" in out["note"]
    assert spawned == {"call_id": "c1", "status": "queued"}


def test_call_phone_does_not_spawn_on_create_error(monkeypatch):
    async def _fake_create(to, purpose, config, ctx=None):
        return {"error": "VAPI_API_KEY is missing."}

    monkeypatch.setattr(vapi, "_create_call", _fake_create)

    def _boom_spawn(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("must not spawn a poller when the call wasn't placed")

    monkeypatch.setattr(vapi, "spawn_call_poller", _boom_spawn)

    ctx = ToolContext(
        chat=_NoopChat(), config=SundayConfig(), modality="chat",
        extras={"daemon": _FakeDaemon()},
    )
    out = asyncio.run(
        vapi._t_call_phone({"to": "+15551234567", "purpose": "say hi"}, ctx)
    )
    assert "error" in out


def test_spawn_call_poller_skips_terminal_status(monkeypatch):
    # An already-ended call needs no poller (and create_task must not be hit).
    def _boom(*a, **k):  # pragma: no cover - must not be reached
        raise AssertionError("create_task should not run for a terminal call")

    monkeypatch.setattr(vapi.asyncio, "create_task", _boom)
    vapi.spawn_call_poller(_FakeDaemon(), "c1", "ended")  # no exception = pass
