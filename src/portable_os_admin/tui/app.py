"""Textual TUI entry point.

Window flow mirrors the course-project's UI requirements ("Интерфейсы
пользователя-1..3"): a startup screen to open/create the admin container,
then a dashboard giving access to configuration management, installation,
log viewing, the exchange partition, and recovery-code issuance -- all
gated behind an open AdminSession, per "Требования к безопасности-1".
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App

from ..config import ConfigManager
from ..container import AdminContainer, ContainerManager
from ..session import AdminSession
from .screens.startup import StartupScreen


class PortableOSAdminApp(App):
    TITLE = "Portable OS Admin"
    CSS = """
    #startup-box, #title {
        margin: 1 2;
    }
    Input {
        margin-bottom: 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.container_manager = ContainerManager()
        self.config_manager = ConfigManager()
        self.session: AdminSession | None = None
        self.container_path: Path | None = None

    @property
    def configs_dir(self) -> Path:
        base = self.container_path.parent if self.container_path else Path.cwd()
        return base / "configs"

    def on_mount(self) -> None:
        self.push_screen(StartupScreen())

    def open_session(self, container: AdminContainer, path: Path) -> None:
        self.session = AdminSession(container)
        self.container_path = path
        from .screens.dashboard import DashboardScreen

        self.switch_screen(DashboardScreen())

    def save_container(self) -> None:
        if self.session is not None:
            self.container_manager.save(self.session.container)

    def close_session(self) -> None:
        if self.session is not None:
            self.save_container()
            self.session.close()
        self.session = None
        self.container_path = None
        self.switch_screen(StartupScreen())


def run() -> None:
    PortableOSAdminApp().run()


if __name__ == "__main__":
    run()
