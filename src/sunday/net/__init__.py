"""Networking integration for the dedicated-host topology.

Tailscale is how a private Sunday brain (a Mac mini at home) is reached:
- Serve  → tailnet-only HTTPS proxy for the desktop UI + device satellites
- Funnel → exactly one public path, the Sendblue webhook, so texting is
           webhook-fast instead of falling back to the poller.
"""
