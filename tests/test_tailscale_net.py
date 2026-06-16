"""Tailscale status parsing + webhook URL construction.

The Funnel commands can't be exercised without a real tailnet, but the parser
(what tells Sunday its own path) and the URL builder are pure and worth pinning.
"""

from __future__ import annotations

from sunday.net import tailscale


def test_parse_status_running_funnel_capable():
    data = {
        "BackendState": "Running",
        "Self": {
            "DNSName": "mini.tail1a2b.ts.net.",
            "HostName": "mini",
            "CapMap": {"https://tailscale.com/cap/funnel": ["foo"]},
        },
        "CurrentTailnet": {
            "Name": "tail1a2b.ts.net",
            "MagicDNSSuffix": "tail1a2b.ts.net",
            "MagicDNSEnabled": True,
        },
    }
    out = tailscale.parse_status(data)
    assert out["running"] is True
    assert out["state"] == "Running"
    assert out["dns_name"] == "mini.tail1a2b.ts.net"  # trailing dot stripped
    assert out["tailnet"] == "tail1a2b.ts.net"
    assert out["magic_dns"] is True
    assert out["funnel_capable"] is True


def test_parse_status_stopped():
    out = tailscale.parse_status({"BackendState": "Stopped", "Self": {}})
    assert out["running"] is False
    assert out["dns_name"] is None
    assert out["funnel_capable"] is None


def test_parse_status_running_no_funnel_cap():
    data = {
        "BackendState": "Running",
        "Self": {"DNSName": "lap.tailxyz.ts.net.", "CapMap": {}},
        "CurrentTailnet": {"Name": "tailxyz.ts.net", "MagicDNSEnabled": True},
    }
    out = tailscale.parse_status(data)
    assert out["funnel_capable"] is False
    assert out["dns_name"] == "lap.tailxyz.ts.net"


def test_manual_commands_scope_a_single_path():
    cmds = tailscale.manual_commands(8765, "/webhooks/sendblue/abc123")
    assert any("serve" in c and "/webhooks/sendblue/abc123" in c for c in cmds)
    assert any("funnel" in c and "8765" in c for c in cmds)


def test_public_webhook_url_built_from_dns_name(tmp_path, monkeypatch):
    # Pin the secret so the URL is deterministic and we don't touch the real
    # credentials file.
    monkeypatch.setenv("SUNDAY_HOME", str(tmp_path))
    monkeypatch.setenv("SENDBLUE_WEBHOOK_SECRET", "s3cr3t")
    from sunday.channels import sendblue
    assert sendblue.webhook_path() == "/webhooks/sendblue/s3cr3t"
    assert (
        sendblue.public_webhook_url("mini.tail1a2b.ts.net")
        == "https://mini.tail1a2b.ts.net/webhooks/sendblue/s3cr3t"
    )
    assert sendblue.public_webhook_url(None) is None
