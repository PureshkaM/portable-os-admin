"""UC-5 "Просмотр журналов операционной системы".

Operates on an already-mounted log-partition directory (the admin mounts
POA_LOGS beforehand); see docs/ARCHITECTURE.md for why the TUI does not
drive real `mount`/`umount` itself in v0.1.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static

from ...errors import LogsCorruptedError, LogsUnavailableError, MediaNotRegisteredError
from ...admin import LogViewer, find_media_by_boot_metadata


class LogsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Просмотр журналов операционной системы", id="title"),
            Input(placeholder="Точка монтирования загрузочного раздела (для сопоставления носителя)", id="boot_mount"),
            Input(placeholder="Точка монтирования раздела логов", id="logs_mount"),
            Input(placeholder="Фильтр (необязательно)", id="filter"),
            Horizontal(
                Button("Загрузить журналы", id="load", variant="primary"),
                Button("Назад", id="back"),
            ),
            RichLog(id="log_view"),
            Static("", id="message"),
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        if event.button.id != "load":
            return

        message = self.query_one("#message", Static)
        log_view = self.query_one("#log_view", RichLog)
        log_view.clear()

        boot_mount = self.query_one("#boot_mount", Input).value.strip()
        logs_mount = self.query_one("#logs_mount", Input).value.strip()
        filter_text = self.query_one("#filter", Input).value.strip() or None

        if boot_mount:
            try:
                record = find_media_by_boot_metadata(self.app.session.container, boot_mount)
            except MediaNotRegisteredError as exc:
                message.update(f"[red]{exc}[/red]")
                return
            message.update(f"[green]Носитель опознан: {record.media_id} ({record.label})[/green]")

        if not logs_mount:
            message.update("[red]Укажите точку монтирования раздела логов[/red]")
            return

        try:
            lines = LogViewer().read_logs(logs_mount, filter_substring=filter_text)
        except (LogsUnavailableError, LogsCorruptedError) as exc:
            message.update(f"[red]{exc}[/red]")
            return

        for line in lines:
            log_view.write(line)
