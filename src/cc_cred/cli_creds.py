import json
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from cc_cred import rotation
from cc_cred.store import CredStore

console = Console()


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Credential manager for Claude Code OAuth tokens.

    Run without arguments to open the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        from cc_cred.tui import CredManagerApp
        CredManagerApp().run()


@main.command()
@click.argument("token")
@click.option("--label", "-l", default="", help="Human-readable label for this credential.")
def add(token: str, label: str) -> None:
    """Register a new OAuth token."""
    store = CredStore()
    try:
        cred = store.add(token, label)
        console.print(f"[green]Added credential[/] [dim]{cred.id[:8]}[/]" + (f" ({label})" if label else ""))
        if store.get_active() is None:
            store.set_active(cred.id)
            console.print("[dim]Set as active (first credential).[/]")
    except ValueError as exc:
        console.print(f"[red]Error:[/] {exc}", err=True)
        sys.exit(1)


@main.command(name="list")
def list_creds() -> None:
    """List all registered credentials and their status."""
    store = CredStore()
    creds = store.list()

    if not creds:
        console.print("[dim]No credentials registered. Run 'cc-creds add <token>'.[/]")
        return

    active = store.get_active()
    active_id = active.id if active else None

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=2)
    table.add_column("ID", style="dim")
    table.add_column("Label")
    table.add_column("Status")
    table.add_column("Resets At")

    colour_map = {
        "available": "green",
        "limited": "yellow",
        "admin_disabled": "red",
        "unknown": "dim",
    }

    for cred in creds:
        status = store.get_status(cred.id)
        colour = colour_map.get(status.status, "white")
        marker = "[bold green]▶[/]" if cred.id == active_id else " "
        reset_str = status.reset_at or "—"
        table.add_row(
            marker,
            cred.id[:8],
            cred.label or "—",
            f"[{colour}]{status.status}[/]",
            reset_str,
        )

    console.print(table)


@main.command()
def status() -> None:
    """Show the currently active credential."""
    store = CredStore()
    active = store.get_active()

    if active is None:
        console.print("[yellow]No active credential set.[/]")
        sys.exit(1)

    cred_status = store.get_status(active.id)
    colour_map = {"available": "green", "limited": "yellow", "admin_disabled": "red", "unknown": "dim"}
    colour = colour_map.get(cred_status.status, "white")

    console.print(f"[bold]Active:[/] {active.id[:8]}" + (f" ({active.label})" if active.label else ""))
    console.print(f"[bold]Status:[/] [{colour}]{cred_status.status}[/]")
    if cred_status.reset_at:
        console.print(f"[bold]Resets:[/] {cred_status.reset_at}")


@main.command()
def rotate() -> None:
    """Manually rotate to the next available credential."""
    store = CredStore()
    next_cred = rotation.rotate(store)
    if next_cred is None:
        console.print("[red]No available credentials to rotate to.[/]", err=True)
        sys.exit(1)
    label = next_cred.label or next_cred.id[:8]
    console.print(f"[green]Rotated to:[/] {label}")


@main.command("set-active")
@click.argument("id_or_label")
def set_active(id_or_label: str) -> None:
    """Set the active credential by ID prefix or label."""
    store = CredStore()
    creds = store.list()

    match = None
    for cred in creds:
        if cred.id.startswith(id_or_label) or cred.label == id_or_label:
            match = cred
            break

    if match is None:
        console.print(f"[red]No credential matching[/] '{id_or_label}'", err=True)
        sys.exit(1)

    store.set_active(match.id)
    console.print(f"[green]Active set to:[/] {match.id[:8]}" + (f" ({match.label})" if match.label else ""))


@main.command("install-hook")
def install_hook() -> None:
    """Add a StopFailure hook to ~/.claude/settings.json to track rate limits."""
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        console.print(f"[red]Settings file not found:[/] {settings_path}", err=True)
        sys.exit(1)

    with open(settings_path, "r") as f:
        settings = json.load(f)

    hook_command = "cc-creds hook-event"
    hooks = settings.setdefault("hooks", {})
    stop_failure = hooks.setdefault("StopFailure", [])

    for entry in stop_failure:
        for h in entry.get("hooks", []):
            if h.get("command") == hook_command:
                console.print("[dim]Hook already installed.[/]")
                return

    stop_failure.append({"hooks": [{"type": "command", "command": hook_command}]})

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    console.print(f"[green]Hook installed.[/] StopFailure → {hook_command}")


@main.command("hook-event")
def hook_event() -> None:
    """Process a StopFailure hook event from stdin (called by Claude Code).

    Reads JSON payload from stdin. If error is 'rate_limit', marks the active
    credential as limited and parses the reset time from last_assistant_message.
    """
    from cc_cred.detection import is_rate_limited_text, parse_reset_time

    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    payload = data.get("payload", data)
    error = payload.get("error", "")

    if error != "rate_limit":
        sys.exit(0)

    store = CredStore()
    active = store.get_active()
    if active is None:
        sys.exit(0)

    last_msg = payload.get("last_assistant_message", "")
    reset_at = parse_reset_time(last_msg)
    store.mark_limited(active.id, reset_at=reset_at)

    last_failure = store.STORE_DIR / "last-stop-failure.json"
    record = {
        "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "session_id": payload.get("session_id"),
        "token_id": active.id,
        "reset_at": reset_at.isoformat() if reset_at else None,
        "cwd": payload.get("cwd"),
        "raw_message": last_msg,
    }
    with open(last_failure, "w") as f:
        json.dump(record, f, indent=2)

    sys.exit(0)
