import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from cc_cred._logging import configure_logging
from cc_cred import rotation
from cc_cred.store import CredStore, Credential
from cc_cred.verify import check_token

console = Console()
err_console = Console(stderr=True)

def _load_status_dict(store: CredStore) -> dict:
    """Return the raw status dict from cred-status.json."""
    path = store._status_path()
    if not path.exists():
        return {}
    with open(path, "r") as f:
        import json as _j
        return _j.load(f)


SETUP_INSTRUCTIONS = """\
[bold]To generate a new token:[/]

  1. [cyan]claude auth login[/]         (authenticate with your Claude account)
  2. [cyan]claude setup-token[/]        (create a long-lived OAuth token)
  3. Copy the printed token (starts with [dim]sk-ant-...[/])
  4. [cyan]cc-creds add <token> --label "my account"[/]
"""


def _expiry_display(cred: Credential) -> tuple[str, str]:
    """Return (display_string, colour) for the token expiry date."""
    if not cred.expires_at:
        return "unknown", "dim"
    try:
        expires = datetime.fromisoformat(cred.expires_at)
        now = datetime.now(timezone.utc)
        days_left = (expires - now).days
        date_str = expires.astimezone().strftime("%Y-%m-%d")
        if days_left < 0:
            return f"{date_str} [EXPIRED]", "red"
        elif days_left <= 7:
            return f"{date_str} ({days_left}d)", "red"
        elif days_left <= 30:
            return f"{date_str} ({days_left}d)", "yellow"
        else:
            return f"{date_str} ({days_left}d)", "green"
    except ValueError:
        return cred.expires_at, "dim"


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """Credential manager for Claude Code OAuth tokens.

    Run without arguments to open the interactive TUI.
    """
    configure_logging()
    if ctx.invoked_subcommand is None:
        from cc_cred.tui import CredManagerApp
        CredManagerApp().run()


@main.command()
@click.argument("token", required=False)
@click.option("--label", "-l", default="", help="Human-readable label for this credential.")
def add(token: Optional[str], label: str) -> None:
    """Register a new OAuth token.

    If you don't have a token yet, run without arguments to see setup instructions.
    """
    if not token:
        console.print(SETUP_INSTRUCTIONS)
        return

    store = CredStore()
    try:
        cred = store.add(token, label)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/] {exc}")
        sys.exit(1)

    with console.status("Verifying token…"):
        status_val, err = check_token(token)

    now = datetime.now(timezone.utc).isoformat()
    store._save_status({
        **{k: v for k, v in _load_status_dict(store).items()},
        cred.id: {
            "status": status_val if status_val != "invalid" else "admin_disabled",
            "reset_at": None,
            "last_checked": now,
            "last_error": err,
        },
    })

    if status_val == "invalid":
        console.print(f"[red]Token rejected by API ({err}). Credential stored but marked disabled.[/]")
    elif status_val == "unknown":
        console.print(f"[yellow]Could not verify token ({err}). Stored as unknown — will retry on next use.[/]")
    else:
        expiry_str, _ = _expiry_display(cred)
        console.print(
            f"[green]Added and verified[/] [dim]{cred.id[:8]}[/]"
            + (f" ({label})" if label else "")
            + f"  expires {expiry_str}"
        )

    if len(store.list()) == 1:
        store.set_active(cred.id)
        console.print("[dim]Set as active (first credential).[/]")


@main.command(name="list")
def list_creds() -> None:
    """List all registered credentials and their status."""
    store = CredStore()
    creds = store.list()

    if not creds:
        console.print("[dim]No credentials registered.[/]")
        console.print()
        console.print(SETUP_INSTRUCTIONS)
        return

    active = store.get_active()
    active_id = active.id if active else None

    table = Table(show_header=True, header_style="bold")
    table.add_column("", width=2)
    table.add_column("ID", style="dim")
    table.add_column("Label")
    table.add_column("Status")
    table.add_column("Resets At")
    table.add_column("Expires")

    colour_map = {
        "available": "green",
        "limited": "yellow",
        "admin_disabled": "red",
        "unknown": "dim",
    }

    with console.status(f"Checking {len(creds)} credential(s)…"):
        now = datetime.now(timezone.utc).isoformat()
        status_dict = _load_status_dict(store)
        for cred in creds:
            new_status, err = check_token(cred.token)
            existing = status_dict.get(cred.id, {})
            if store.get_status(cred.id).status not in ("limited", "admin_disabled"):
                status_dict[cred.id] = {
                    "status": new_status,
                    "reset_at": existing.get("reset_at"),
                    "last_checked": now,
                    "last_error": err,
                }
        store._save_status(status_dict)

    for cred in creds:
        status = store.get_status(cred.id)
        status_colour = colour_map.get(status.status, "white")
        marker = "[bold green]▶[/]" if cred.id == active_id else " "
        reset_str = status.reset_at or "—"
        expiry_str, expiry_colour = _expiry_display(cred)
        table.add_row(
            marker,
            cred.id[:8],
            cred.label or "—",
            f"[{status_colour}]{status.status}[/]",
            reset_str,
            f"[{expiry_colour}]{expiry_str}[/]",
        )

    console.print(table)


