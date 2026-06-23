"""The cua-driver built-in connector wires into the MCP machinery the same way
Playwright does: enabling it writes a stdio server (`cua-driver mcp`) into
mcp.json, and its readiness reflects whether the `cua-driver` binary is on PATH
(not the node gate, which only applies to npx-spawned servers)."""

import pytest

from sunday import mcp


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    # point sunday_home (and thus config_path) at a temp dir so we never touch
    # the real ~/.sunday/mcp.json
    monkeypatch.setattr(mcp, "sunday_home", lambda: tmp_path)
    yield


def test_cua_driver_is_a_builtin_connector():
    b = mcp.BUILTIN_SERVERS["cua-driver"]
    assert b["config"] == {"command": "cua-driver", "args": ["mcp"]}
    assert b["needs"] == "cua-driver"
    assert "token_env" not in b  # no pairing token, unlike Playwright


def test_need_met_checks_binary_on_path(monkeypatch):
    monkeypatch.setattr(mcp, "_binary_available", lambda name: name == "cua-driver")
    assert mcp._need_met("cua-driver") is True
    assert mcp._need_met("nonexistent-bin") is False
    assert mcp._need_met(None) is True


def test_need_met_node_uses_node_available(monkeypatch):
    monkeypatch.setattr(mcp, "node_available", lambda: False)
    # node need must NOT be satisfied just by a same-named binary check
    assert mcp._need_met("node") is False


def test_status_readiness_follows_binary_presence(monkeypatch):
    monkeypatch.setattr(mcp, "_binary_available", lambda name: False)
    row = next(c for c in mcp.builtin_status() if c["id"] == "cua-driver")
    assert row["ready"] is False
    assert row["enabled"] is False

    monkeypatch.setattr(mcp, "_binary_available", lambda name: True)
    row = next(c for c in mcp.builtin_status() if c["id"] == "cua-driver")
    assert row["ready"] is True


def test_enable_writes_stdio_server_then_disable_removes_it():
    cfg = mcp.set_builtin("cua-driver", True)
    assert cfg["mcpServers"]["cua-driver"] == {"command": "cua-driver", "args": ["mcp"]}
    assert mcp.load_config()["mcpServers"]["cua-driver"]["args"] == ["mcp"]

    cfg = mcp.set_builtin("cua-driver", False)
    assert "cua-driver" not in cfg["mcpServers"]
