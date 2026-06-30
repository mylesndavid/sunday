"""Sunday's relay client — the daemon's outbound socket to the hosted pipe.

The relay (spec §2) is a thin, stateless, hosted broker that gives every Sunday
daemon a public URL for inbound webhooks WITHOUT the user touching DNS, Funnel,
or port-forwarding. The daemon dials OUT to it; the relay forwards inbound HTTP
down that socket. This package is the DAEMON side of that: a channel-style
module that registers one background task (the connect loop) and exposes
`public_url(slug)` for the UI.

See `sunday.relay.client` for the wire protocol + loopback delivery.
"""

from __future__ import annotations

from sunday.relay.client import public_url, register

__all__ = ["register", "public_url"]