@main.command()
def status() -> None:
    """Show the currently active credential."""
    from cc_cred._logging import get_logger, mask_token
    log = get_logger()

    store = CredStore()
    active = store.get_active()

    log.debug("status command", extra={
        "active_id": active.id[:8] if active else None,
        "active_label": active.label if active else None,
    })

    if active is None:
        console.print("[yellow]No active credential set.[/]")
        console.print()
        console.print(SETUP_INSTRUCTIONS)
        sys.exit(1)

    cred_status = store.get_status(active.id)

    log.debug(f"cached status  status={cred_status.status}  last_checked={cred_status.last_checked}")

    log.debug(f"calling check_token  token={mask_token(active.token)}")
    with console.status("Checking token…"):
        new_status, err = check_token(active.token)
    log.debug(f"check_token result  new_status={new_status}  error={err}")
    now = datetime.now(timezone.utc).isoformat()
    status_dict = _load_status_dict(store)
    existing = status_dict.get(active.id, {})
    if cred_status.status not in ("limited", "admin_disabled"):
        status_dict[active.id] = {
            "status": new_status,
            "reset_at": existing.get("reset_at"),
            "last_checked": now,
            "last_error": err,
        }
        store._save_status(status_dict)
        cred_status = store.get_status(active.id)

    colour_map = {"available": "green", "limited": "yellow", "admin_disabled": "red", "unknown": "dim", "invalid": "red"}
    colour = colour_map.get(cred_status.status, "white")
    expiry_str, expiry_colour = _expiry_display(active)

    console.print(f"[bold]Active:[/] {active.id[:8]}" + (f" ({active.label})" if active.label else ""))
    console.print(f"[bold]Status:[/] [{colour}]{cred_status.status}[/]")
    console.print(f"[bold]Expires:[/] [{expiry_colour}]{expiry_str}[/]")
    if cred_status.reset_at:
        console.print(f"[bold]Resets:[/] {cred_status.reset_at}")


@main.command()
def rotate() -> None:
    """Manually rotate to the next available credential."""
    store = CredStore()
    next_cred = rotation.rotate(store)
    if next_cred is None:
        err_console.print("[red]No available credentials to rotate to.[/]")
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
        err_console.print(f"[red]No credential matching[/] '{id_or_label}'")
        sys.exit(1)

    store.set_active(match.id)
    console.print(f"[green]Active set to:[/] {match.id[:8]}" + (f" ({match.label})" if match.label else ""))


@main.command("install-hook")
def install_hook() -> None:
    """Add Stop and StopFailure hooks to ~/.claude/settings.json.

    Stop     — records session cost/token usage against the active credential.
    StopFailure — detects rate limits, rotates credential, and records usage.
    """
    settings_path = Path.home() / ".claude" / "settings.json"

    if not settings_path.exists():
        err_console.print(f"[red]Settings file not found:[/] {settings_path}")
        sys.exit(1)

    with open(settings_path, "r") as f:
        settings = json.load(f)

    hook_command = "cc-creds hook-event"
    hooks = settings.setdefault("hooks", {})
    installed: list[str] = []
    already: list[str] = []

    for event in ("Stop", "StopFailure", "UserPromptSubmit"):
        event_hooks = hooks.setdefault(event, [])
        found = any(
            h.get("command") == hook_command
            for entry in event_hooks
            for h in entry.get("hooks", [])
        )
        if found:
            already.append(event)
        else:
            event_hooks.append({"hooks": [{"type": "command", "command": hook_command}]})
            installed.append(event)

    if installed:
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        console.print(f"[green]Installed:[/] {', '.join(installed)} → {hook_command}")
    if already:
        console.print(f"[dim]Already installed:[/] {', '.join(already)}")


