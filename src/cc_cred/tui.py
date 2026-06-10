from datetime import datetime, timezone
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.coordinate import Coordinate
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)

from cc_cred.store import CredStore


STATUS_COLOURS = {
    "available": "green",
    "limited": "yellow",
    "admin_disabled": "red",
    "unknown": "dim",
}


def _mask_token(token: str) -> str:
    if len(token) <= 12:
        return "*" * len(token)
    return token[:8] + "..." + token[-4:]


def _format_reset(reset_at: Optional[str]) -> str:
    if not reset_at:
        return "—"
    try:
        dt = datetime.fromisoformat(reset_at)
        local = dt.astimezone()
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except ValueError:
        return reset_at


def _format_expiry(expires_at: Optional[str]) -> tuple[str, str]:
    """Return (text, colour) for token expiry."""
    if not expires_at:
        return "unknown", "dim"
    try:
        expires = datetime.fromisoformat(expires_at)
        now = datetime.now(timezone.utc)
        days_left = (expires - now).days
        date_str = expires.astimezone().strftime("%Y-%m-%d")
        if days_left < 0:
            return f"{date_str} EXPIRED", "red"
        elif days_left <= 7:
            return f"{date_str} ({days_left}d)", "red"
        elif days_left <= 30:
            return f"{date_str} ({days_left}d)", "yellow"
        else:
            return f"{date_str} ({days_left}d)", "green"
    except ValueError:
        return expires_at, "dim"


class AddKeyModal(ModalScreen):
    """Modal screen for adding a new credential."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="add-key-dialog"):
            yield Label("Add credential", id="add-key-title")
            yield Input(placeholder="OAuth token (sk-ant-...)", password=True, id="token-input")
            yield Input(placeholder="Label (optional)", id="label-input")
            with Container(id="add-key-buttons"):
                yield Button("Add", variant="primary", id="add-btn")
                yield Button("Cancel", id="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.dismiss(None)
            return

        token = self.query_one("#token-input", Input).value.strip()
        label = self.query_one("#label-input", Input).value.strip()

        if not token:
            self.query_one("#token-input", Input).focus()
            return

        self.dismiss((token, label))


class CredentialsTab(Container):
    """Credential manager tab with DataTable."""

    BINDINGS = [
        Binding("a", "add_key", "Add"),
        Binding("d", "delete_key", "Delete"),
        Binding("enter", "set_active", "Set active"),
        Binding("r", "rotate", "Rotate"),
    ]

    def __init__(self, store: CredStore) -> None:
        super().__init__()
        self._store = store

    def compose(self) -> ComposeResult:
        yield DataTable(id="creds-table", cursor_type="row")
        yield Static("", id="creds-status")

    def on_mount(self) -> None:
        self._build_table()

    def _build_table(self) -> None:
        table = self.query_one("#creds-table", DataTable)
        table.clear(columns=True)
        table.add_columns("", "ID", "Label", "Token", "Status", "Resets", "Expires")

        active = self._store.get_active()
        active_id = active.id if active else None

        for cred in self._store.list():
            status = self._store.get_status(cred.id)
            colour = STATUS_COLOURS.get(status.status, "white")
            marker = "[bold green]▶[/]" if cred.id == active_id else " "
            expiry_text, expiry_colour = _format_expiry(cred.expires_at)
            table.add_row(
                marker,
                cred.id[:8],
                cred.label or "—",
                _mask_token(cred.token),
                f"[{colour}]{status.status}[/]",
                _format_reset(status.reset_at),
                f"[{expiry_colour}]{expiry_text}[/]",
                key=cred.id,
            )

    def _selected_cred_id(self) -> Optional[str]:
        table = self.query_one("#creds-table", DataTable)
        if table.cursor_row is None:
            return None
        row_key, _ = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        return str(row_key.value) if row_key else None

    def action_add_key(self) -> None:
        self.app.push_screen(AddKeyModal(), self._on_add_key_result)

    def _on_add_key_result(self, result: Optional[tuple]) -> None:
        if result is None:
            return
        token, label = result
        try:
            self._store.add(token, label)
            self._build_table()
            self.query_one("#creds-status", Static).update("[green]Credential added.[/]")
        except ValueError as exc:
            self.query_one("#creds-status", Static).update(f"[red]{exc}[/]")

    def action_delete_key(self) -> None:
        cred_id = self._selected_cred_id()
        if not cred_id:
            return
        try:
            self._store.remove(cred_id)
            self._build_table()
            self.query_one("#creds-status", Static).update("[yellow]Credential removed.[/]")
        except KeyError as exc:
            self.query_one("#creds-status", Static).update(f"[red]{exc}[/]")

    def action_set_active(self) -> None:
        cred_id = self._selected_cred_id()
        if not cred_id:
            return
        self._store.set_active(cred_id)
        self._build_table()
        self.query_one("#creds-status", Static).update("[green]Active credential updated.[/]")

    def action_rotate(self) -> None:
        from cc_cred import rotation
        next_cred = rotation.rotate(self._store)
        if next_cred:
            self._build_table()
            label = next_cred.label or next_cred.id[:8]
            self.query_one("#creds-status", Static).update(f"[green]Rotated to: {label}[/]")
        else:
            self.query_one("#creds-status", Static).update("[red]No available credentials to rotate to.[/]")


class StatsTab(Container):
    """Per-credential usage stats from sessions.jsonl."""

    def __init__(self, store: CredStore) -> None:
        super().__init__()
        self._store = store

    def compose(self) -> ComposeResult:
        yield DataTable(id="stats-table", cursor_type="row")

    def on_mount(self) -> None:
        self._build_table()

    def _build_table(self) -> None:
        table = self.query_one("#stats-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Label", "Sessions", "Total Cost (USD)", "Input Tokens", "Output Tokens")

        for cred in self._store.list():
            stats = self._store.get_stats(cred.id)
            table.add_row(
                cred.label or cred.id[:8],
                str(stats.session_count),
                f"${stats.total_cost_usd:.4f}",
                str(stats.total_input_tokens),
                str(stats.total_output_tokens),
                key=cred.id,
            )


class CredManagerApp(App):
    """cc-creds interactive credential manager."""

    CSS = """
    #add-key-dialog {
        background: $surface;
        border: solid $primary;
        padding: 1 2;
        width: 60;
        height: auto;
        align: center middle;
    }
    #add-key-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #add-key-buttons {
        layout: horizontal;
        height: auto;
        margin-top: 1;
    }
    #add-key-buttons Button {
        margin-right: 1;
    }
    #creds-status {
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._store = CredStore()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            with TabPane("Credentials", id="creds-pane"):
                yield CredentialsTab(self._store)
            with TabPane("Stats", id="stats-pane"):
                yield StatsTab(self._store)
        yield Footer()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.tab.id == "stats-pane":
            stats_tab = self.query_one(StatsTab)
            stats_tab._build_table()
