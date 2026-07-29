"""UC-6 "Взаимодействие с разделом обмена" (administrator side)."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from ...admin import ExchangeManager, find_media_by_boot_metadata
from ...errors import ExchangeError, InsufficientSpaceError, MediaNotRegisteredError


class ExchangeScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Раздел обмена", id="title"),
            Input(placeholder="Точка монтирования загрузочного раздела (для сопоставления носителя)", id="boot_mount"),
            Input(placeholder="Точка монтирования раздела обмена", id="exchange_mount"),
            Button("Обновить список файлов", id="refresh"),
            DataTable(id="files_table"),
            Horizontal(
                Input(placeholder="Путь к файлу для записи", id="write_path"),
                Button("Записать", id="write"),
            ),
            Horizontal(
                Input(placeholder="Имя файла для чтения/удаления", id="selected_name"),
                Input(placeholder="Куда скопировать (для чтения)", id="read_dest"),
                Button("Прочитать", id="read"),
                Button("Удалить", id="delete", variant="error"),
            ),
            Static("", id="message"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#files_table", DataTable).add_columns("файл", "размер (байт)")

    def _mount_point(self) -> str | None:
        message = self.query_one("#message", Static)
        boot_mount = self.query_one("#boot_mount", Input).value.strip()
        exchange_mount = self.query_one("#exchange_mount", Input).value.strip()
        if boot_mount:
            try:
                record = find_media_by_boot_metadata(self.app.session.container, boot_mount)
                message.update(f"[green]Носитель опознан: {record.media_id} ({record.label})[/green]")
            except MediaNotRegisteredError as exc:
                message.update(f"[red]{exc}[/red]")
                return None
        if not exchange_mount:
            message.update("[red]Укажите точку монтирования раздела обмена[/red]")
            return None
        return exchange_mount

    def on_button_pressed(self, event: Button.Pressed) -> None:
        message = self.query_one("#message", Static)
        manager = ExchangeManager()

        if event.button.id == "refresh":
            mount_point = self._mount_point()
            if mount_point is None:
                return
            table = self.query_one("#files_table", DataTable)
            table.clear()
            try:
                for info in manager.list_files(mount_point):
                    table.add_row(info.name, str(info.size_bytes))
            except ExchangeError as exc:
                message.update(f"[red]{exc}[/red]")

        elif event.button.id == "write":
            mount_point = self._mount_point()
            if mount_point is None:
                return
            source = self.query_one("#write_path", Input).value.strip()
            if not source:
                message.update("[red]Укажите путь к файлу для записи[/red]")
                return
            try:
                manager.write_file(mount_point, Path(source))
                message.update(f"[green]Файл {source!r} записан в раздел обмена[/green]")
            except (ExchangeError, InsufficientSpaceError, OSError) as exc:
                message.update(f"[red]{exc}[/red]")

        elif event.button.id == "read":
            mount_point = self._mount_point()
            if mount_point is None:
                return
            name = self.query_one("#selected_name", Input).value.strip()
            dest = self.query_one("#read_dest", Input).value.strip()
            if not name or not dest:
                message.update("[red]Укажите имя файла и путь назначения[/red]")
                return
            try:
                manager.read_file(mount_point, name, Path(dest))
                message.update(f"[green]Файл {name!r} скопирован в {dest!r}[/green]")
            except (ExchangeError, OSError) as exc:
                message.update(f"[red]{exc}[/red]")

        elif event.button.id == "delete":
            mount_point = self._mount_point()
            if mount_point is None:
                return
            name = self.query_one("#selected_name", Input).value.strip()
            if not name:
                message.update("[red]Укажите имя файла для удаления[/red]")
                return
            try:
                manager.delete_file(mount_point, name)
                message.update(f"[green]Файл {name!r} удалён из раздела обмена[/green]")
            except (ExchangeError, OSError) as exc:
                message.update(f"[red]{exc}[/red]")
