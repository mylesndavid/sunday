"""Tailscale CLI integration.

Sunday talks to the local `tailscale` binary to learn its own identity (the
MagicDNS name it answers to) and to wire up the one public ingress the
dedicated-host topology needs: a Funnel on just the Sendblue webhook path.

Design notes:
- We never bind the daemon to 0.0.0.0. The daemon stays on 127.0.0.1; Tailscale
  Serve/Funnel proxy to it. The box's only public surface is the webhook path.
- `status()` is the source of truth for "what's my path" — it parses
  `tailscale status --json`, which is stable across CLI versions. The Funnel
  *configuration* commands vary more between versions, so `configure_funnel()`
  is best-effort and always also returns the exact manual commands. Setup stays
  smooth whether the auto path works or the user pastes two lines.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache

# Where the macOS app and common installs drop the CLI, in preference order.
_CANDIDATE_PATHS = (
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/usr/bin/tailscale",
)

# Capability key Tailscale reports when this node may operate a Funnel.
_FUNNEL_CAP = "funnel"


@lru_cache(maxsize=1)
def cli_path() -> str | None:
    """Absolute path to the `tailscale` binary, or None if not installed."""
    found = shutil.which("tailscale")
    if found:
        return found
    for candidate in _CANDIDATE_PATHS:
        if shutil.which(candidate) or _is_exec(candidate):
            return candidate
    return None


def _is_exec(path: str) -> bool:
    import os
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _run(args: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run the tailscale CLI. Returns (returncode, stdout, stderr).
    A missing binary or timeout surfaces as a non-zero rc with the reason in
    stderr rather than an exception — callers degrade gracefully."""
    cli = cli_path()
    if not cli:
        return (127, "", "tailscale CLI not found")
    try:
        proc = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired:
        return (124, "", f"tailscale {' '.join(args)} timed out")
    except Exception as exc:  # noqa: BLE001
        return (1, "", f"{type(exc).__name__}: {exc}")


def parse_status(data: dict) -> dict:
    """Pure parser over `tailscale status --json` output.

    Returns the fields Sunday cares about: whether the tailnet is up, the node's
    MagicDNS name (trailing dot stripped), the tailnet name, and whether this
    node is allowed to run a Funnel. Unknown/absent → None rather than a guess.
    """
    state = (data.get("BackendState") or "").strip()
    running = state == "Running"

    self_node = data.get("Self") or {}
    dns_name = (self_node.get("DNSName") or "").rstrip(".") or None

    tailnet = None
    current = data.get("CurrentTailnet") or {}
    if isinstance(current, dict):
        tailnet = current.get("Name") or current.get("MagicDNSSuffix") or None

    magic_dns = bool(current.get("MagicDNSEnabled")) if isinstance(current, dict) else False

    funnel_capable: bool | None = None
    cap_map = self_node.get("CapMap")
    if isinstance(cap_map, dict):
        funnel_capable = any(_FUNNEL_CAP in str(k).lower() for k in cap_map)

    return {
        "running": running,
        "state": state or None,
        "dns_name": dns_name if magic_dns or dns_name else dns_name,
        "tailnet": tailnet,
        "magic_dns": magic_dns,
        "funnel_capable": funnel_capable,
    }


def status() -> dict:
    """Live reachability snapshot. Never raises — returns a dict with
    `installed` and, when up, `dns_name`/`tailnet`/`funnel_capable`."""
    if not cli_path():
        return {
            "installed": False,
            "running": False,
            "dns_name": None,
            "tailnet": None,
            "magic_dns": False,
            "funnel_capable": None,
            "error": "tailscale CLI not found",
        }
    rc, out, err = _run(["status", "--json"])
    if rc != 0 or not out.strip():
        return {
            "installed": True,
            "running": False,
            "dns_name": None,
            "tailnet": None,
            "magic_dns": False,
            "funnel_capable": None,
            "error": (err or out or "tailscale status failed").strip(),
        }
    try:
        data = json.loads(out)
    except (ValueError, TypeError) as exc:
        return {
            "installed": True,
            "running": False,
            "dns_name": None,
            "tailnet": None,
            "magic_dns": False,
            "funnel_capable": None,
            "error": f"could not parse tailscale status: {exc}",
        }
    parsed = parse_status(data)
    parsed["installed"] = True
    return parsed


def manual_commands(port: int, path: str) -> list[str]:
    """The exact commands a user can paste to expose just `path` publicly.
    Always shown alongside the auto attempt so setup never dead-ends.

    The proxy target MUST include `path`. `--set-path <path> <port>` strips the
    mount prefix before proxying, so the daemon would receive `/` instead of
    the secret webhook path — unauthenticated (401) and unrouteable. Giving the
    target the full URL (`http://127.0.0.1:<port><path>`) preserves the path.
    This is why the Sendblue webhook silently never fired and every inbound
    text fell back to the 30s poller."""
    target = f"http://127.0.0.1:{port}{path}"
    return [
        f"tailscale serve --bg --set-path {path} {target}",
        f"tailscale funnel --bg --set-path {path} {target}",
    ]


def configure_funnel(port: int, path: str) -> dict:
    """Best-effort: proxy the tailnet to the daemon and Funnel only `path`.

    Runs `serve` then `funnel` scoped to the single webhook sub-path so the box
    gains exactly one public ingress. Returns per-step results plus the manual
    commands; `ok` is True only if every step succeeded.
    """
    if not cli_path():
        return {
            "ok": False,
            "error": "tailscale CLI not found",
            "steps": [],
            "manual": manual_commands(port, path),
        }

    # Target includes the path so Tailscale doesn't strip the mount prefix —
    # see manual_commands() for the full why. Stripping was the root cause of
    # the webhook 401'ing every Sendblue delivery.
    target = f"http://127.0.0.1:{port}{path}"
    plan = [
        ["serve", "--bg", "--set-path", path, target],
        ["funnel", "--bg", "--set-path", path, target],
    ]
    steps = []
    ok = True
    for args in plan:
        rc, out, err = _run(args, timeout=20.0)
        steps.append({
            "cmd": "tailscale " + " ".join(args),
            "rc": rc,
            "stdout": out.strip(),
            "stderr": err.strip(),
        })
        if rc != 0:
            ok = False
            break

    return {
        "ok": ok,
        "steps": steps,
        "manual": manual_commands(port, path),
    }
