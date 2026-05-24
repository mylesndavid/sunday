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
def say(text: str = typer.Argument(..., help="What to say to Sunday.")) -> None:
    """Send a message to Sunday and print her reply."""
    try:
        result = _run(call(socket_path(), "say", {"text": text, "modality": "cli"}))
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
