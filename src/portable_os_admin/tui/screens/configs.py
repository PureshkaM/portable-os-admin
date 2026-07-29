"""UC-3 "Управление файлом конфигурации установки"."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from ...config.validator import ConfigValidationUnavailableError


class ConfigsScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Файлы конфигурации установки", id="title"),
            DataTable(id="configs_table"),
            Input(placeholder="Имя новой конфигурации", id="new_name"),
            Horizontal(
                Button("Создать (Debian)", id="new_debian"),
                Button("Создать (RedOS)", id="new_redos"),
                Button("Проверить выбранную", id="validate", variant="primary"),
                Button("Назад", id="back"),
            ),
            Static("", id="message"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._paths = []
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#configs_table", DataTable)
        table.clear(columns=True)
        table.add_columns("файл", "цель", "релиз", "статус проверки")
        self._paths = self.app.config_manager.list_configs(self.app.configs_dir)
        for path in self._paths:
            config = self.app.config_manager.open(path)
            table.add_row(path.name, config.target_os, config.release, config.validation_status)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        message = self.query_one("#message", Static)
        button_id = event.button.id

        if button_id in ("new_debian", "new_redos"):
            name = self.query_one("#new_name", Input).value.strip()
            if not name:
                message.update("[red]Введите имя конфигурации[/red]")
                return
            target_os = "debian" if button_id == "new_debian" else "redos"
            self.app.config_manager.create(self.app.configs_dir, name, target_os)
            message.update(f"[green]Создана конфигурация «{name}»[/green]")
            self._refresh_table()

        elif button_id == "validate":
            table = self.query_one("#configs_table", DataTable)
            if not self._paths or table.cursor_row is None:
                message.update("[red]Выберите конфигурацию в таблице[/red]")
                return
            path = self._paths[table.cursor_row]
            config = self.app.config_manager.open(path)
            try:
                self.app.config_manager.validate(config)
            except ConfigValidationUnavailableError as exc:
                message.update(f"[red]Проверка недоступна: {exc}[/red]")
                return
            self.app.config_manager.save(config, path)
            if config.validation_status == "valid":
                message.update("[green]Конфигурация корректна[/green]")
            else:
                message.update("[red]" + "; ".join(config.validation_errors) + "[/red]")
            self._refresh_table()

        elif button_id == "back":
            self.app.pop_screen()