@main.command("hook-event")
def hook_event() -> None:
    """Process Stop and StopFailure hook events from stdin (called by Claude Code).

    Stop         — reads session state (or transcript fallback) and records
                   cost/token usage in sessions.jsonl against the active credential.
    StopFailure  — same as Stop, plus detects rate_limit, marks the credential
                   limited using resets_at from session state, and rotates.
    """
    from cc_cred.detection import parse_reset_time
    from cc_cred.session_tracker import get_session_usage
    from cc_cred._logging import get_logger
    log = get_logger()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except (json.JSONDecodeError, Exception):
        sys.exit(0)

    hook_type = data.get("hook_type", "")
    payload = data.get("payload", data)
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    cwd = payload.get("cwd", "")
    error = payload.get("error", "")

    log.debug("hook-event received", extra={
        "hook_type": hook_type,
        "session_id": session_id,
        "error": error,
        "has_transcript": bool(transcript_path),
    })

    if hook_type not in ("Stop", "StopFailure", "UserPromptSubmit"):
        sys.exit(0)

    store = CredStore()

    # Determine session owner via the inherited env var — the hook process is a
    # subprocess of Claude Code, so it inherits CLAUDE_CODE_OAUTH_TOKEN which is
    # the actual token the session was running with. Fall back to get_active() if
    # the var is absent (e.g. session predates cc-creds setup).
    session_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if session_token:
        session_cred = store.get_by_token(session_token)
        log.debug("session owner from env var", extra={
            "found": session_cred is not None,
            "id": session_cred.id[:8] if session_cred else None,
        })
    else:
        session_cred = None
        log.debug("CLAUDE_CODE_OAUTH_TOKEN not in env, falling back to get_active()")

    active = session_cred or store.get_active()
    if active is None:
        sys.exit(0)

    # Fetch usage from session state file, falling back to transcript.
    usage = get_session_usage(session_id, transcript_path) if session_id else None

    log.debug("session usage resolved", extra={
        "source": usage.source if usage else "none",
        "cost_usd": usage.cost_usd if usage else None,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
    })

    # Upsert the session record in sessions.jsonl.
    # UserPromptSubmit fires mid-session — register on first occurrence, then keep
    # updating with rolling stats. Stop/StopFailure finalise with the terminal status.
    if session_id:
        if not store.session_exists(session_id):
            store.register_session(session_id, active.id, cwd, "")

        if usage:
            if hook_type == "UserPromptSubmit":
                status_val = "running"
            elif hook_type == "StopFailure" and error == "rate_limit":
                status_val = "interrupted"
            elif hook_type == "StopFailure":
                status_val = "error"
            else:
                status_val = "success"

            store.update_session(
                session_id,
                cost_usd=usage.cost_usd,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                status=status_val,
            )

    # Rate limit handling (StopFailure only).
    if hook_type == "StopFailure" and error == "rate_limit":
        # Prefer resets_at from session state (clean Unix timestamp) over text parsing.
        reset_at = None
        if usage and usage.rate_limit_resets_at:
            reset_at = usage.rate_limit_resets_at
            log.debug("using resets_at from session state", extra={
                "reset_at": reset_at.isoformat(),
                "used_pct": usage.rate_limit_used_pct,
            })
        else:
            last_msg = payload.get("last_assistant_message", "")
            reset_at = parse_reset_time(last_msg)
            log.debug("using resets_at from text parse", extra={
                "reset_at": reset_at.isoformat() if reset_at else None,
                "raw": last_msg[:100],
            })

        store.mark_limited(active.id, reset_at=reset_at)
        next_cred = rotation.rotate(store)

        last_failure = store.STORE_DIR / "last-stop-failure.json"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "token_id": active.id,
            "rotated_to": next_cred.id if next_cred else None,
            "reset_at": reset_at.isoformat() if reset_at else None,
            "cwd": cwd,
            "raw_message": payload.get("last_assistant_message", ""),
        }
        with open(last_failure, "w") as f:
            json.dump(record, f, indent=2)

        if next_cred is None:
            # All credentials exhausted — surface a helpful message in the terminal.
            # Claude Code displays systemMessage output from StopFailure hooks.
            creds = store.list()
            reset_hints = []
            for c in creds:
                st = store.get_status(c.id)
                if st.reset_at:
                    reset_hints.append(f"{c.label or c.id[:8]} resets {st.reset_at}")
            hint = "  " + " / ".join(reset_hints) if reset_hints else ""
            msg = (
                "All cc-creds credentials are exhausted."
                + (f"\n{hint}" if hint else "")
                + "\n\nTo add another account:\n"
                + "  1. claude auth login\n"
                + "  2. claude setup-token\n"
                + "  3. cc-creds add <token> --label \"new account\""
            )
            print(json.dumps({"systemMessage": msg}))

    sys.exit(0)
