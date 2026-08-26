"""Device ids match loosely; thinking effort is clamped before it's sent.

Both guard real failures seen in production:
- The brain asked for 'Myless-Mac mini-2' (space) when the device had
  registered as 'Myless-Mac-mini-2' (hyphen) and the tool hard-failed even
  though exactly one device obviously matched.
- A bad reasoning effort is a hard 400 from every provider, so an unknown
  value must never be forwarded.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from sunday.config import load_config
from sunday.devices.tools import _norm_device_id, _resolve_device
from sunday.runtime.providers.openai_compat import _reasoning_effort, _takes_reasoning_effort


class _Manager:
    def __init__(self, devices):
        self._devices = devices

    def list_devices(self):
        return self._devices


class _Ctx:
    def __init__(self, devices):
        self.extras = {"devices": _Manager(devices)}


MINI = {"device_id": "Myless-Mac-mini-2", "capabilities": ["shell", "screen"]}
LOCAL = {"device_id": "local", "capabilities": ["shell"]}


def test_norm_folds_case_and_punctuation():
    assert _norm_device_id("Myless-Mac mini-2") == "mylessmacmini2"
    assert _norm_device_id("myless_mac_mini_2") == "mylessmacmini2"
    assert _norm_device_id("MylessMacMini2") == "mylessmacmini2"


@pytest.mark.parametrize("asked", [
    "Myless-Mac-mini-2",     # exact
    "Myless-Mac mini-2",     # the real-world miss: space for hyphen
    "myless_mac_mini_2",
    "MYLESS-MAC-MINI-2",
    "MylessMacMini2",
])
def test_near_miss_ids_resolve(asked):
    device_id, err = _resolve_device(_Ctx([LOCAL, MINI]), explicit=asked)
    assert err is None
    assert device_id == "Myless-Mac-mini-2"


def test_unknown_id_still_errors_and_lists_what_is_connected():
    device_id, err = _resolve_device(_Ctx([LOCAL, MINI]), explicit="some-other-mac")
    assert device_id is None
    assert "some-other-mac" in err
    assert "Myless-Mac-mini-2" in err


def test_ambiguous_fold_refuses_rather_than_guessing():
    devices = [{"device_id": "mac-mini"}, {"device_id": "Mac Mini"}]
    device_id, err = _resolve_device(_Ctx(devices), explicit="macmini")
    assert device_id is None
    assert "more than one" in err


def test_exact_match_wins_over_folding():
    """An id that exists verbatim resolves to itself even when a sibling folds
    to the same value — no ambiguity error for a name the caller got right."""
    devices = [{"device_id": "mac-mini"}, {"device_id": "Mac Mini"}]
    device_id, err = _resolve_device(_Ctx(devices), explicit="mac-mini")
    assert (device_id, err) == ("mac-mini", None)


@pytest.mark.parametrize("configured,sent", [
    ("low", "low"), ("medium", "medium"), ("high", "high"),
    ("HIGH", "high"), ("  low  ", "low"),
    ("bogus", "medium"), ("", "medium"), (None, "medium"),
])
def test_reasoning_effort_is_clamped(configured, sent):
    cfg = load_config()
    cfg.model = replace(cfg.model, reasoning_effort=configured)
    assert _reasoning_effort(cfg) == sent


@pytest.mark.parametrize("model,takes", [
    ("gpt-5.5", True), ("gpt-5.6-luna", True), ("o1", True), ("o3", True),
    ("gpt-4o-mini", False), ("gpt-4o", False), ("llama3", False),
])
def test_only_reasoning_models_get_the_top_level_param(model, takes):
    cfg = load_config()
    assert _takes_reasoning_effort(replace(cfg.model, name=model)) is takes
