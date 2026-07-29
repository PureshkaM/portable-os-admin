"""UC-1 "Открытие контейнера и создание сеанса" / UC-2 "Создание контейнера
администратора"."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Static

from ...errors import (
    ContainerAlreadyExistsError,
    ContainerAuthenticationError,
    ContainerCorruptedError,
    WeakPasswordError,
)

DEFAULT_CONTAINER_NAME = "admin.container"


class StartupScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Контейнер администратора", id="title"),
            Input(placeholder=f"Путь к контейнеру (по умолчанию: {DEFAULT_CONTAINER_NAME})", id="path"),
            Input(placeholder="Пароль", password=True, id="password"),
            Horizontal(
                Button("Открыть существующий", id="open", variant="primary"),
                Button("Создать новый", id="create"),
            ),
            Static("", id="message"),
            id="startup-box",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        raw_path = self.query_one("#path", Input).value.strip()
        path = Path(raw_path) if raw_path else Path(DEFAULT_CONTAINER_NAME)
        password = self.query_one("#password", Input).value
        message = self.query_one("#message", Static)

        cm = self.app.container_manager
        try:
            if event.button.id == "open":
                container = cm.open(path, password)
            else:
                container = cm.create(path, password)
        except ContainerAuthenticationError as exc:
            message.update(f"[red]{exc}[/red]")
            return
        except ContainerAlreadyExistsError as exc:
            message.update(f"[red]{exc}[/red]")
            return
        except ContainerCorruptedError as exc:
            message.update(f"[red]{exc}[/red]")
            return
        except WeakPasswordError as exc:
            message.update("[red]" + "; ".join(exc.problems) + "[/red]")
            return

        self.app.open_session(container, path)
