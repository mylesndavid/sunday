"""Sunday CLI.

The CLI is one of many modalities — it talks to the same daemon and writes
to the same chat. Every other surface (Electron, iMessage, voice) will do
the same.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import typer

from sunday import __version__
from sunday import daemon as daemon_module
from sunday.attachments import stash_local_file
from sunday.credentials import credential_present, set_credential
from sunday.ipc import IpcError, call
from sunday.paths import socket_path

app = typer.Typer(no_args_is_help=True, help="Sunday — a personal AI that knows you.")
credential_app = typer.Typer(help="Manage local API keys.")
app.add_typer(credential_app, name="credential")


def _run(coro):
    return asyncio.run(coro)


@app.command()
def version() -> None:
    """Print Sunday's version."""
    typer.echo(__version__)


@app.command()
def init(
    public_url: str = typer.Option(
        "",
        "--public-url",
        help="Public URL Sunday will be reachable at (https://sunday.example.com). "
             "If omitted, the wizard asks interactively.",
    ),
) -> None:
    """Interactive setup wizard — credentials, channels, verification.

    Walks through the central nervous system once: brain, memory, iMessage,
    phone calls, browser tools. Verifies each as you go. Idempotent — re-run
    to reconfigure or add channels later.
    """
    from sunday.setup import run as run_setup
    raise typer.Exit(code=run_setup(public_url or None))


@app.command()
def start() -> None:
    """Run the daemon in the foreground. Ctrl+C to stop."""
    daemon_module.main()


@app.command()
def stop() -> None:
    """Ask a running daemon to shut down."""
    try:
        _run(call(socket_path(), "stop"))
        typer.echo("stopped")
    except IpcError as exc:
        typer.echo(f"could not stop: {exc}", err=True)
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show daemon status — running, model, message count."""
    try:
        result = _run(call(socket_path(), "status"))
    except IpcError as exc:
        typer.echo(f"daemon not running: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"sunday {result['version']}")
    typer.echo(f"model:    {result['model']}")
    typer.echo(f"messages: {result['messages']}")


@app.command()
def say(
    text: str = typer.Argument("", help="What to say to Sunday."),
    attach: list[str] = typer.Option(
        None, "--attach", "-a",
        help="Path to a file/image to send with the message. Repeatable.",
    ),
) -> None:
    """Send a message — and optional attachments — to Sunday and print her reply."""
    attachments: list[dict] = []
    if attach:
        for p in attach:
            try:
                att = stash_local_file(p)
            except FileNotFoundError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1)
            attachments.append(att.to_dict())
    payload = {"text": text, "modality": "cli"}
    if attachments:
        payload["attachments"] = attachments
    if not text and not attachments:
        typer.echo("nothing to send: provide TEXT or at least one --attach", err=True)
        raise typer.Exit(code=1)
    try:
        result = _run(call(socket_path(), "say", payload))
    except IpcError as exc:
        typer.echo(f"could not reach Sunday: {exc}", err=True)
        raise typer.Exit(code=1)
    typer.echo(result["reply"])


@app.command(name="log")
def show_log(limit: int = typer.Option(20, "--limit", "-n", help="How many messages to show.")) -> None:
    """Show the recent chat — every modality, one log."""
    try:
        result = _run(call(socket_path(), "log", {"limit": limit}))
    except IpcError as exc:
        typer.echo(f"daemon not running: {exc}", err=True)
        raise typer.Exit(code=1)
    for msg in result["messages"]:
        ts = datetime.fromtimestamp(msg["created_at"]).strftime("%H:%M:%S")
        speaker = "you" if msg["role"] == "user" else msg["role"]
        typer.echo(f"[{ts}] {speaker} ({msg['modality']}): {msg['content']}")


@app.command()
def tools() -> None:
    """List the tools Sunday currently has wired up."""
    try:
        result = _run(call(socket_path(), "tools"))
    except IpcError as exc:
        typer.echo(f"daemon not running: {exc}", err=True)
        raise typer.Exit(code=1)
    for t in result["tools"]:
        typer.echo(f"  {t['name']:<28} {t['description']}")


@app.command()
def cost(
    hours: int = typer.Option(24, "--hours", "-h", help="Window to summarize (default: last 24h)."),
    recent: bool = typer.Option(False, "--recent", "-r", help="Print the most recent calls instead of an aggregate."),
    limit: int = typer.Option(20, "--limit", "-n", help="Row limit for --recent."),
) -> None:
    """Show LLM + transcription spend.

    Reads the local cost.db directly — no daemon required, so this also works
    while the daemon is stopped. By default shows the last 24h grouped by
    purpose (chat / extract_facts / graph_rebuild / observer_tick / …) and
    model, so it's obvious which subsystem is costing what.
    """
    import time
    from sunday.cost import get_store

    store = get_store()
    if recent:
        rows = store.recent(limit=limit)
        if not rows:
            typer.echo("no calls logged yet.")
            return
        typer.echo(f"{'when':<20} {'purpose':<22} {'model':<32} {'in':>8} {'out':>8} {'aud':>6} {'ms':>6} {'$':>10}")
        for r in rows:
            when = datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            typer.echo(
                f"{when:<20} {r['purpose'][:22]:<22} {r['model'][:32]:<32} "
                f"{r['prompt_tokens']:>8} {r['completion_tokens']:>8} "
                f"{int(r['audio_seconds']):>6} {r['latency_ms']:>6} "
                f"{r['cost_usd']:>10.6f}"
            )
        return

    since = time.time() - hours * 3600
    s = store.summary(since)
    typer.echo(f"\nLast {hours}h — {s['total_calls']} calls — ${s['total_cost_usd']:.4f}\n")
    if s["by_purpose"]:
        typer.echo("by purpose:")
        for purpose, info in s["by_purpose"].items():
            typer.echo(
                f"  {purpose:<24} {info['calls']:>5}× "
                f"in={info['prompt_tokens']:>8}  out={info['completion_tokens']:>8}  "
                f"audio={int(info['audio_seconds']):>5}s  ${info['cost_usd']:.4f}"
            )
    if s["by_model"]:
        typer.echo("\nby model:")
        for model, info in s["by_model"].items():
            typer.echo(f"  {model:<40} {info['calls']:>5}×  ${info['cost_usd']:.4f}")


@credential_app.command("set")
def credential_set(name: str = typer.Argument(...), value: str = typer.Argument(...)) -> None:
    """Save an API key to ~/.sunday/credentials.env (mode 0600)."""
    set_credential(name, value)
    typer.echo(f"saved {name}")


@credential_app.command("check")
def credential_check(name: str = typer.Argument(...)) -> None:
    """Verify a credential is set (without printing it)."""
    if credential_present(name):
        typer.echo(f"{name}: present")
    else:
        typer.echo(f"{name}: missing", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
