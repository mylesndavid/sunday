"""Standard paths for Sunday's runtime data.

All user data lives under ~/.sunday/ — chat log, credentials, socket, logs.
Override via SUNDAY_HOME env var (useful for tests and multi-tenant setups).
"""

from __future__ import annotations

import os
from pathlib import Path


def sunday_home() -> Path:
    return Path(os.environ.get("SUNDAY_HOME", "~/.sunday")).expanduser()


def config_path() -> Path:
    return sunday_home() / "config.yaml"


def credentials_path() -> Path:
    return sunday_home() / "credentials.env"


def socket_path() -> Path:
    return sunday_home() / "sunday.sock"


def auth_token_path() -> Path:
    """The shared bearer token for the HTTP/WS API. Minted by the daemon on
    first start; read by the desktop app and by a local satellite."""
    return sunday_home() / "auth.token"


def db_path() -> Path:
    return sunday_home() / "sunday.db"


def log_path() -> Path:
    return sunday_home() / "logs" / "sunday.log"


def custom_prompt_path() -> Path:
    """User-overridable identity prompt. When present, replaces the built-in
    SUNDAY_SYSTEM_PROMPT verbatim — full control."""
    return sunday_home() / "identity.md"


def ensure_home() -> Path:
    home = sunday_home()
    for child in (home, home / "logs"):
        child.mkdir(parents=True, exist_ok=True)
    return home
