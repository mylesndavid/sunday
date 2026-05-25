"""`sunday init` — interactive setup wizard.

Walks a fresh install through credentials + channel registration + a real
verification pass. Same idea as `hermes init` but Python+Rich instead of
Ink. Idempotent: re-running reads the current credentials and only asks
about what's missing or what you want to change.

Pattern per step:
  ask()      → prompt the user (skip if already set unless --reconfigure)
  store()    → write to ~/.sunday/credentials.env atomically
  verify()   → make one cheap API call to confirm it works
  surface() → print outcome in green / red / yellow

Steps are independent — declining one doesn't fail the whole wizard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from sunday import __version__
from sunday.credentials import get_credential, set_credential

console = Console()


# ─── small helpers ────────────────────────────────────────────────────────


def banner() -> None:
    console.print()
    console.print(Panel.fit(
        "[bold]Sunday[/bold]  [dim]— a personal AI you self-host[/dim]\n"
        "Setup walks you through credentials + channels + a real verification pass.\n"
        "Re-run any time to reconfigure.",
        title=f"sunday init  ·  v{__version__}",
        title_align="left",
        border_style="yellow",
    ))
    console.print()


def heading(text: str, n: int, total: int) -> None:
    console.print(f"\n[dim]{n}/{total}[/dim]  [bold yellow]{text}[/bold yellow]")


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]…[/yellow] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def info(msg: str) -> None:
    console.print(f"  [dim]·[/dim] {msg}")


def existing(key: str) -> str | None:
    v = get_credential(key)
    return v if v else None


def ask_secret(label: str, key: str, get_at: str | None = None) -> str | None:
    """Prompt for a credential. Returns the value (existing if unchanged).
    Empty input keeps the existing value."""
    current = existing(key)
    if current:
        suffix = f"[dim]({key} already set, last 4: …{current[-4:]})[/dim]"
        if not Confirm.ask(f"  Replace {label}? {suffix}", default=False):
            return current

    prompt = f"  Enter {label}"
    if get_at:
        prompt += f"  [dim](get one at {get_at})[/dim]"
    value = Prompt.ask(prompt, password=True, default="", show_default=False).strip()
    if not value:
        info(f"skipped — {key} left unset")
        return current
    set_credential(key, value)
    ok(f"{key} saved")
    return value


def ask_open(label: str, key: str) -> str | None:
    """Like ask_secret but echoes the value (for non-sensitive things like
    a phone number ID or account ID)."""
    current = existing(key)
    if current:
        if not Confirm.ask(f"  Replace {label}? [dim](current: {current})[/dim]", default=False):
            return current
    value = Prompt.ask(f"  Enter {label}", default=current or "").strip()
    if not value:
        return current
    set_credential(key, value)
    ok(f"{key} saved")
    return value


# ─── steps ────────────────────────────────────────────────────────────────


@dataclass
class StepResult:
    name: str
    ok: bool
    note: str = ""


async def step_brain() -> StepResult:
    """Configure the LLM. OpenRouter is the recommended primary; OpenAI +
    Anthropic are optional fallbacks for the router."""
    info("Sunday's brain runs through any OpenAI-compatible API. OpenRouter is the recommended primary — one key, all models.")
    or_key = ask_secret("OPENROUTER_API_KEY", "OPENROUTER_API_KEY", get_at="https://openrouter.ai/keys")
    if not or_key:
        warn("no OpenRouter key — Sunday's brain won't work until you set one")
        return StepResult(name="Brain", ok=False, note="OPENROUTER_API_KEY missing")

    if Confirm.ask("  Add OpenAI as a fallback provider?", default=False):
        ask_secret("OPENAI_API_KEY", "OPENAI_API_KEY", get_at="https://platform.openai.com/api-keys")
    if Confirm.ask("  Add Anthropic as a fallback provider?", default=False):
        ask_secret("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY", get_at="https://console.anthropic.com/")

    info("verifying with a 1-token completion…")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {or_key}",
                    "HTTP-Referer": "https://sunday.local",
                    "X-Title": "Sunday",
                },
                json={
                    "model": "deepseek/deepseek-chat",
                    "messages": [{"role": "user", "content": "say hi in two words"}],
                    "max_tokens": 10,
                },
            )
        if res.status_code == 200:
            reply = res.json()["choices"][0]["message"]["content"].strip()
            ok(f"OpenRouter live — model said \"{reply}\"")
            return StepResult(name="Brain", ok=True, note=reply)
        fail(f"OpenRouter returned {res.status_code}: {res.text[:200]}")
        return StepResult(name="Brain", ok=False, note=f"http {res.status_code}")
    except Exception as exc:  # noqa: BLE001
        fail(f"network: {exc}")
        return StepResult(name="Brain", ok=False, note=str(exc)[:200])


async def step_memory() -> StepResult:
    """Memory needs OPENAI_API_KEY for embeddings."""
    info("Memory uses OpenAI's text-embedding-3-small. Stored locally in ~/.sunday/memories.db.")
    key = existing("OPENAI_API_KEY") or ask_secret(
        "OPENAI_API_KEY (for embeddings only)",
        "OPENAI_API_KEY",
        get_at="https://platform.openai.com/api-keys",
    )
    if not key:
        warn("no OPENAI_API_KEY — memory will be disabled until set")
        return StepResult(name="Memory", ok=False, note="OPENAI_API_KEY missing")

    info("verifying with a 1-vector embed…")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            res = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "text-embedding-3-small", "input": "hello"},
            )
        if res.status_code == 200:
            dims = len(res.json()["data"][0]["embedding"])
            ok(f"OpenAI embeddings live — {dims}-dim vectors")
            return StepResult(name="Memory", ok=True)
        fail(f"OpenAI returned {res.status_code}: {res.text[:200]}")
        return StepResult(name="Memory", ok=False, note=f"http {res.status_code}")
    except Exception as exc:  # noqa: BLE001
        fail(f"network: {exc}")
        return StepResult(name="Memory", ok=False, note=str(exc)[:200])


async def step_sendblue(public_url: str) -> StepResult:
    """Sendblue inbound webhook + outbound iMessages."""
    info("Sendblue gives Sunday her own iMessage-capable phone number. Skip if you don't want texting.")
    if not Confirm.ask("  Configure Sendblue?", default=True):
        return StepResult(name="Sendblue", ok=False, note="skipped by user")

    api_key = ask_secret("SENDBLUE_API_KEY_ID", "SENDBLUE_API_KEY_ID", get_at="https://app.sendblue.co")
    api_secret = ask_secret("SENDBLUE_API_SECRET_KEY", "SENDBLUE_API_SECRET_KEY")
    if not api_key or not api_secret:
        return StepResult(name="Sendblue", ok=False, note="keys missing")

    info("verifying credentials…")
    headers = {"sb-api-key-id": api_key, "sb-api-secret-key": api_secret, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get("https://api.sendblue.co/accounts", headers=headers)
        if res.status_code != 200:
            fail(f"Sendblue returned {res.status_code}")
            return StepResult(name="Sendblue", ok=False, note=f"http {res.status_code}")
        plan = res.json().get("data", {}).get("plan", "?")
        ok(f"Sendblue authenticated — plan: {plan}")
    except Exception as exc:  # noqa: BLE001
        fail(f"network: {exc}")
        return StepResult(name="Sendblue", ok=False, note=str(exc)[:200])

    webhook_url = f"{public_url.rstrip('/')}/webhooks/sendblue"
    if Confirm.ask(f"  Register webhook at {webhook_url}?", default=True):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Sendblue's POST /api/account/webhooks appends rather than
                # replacing. Read current list, dedupe, only add if missing.
                current = (await client.get("https://api.sendblue.co/accounts/webhooks", headers=headers)).json()
                receive = (current.get("data") or {}).get("receive") or []
                if webhook_url in receive:
                    ok(f"webhook already registered")
                else:
                    add = await client.post(
                        "https://api.sendblue.co/api/account/webhooks",
                        headers=headers,
                        json={"webhooks": [webhook_url], "type": "receive"},
                    )
                    if add.status_code in (200, 201):
                        ok(f"webhook registered → {webhook_url}")
                    else:
                        fail(f"webhook register returned {add.status_code}: {add.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            fail(f"webhook register: {exc}")

    return StepResult(name="Sendblue", ok=True)


async def step_vapi() -> StepResult:
    info("VAPI gives Sunday the ability to place outbound phone calls. Skip if not needed.")
    if not Confirm.ask("  Configure VAPI?", default=False):
        return StepResult(name="VAPI", ok=False, note="skipped by user")

    api_key  = ask_secret("VAPI_API_KEY", "VAPI_API_KEY", get_at="https://dashboard.vapi.ai")
    phone_id = ask_open("VAPI_PHONE_NUMBER_ID", "VAPI_PHONE_NUMBER_ID")
    if not api_key or not phone_id:
        return StepResult(name="VAPI", ok=False, note="keys missing")

    info("verifying credentials…")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                "https://api.vapi.ai/phone-number",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if res.status_code == 200:
            ok("VAPI authenticated")
            return StepResult(name="VAPI", ok=True)
        fail(f"VAPI returned {res.status_code}")
        return StepResult(name="VAPI", ok=False, note=f"http {res.status_code}")
    except Exception as exc:  # noqa: BLE001
        fail(f"network: {exc}")
        return StepResult(name="VAPI", ok=False, note=str(exc)[:200])


async def step_cloudflare() -> StepResult:
    info("Cloudflare Browser Rendering lets Sunday navigate + screenshot + scrape any URL.")
    if not Confirm.ask("  Configure Cloudflare browser tools?", default=False):
        return StepResult(name="Cloudflare", ok=False, note="skipped by user")

    token   = ask_secret("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN", get_at="https://dash.cloudflare.com/profile/api-tokens")
    account = ask_open("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID")
    if not token or not account:
        return StepResult(name="Cloudflare", ok=False, note="keys missing")

    info("verifying with a /verify call…")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            res = await client.get(
                "https://api.cloudflare.com/client/v4/user/tokens/verify",
                headers={"Authorization": f"Bearer {token}"},
            )
        if res.status_code == 200 and res.json().get("success"):
            ok("Cloudflare token live")
            return StepResult(name="Cloudflare", ok=True)
        fail(f"verify failed: {res.text[:200]}")
        return StepResult(name="Cloudflare", ok=False, note=f"http {res.status_code}")
    except Exception as exc:  # noqa: BLE001
        fail(f"network: {exc}")
        return StepResult(name="Cloudflare", ok=False, note=str(exc)[:200])


# ─── orchestration ────────────────────────────────────────────────────────


async def _run_async(public_url: str | None) -> list[StepResult]:
    banner()

    if not public_url:
        info("If this Sunday will be reachable from the internet (so satellites + iMessage can find her),")
        info("enter the public URL she'll serve from. Leave blank for local-only.")
        public_url = Prompt.ask("  Public URL (e.g. https://sunday.example.com)", default="").strip() or "http://localhost:8765"
    info(f"using public URL: [bold]{public_url}[/bold]")

    results: list[StepResult] = []
    steps: list[Callable[[], Awaitable[StepResult]]] = [
        ("Brain (OpenRouter)",      step_brain),
        ("Memory (OpenAI embed)",   step_memory),
        ("iMessage (Sendblue)",     lambda: step_sendblue(public_url)),
        ("Phone calls (VAPI)",      step_vapi),
        ("Browser (Cloudflare)",    step_cloudflare),
    ]
    for i, (label, fn) in enumerate(steps, 1):
        heading(label, i, len(steps))
        results.append(await fn())

    # Summary
    console.print()
    table = Table(title="setup summary", title_style="bold yellow", show_header=False, box=None, padding=(0, 1))
    for r in results:
        symbol = "[green]✓[/green]" if r.ok else "[yellow]·[/yellow]"
        table.add_row(symbol, r.name, "[dim]" + r.note + "[/dim]")
    console.print(table)

    # Next steps
    console.print(Panel(
        "[bold]Next steps[/bold]\n\n"
        f"  1. Start the daemon: [yellow]sunday start[/yellow]\n"
        f"  2. From any other Mac, dial in as a satellite:\n"
        f"     [dim]sunday-satellite --server { public_url.replace('http', 'ws').rstrip('/')}/v1/devices/ws \\\n"
        f"                       --device-id $(hostname -s)[/dim]\n"
        f"  3. Point the Electron app at this daemon:\n"
        f"     [dim]SUNDAY_DAEMON_HTTP={public_url} SUNDAY_DAEMON_WS={public_url.replace('http','ws').rstrip('/')}/v1/ws \\\n"
        f"                npm start[/dim]  (from electron/)\n"
        f"  4. Text the Sendblue number — Sunday should reply.",
        border_style="dim",
    ))
    console.print()
    return results


def run(public_url: str | None = None) -> int:
    """Entry point called by `sunday init`. Returns 0 on success, 1 if any
    configured step failed verification."""
    results = asyncio.run(_run_async(public_url))
    # Only fail the command if a step the user explicitly configured failed.
    # "skipped by user" doesn't count.
    bad = [r for r in results if not r.ok and "skipped" not in r.note]
    return 1 if bad else 0
