"""One-click Codex sign-in — the OAuth/PKCE dance the `codex` CLI does, run by
the daemon so the user never opens a terminal.

Flow: the app asks the daemon for an authorize URL (`build_authorize`), opens it
in the browser, the user logs into ChatGPT, OpenAI redirects to
http://localhost:1455/auth/callback with a code, the daemon exchanges it for
tokens (`exchange_code`) and writes them to ~/.codex/auth.json in the same shape
the CLI uses (`write_auth`). From there codex.py reads + refreshes them as usual.

The redirect URI/port is fixed by the codex OAuth client registration — it MUST
be http://localhost:1455/auth/callback. Because the callback is localhost, the
browser and the daemon have to be on the same machine, which is exactly the
"This Mac" case Codex already requires.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from sunday.runtime.providers.codex import AUTH_PATH, CLIENT_ID, TOKEN_URL

AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
REDIRECT_PORT = 1455
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/auth/callback"
SCOPE = "openid profile email offline_access"


def new_verifier() -> str:
    """A PKCE code_verifier (URL-safe, no padding)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()


def new_state() -> str:
    return secrets.token_urlsafe(24)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def build_authorize(verifier: str, state: str) -> str:
    """The URL to open in the browser to start the ChatGPT sign-in."""
    q = urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    })
    return f"{AUTHORIZE_URL}?{q}"


async def exchange_code(code: str, verifier: str) -> dict[str, Any]:
    """Swap the authorization code for tokens."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(TOKEN_URL, json={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "code_verifier": verifier,
        })
        r.raise_for_status()
        return r.json()


def _claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001
        return {}


def _account_id(id_token: str) -> str:
    auth = _claims(id_token).get("https://api.openai.com/auth") or {}
    if isinstance(auth, dict):
        return auth.get("chatgpt_account_id") or auth.get("account_id") or ""
    return ""


def account_email() -> str:
    """Email on the currently-stored Codex login (for "Connected as …")."""
    try:
        auth = json.loads(AUTH_PATH.read_text())
        return _claims(auth["tokens"].get("id_token", "")).get("email", "")
    except Exception:  # noqa: BLE001
        return ""


def write_auth(tokens: dict[str, Any]) -> str:
    """Persist tokens to ~/.codex/auth.json (CLI-compatible). Returns the email."""
    id_token = tokens.get("id_token", "")
    auth = {
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": id_token,
            "access_token": tokens.get("access_token", ""),
            "refresh_token": tokens.get("refresh_token", ""),
            "account_id": _account_id(id_token),
        },
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(json.dumps(auth, indent=2))
    try:
        AUTH_PATH.chmod(0o600)
    except Exception:  # noqa: BLE001
        pass
    return _claims(id_token).get("email", "")
