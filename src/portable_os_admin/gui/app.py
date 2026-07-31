"""Tkinter GUI entry point.

Same window flow as the Textual TUI (`portable_os_admin.tui.app`) -- a
startup screen to open/create the admin container, then a dashboard giving
access to configuration management, installation, log viewing, the exchange
partition, and recovery-code issuance -- but the multi-field flows
(configuration authoring, installation) are wizards here instead of single
crowded forms. See `gui/__init__.py` for why this presentation layer exists
alongside the TUI rather than replacing it.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..config import ConfigManager
from ..container import AdminContainer, ContainerManager
from ..session import AdminSession


class PortableOSAdminGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Portable OS Admin")
        self.geometry("880x640")
        self.minsize(760, 560)

        style = ttk.Style(self)
        # "clam" renders consistently across the Linux desktops this app
        # actually targets (RedOS/Astra/Debian window managers); left
        # unset, Tk falls back to a platform default that can look
        # inconsistent between them. Fall back silently if unavailable.
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.container_manager = ContainerManager()
        self.config_manager = ConfigManager()
        self.session: AdminSession | None = None
        self.container_path: Path | None = None

        self._content = ttk.Frame(self)
        self._content.pack(fill="both", expand=True)
        self._current_frame: ttk.Frame | None = None

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.show_startup()

    @property
    def configs_dir(self) -> Path:
        base = self.container_path.parent if self.container_path else Path.cwd()
        return base / "configs"

    def show_frame(self, frame_cls, **kwargs) -> None:
        if self._current_frame is not None:
            self._current_frame.destroy()
        self._current_frame = frame_cls(self._content, self, **kwargs)
        self._current_frame.pack(fill="both", expand=True)

    def show_startup(self) -> None:
        from .screens.startup import StartupFrame

        self.show_frame(StartupFrame)

    def show_dashboard(self) -> None:
        from .screens.dashboard import DashboardFrame

        self.show_frame(DashboardFrame)

    def open_session(self, container: AdminContainer, path: Path) -> None:
        self.session = AdminSession(container)
        self.container_path = path
        self.show_dashboard()

    def save_container(self) -> None:
        if self.session is not None:
            self.container_manager.save(self.session.container)

    def close_session(self) -> None:
        if self.session is not None:
            self.save_container()
            self.session.close()
        self.session = None
        self.container_path = None
        self.show_startup()

    def _on_close_window(self) -> None:
        self.save_container()
        self.destroy()


def run() -> None:
    PortableOSAdminGUI().mainloop()


if __name__ == "__main__":
    run()
