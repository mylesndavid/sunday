"""Local credential store at ~/.sunday/credentials.env (mode 0600).

Plain KEY=VALUE lines. Env vars win when present so dev/CI can override.
"""

from __future__ import annotations

import os
from pathlib import Path

from sunday.paths import credentials_path, ensure_home


def load_credentials(path: Path | None = None) -> dict[str, str]:
    resolved = path or credentials_path()
    if not resolved.exists():
        return {}
    out: dict[str, str] = {}
    for line in resolved.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def get_credential(name: str, path: Path | None = None) -> str | None:
    env = os.environ.get(name)
    if env:
        return env
    return load_credentials(path).get(name) or None


def set_credential(name: str, value: str, path: Path | None = None) -> None:
    ensure_home()
    resolved = path or credentials_path()
    key = name.strip()
    if not key:
        raise ValueError("credential name is required")
    new_value = value.strip()
    lines: list[str] = []
    found = False
    if resolved.exists():
        for raw in resolved.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if "=" in stripped and not stripped.startswith("#"):
                existing = stripped.split("=", 1)[0].strip()
                if existing == key:
                    lines.append(f"{key}={new_value}")
                    found = True
                    continue
            lines.append(raw)
    if not found:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={new_value}")
    resolved.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    resolved.chmod(0o600)


def credential_present(name: str, path: Path | None = None) -> bool:
    return bool(get_credential(name, path=path))
