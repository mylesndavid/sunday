"""The device WebSocket must be authenticated.

`/v1/devices/ws` is the endpoint satellites dial home to. A satellite exposes
shell, screen capture, and iMessage, so an unauthenticated peer that can reach
this endpoint could register a device, hijack a device_id, or feed the brain
forged tool results — especially on a daemon exposed over a public URL.

These tests pin the fix: the endpoint is NOT in the auth-exempt list and the
bearer-token middleware actually rejects requests that lack a valid token.
"""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point Sunday at a throwaway home so token minting can't touch the real
    ~/.sunday, and reset the module-level token cache around each test."""
    monkeypatch.setenv("SUNDAY_HOME", str(tmp_path))
    import sunday.daemon as daemon

    daemon._AUTH_TOKEN_CACHE = None
    yield tmp_path
    daemon._AUTH_TOKEN_CACHE = None


def test_devices_ws_not_auth_exempt():
    # The browser UI socket stays exempt (it can't set an Authorization header
    # on the handshake, so it authenticates via the ?token= query param). The
    # device socket is a real client that DOES send the header, so it must ride
    # the normal middleware — i.e. it must NOT be exempt.
    from sunday.daemon import _AUTH_EXEMPT_PREFIXES

    assert "/v1/devices/ws" not in _AUTH_EXEMPT_PREFIXES
    assert "/v1/ws" in _AUTH_EXEMPT_PREFIXES


async def _client(handler) -> TestClient:
    from sunday.daemon import _auth_middleware

    app = web.Application(middlewares=[_auth_middleware])
    app.router.add_get("/v1/devices/ws", handler)
    app.router.add_get("/v1/health", handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_devices_ws_rejected_without_token(home):
    async def handler(request):
        return web.json_response({"ok": True})

    client = await _client(handler)
    try:
        resp = await client.get("/v1/devices/ws")
        assert resp.status == 401
    finally:
        await client.close()


async def test_devices_ws_rejected_with_wrong_token(home):
    async def handler(request):
        return web.json_response({"ok": True})

    client = await _client(handler)
    try:
        resp = await client.get(
            "/v1/devices/ws", headers={"Authorization": "Bearer not-the-token"}
        )
        assert resp.status == 401
    finally:
        await client.close()


async def test_devices_ws_allowed_with_valid_token(home):
    from sunday.daemon import get_or_create_auth_token

    token = get_or_create_auth_token()

    async def handler(request):
        return web.json_response({"ok": True})

    client = await _client(handler)
    try:
        resp = await client.get(
            "/v1/devices/ws", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status == 200
    finally:
        await client.close()


async def test_health_stays_open(home):
    # Sanity check that the exemption logic still lets the unauthenticated
    # health probe through — i.e. we didn't accidentally lock everything down.
    async def handler(request):
        return web.json_response({"ok": True})

    client = await _client(handler)
    try:
        resp = await client.get("/v1/health")
        assert resp.status == 200
    finally:
        await client.close()
