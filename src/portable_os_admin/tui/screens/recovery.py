"""UC-7 "Выдача кода восстановления доступа"."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Static

from ...errors import MediaNotRegisteredError, RecoveryDeniedError, RecoveryRequestInvalidError
from ...recovery import RecoveryService


class RecoveryScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Выдача кода восстановления доступа", id="title"),
            Input(placeholder="Запрос восстановления (media_id:слот:контрольная_сумма)", id="request"),
            Horizontal(
                Button("Выдать код", id="issue", variant="primary"),
                Button("Назад", id="back"),
            ),
            Static("", id="message"),
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "back":
            self.app.pop_screen()
            return
        if event.button.id != "issue":
            return

        message = self.query_one("#message", Static)
        request_text = self.query_one("#request", Input).value.strip()
        if not request_text:
            message.update("[red]Введите запрос восстановления[/red]")
            return

        service = RecoveryService(self.app.session)
        try:
            code = service.issue_code(request_text)
        except RecoveryRequestInvalidError as exc:
            message.update(f"[red]{exc}[/red]")
            return
        except MediaNotRegisteredError as exc:
            message.update(f"[red]{exc}[/red]")
            return
        except RecoveryDeniedError as exc:
            message.update(f"[red]{exc}[/red]")
            return

        self.app.save_container()
        message.update(f"[green]Код восстановления: {code} — передайте его пользователю[/green]")
