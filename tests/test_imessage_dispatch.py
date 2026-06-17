"""Native iMessage inbound routes through daemon._say, which gives 'by the way'
steering: a text arriving mid-turn folds into the running turn (steered=True)
and we send nothing; a text that runs its own turn gets its reply sent as
bubbles."""

import asyncio

import pytest

from sunday.channels import imessage_watch as w


class _Config:
    imessage_indicators = False


class _FakeDaemon:
    def __init__(self, result):
        self._result = result
        self.said = []
        self.config = _Config()

    async def _say(self, text, modality, attachments=None):
        self.said.append((text, modality))
        return self._result


@pytest.fixture
def captured_sends(monkeypatch):
    sends = []

    async def _fake_send(to, body, attachments=None):
        sends.append((to, body))
        return {"ok": True}

    monkeypatch.setattr(w.im, "send_imessage", _fake_send)
    # no real sleeping between bubbles in tests
    async def _no_sleep(_):
        return None
    monkeypatch.setattr(w.asyncio, "sleep", _no_sleep)
    return sends


def test_reply_is_sent_as_bubbles(captured_sends):
    daemon = _FakeDaemon({"reply": "on it\n\ngive me a sec"})
    asyncio.run(w._dispatch(daemon, "+15551234567", "do the thing"))
    assert [b for _, b in captured_sends] == ["on it", "give me a sec"]
    assert daemon.said == [("do the thing", "imessage_native")]


def test_steered_sends_nothing(captured_sends):
    daemon = _FakeDaemon({"steered": True, "reply": None})
    asyncio.run(w._dispatch(daemon, "+15551234567", "oh also, by the way"))
    assert captured_sends == []  # the running turn's reply covers it


def test_empty_reply_sends_nothing(captured_sends):
    daemon = _FakeDaemon({"reply": ""})
    asyncio.run(w._dispatch(daemon, "+15551234567", "hm"))
    assert captured_sends == []
