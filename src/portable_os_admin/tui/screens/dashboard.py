"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Static


class DashboardScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Переносные системы, зарегистрированные в контейнере", id="title"),
            DataTable(id="media_table"),
            Horizontal(
                Button("Конфигурации", id="configs"),
                Button("Установка ОС", id="install"),
                Button("Журналы", id="logs"),
                Button("Раздел обмена", id="exchange"),
                Button("Восстановление", id="recovery"),
            ),
            Horizontal(
                Button("Закрыть сеанс", id="close", variant="error"),
            ),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_table()

    def on_screen_resume(self) -> None:
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#media_table", DataTable)
        table.clear(columns=True)
        table.add_columns("media_id", "метка", "ОС", "конфигурация", "создан")
        for record in self.app.session.container.data.media_records:
            table.add_row(record.media_id, record.label, record.target_os, record.config_name, record.created_at)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "configs":
            from .configs import ConfigsScreen

            self.app.push_screen(ConfigsScreen())
        elif button_id == "install":
            from .install import InstallScreen

            self.app.push_screen(InstallScreen())
        elif button_id == "logs":
            from .logs import LogsScreen

            self.app.push_screen(LogsScreen())
        elif button_id == "exchange":
            from .exchange import ExchangeScreen

            self.app.push_screen(ExchangeScreen())
        elif button_id == "recovery":
            from .recovery import RecoveryScreen

            self.app.push_screen(RecoveryScreen())
        elif button_id == "close":
            self.app.close_session()
