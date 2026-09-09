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

import gc
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from tkinter import messagebox

from ..config import ConfigManager
from ..container import AdminContainer, ContainerManager
from ..session import AdminSession
from . import theme
from .elevate import elevate_or_exit


class PortableOSAdminGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Portable OS Admin")
        # Wider than before: the wizard now shows its step list alongside the
        # content instead of a bare progress bar, and the form needs the same
        # room it always did next to it.
        self.geometry("1040x700")
        self.minsize(900, 600)

        # One call decides colours, fonts and spacing everywhere -- see
        # gui/theme.py. It selects "clam" for the same reason this code always
        # did: it is the one built-in theme that renders consistently across
        # the Linux desktops this application targets, and the only one that
        # honours the colours set on it.
        try:
            theme.apply(self)
        except tk.TclError:
            # A Tk without the clam theme still has to run, just plainer.
            pass

        self.container_manager = ContainerManager()
        self.config_manager = ConfigManager()
        self.session: AdminSession | None = None
        self.container_path: Path | None = None

        self._content = ttk.Frame(self)
        self._content.pack(fill="both", expand=True)
        self._current_frame: ttk.Frame | None = None
        #: Оболочка с боковой навигацией. Живёт, пока открыт сеанс: разделы
        #: сменяются внутри неё, а мастера занимают окно целиком и убирают её.
        self._shell = None
        self._status: tk.StringVar | None = None

        self.protocol("WM_DELETE_WINDOW", self._on_close_window)
        self.show_startup()

    @property
    def container(self) -> "AdminContainer | None":
        """Открытый контейнер -- единственное хранилище всего, что ведёт

        администратор: носителей, записей восстановления и конфигураций.
        Каталога `configs/` рядом с файлом больше нет; до 09.09.2026
        конфигурации лежали там открытым текстом.
        """
        return self.session.container if self.session is not None else None

    def show_frame(self, frame_cls, **kwargs) -> None:
        """Экран во весь размер окна: начальный экран и мастера.

        Мастер занимает окно целиком намеренно: у него свой список шагов
        слева, и вторая навигация рядом предлагала бы уйти из середины работы,
        которую нельзя бросить на полпути.
        """
        self._teardown_current()
        if self._shell is not None:
            self._shell.destroy()
            self._shell = None
        self._current_frame = frame_cls(self._content, self, **kwargs)
        self._current_frame.pack(fill="both", expand=True)

    def show_section(self, key: str, **kwargs) -> None:
        """Раздел внутри оболочки с боковой навигацией.

        Разделы -- это «где я нахожусь»: список носителей, конфигурации,
        журналы, обмен, восстановление, обслуживание. Оболочка при переходе не
        пересоздаётся, поэтому список слева не мигает и не теряет прокрутку.
        """
        from .shell import NavigationShell

        screens = {
            "media": ("dashboard", "DashboardFrame"),
            "configs": ("configs_list", "ConfigsListFrame"),
            "logs": ("logs", "LogsFrame"),
            "exchange": ("exchange", "ExchangeFrame"),
            "recovery": ("recovery", "RecoveryFrame"),
            "maintenance": ("maintenance", "MaintenanceFrame"),
        }
        module_name, class_name = screens[key]
        module = __import__(
            f"portable_os_admin.gui.screens.{module_name}", fromlist=[class_name]
        )
        frame_cls = getattr(module, class_name)

        self._teardown_current()
        if self._shell is None:
            self._shell = NavigationShell(self._content, self)
            self._shell.pack(fill="both", expand=True)
        self._current_frame = frame_cls(self._shell.content, self, **kwargs)
        self._current_frame.pack(fill="both", expand=True)
        self._shell.mark_active(key)

    def _teardown_current(self) -> None:
        if self._current_frame is not None:
            self._current_frame.destroy()
            # On the UI thread, deliberately: a screen leaves tk.Variable
            # objects behind, and finalising one from a worker thread calls
            # Tcl from the wrong thread and hangs that thread for good. See
            # gui/widgets.py run_in_background.
            gc.collect()
            self._current_frame = None

    def set_status(self, text: str) -> None:
        """Строка пояснения к разделу, на который наведён курсор.

        Пусто -- законное значение: убрать пояснение так же важно, как
        показать, иначе оно останется от раздела, который уже не под курсором.
        """
        if self._status is not None:
            self._status.set(text)

    def show_startup(self) -> None:
        from .screens.startup import StartupFrame

        self.show_frame(StartupFrame)

    def show_dashboard(self) -> None:
        """Список носителей -- он же раздел, с которого начинается работа."""
        self.show_section("media")

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
        self.show_startup()  # он же уберёт оболочку с навигацией

    def _on_close_window(self) -> None:
        # Closing mid-install is how the medium ends up mounted, its volume
        # open and a bootstrap process orphaned and still writing. Asked rather
        # than prevented: it is the administrator's machine, and there are good
        # reasons to stop.
        if self.install_in_progress():
            if not messagebox.askyesno(
                "Идёт установка",
                "Сейчас идёт установка на носитель. Если закрыть программу, установка "
                "будет прервана, а носитель останется в непригодном состоянии — его "
                "придётся размечать заново.\n\nВсё равно закрыть?",
                icon="warning",
                default="no",
                parent=self,
            ):
                return
        self.save_container()
        self.destroy()

    def install_in_progress(self) -> bool:
        frame = self._current_frame
        return bool(getattr(frame, "_running", False))


def run() -> None:
    # Before any window: the program administers portable media and erases
    # disks, so it opens only for someone who can prove they are the machine's
    # administrator. This also removes the old failure mode where installation
    # was attempted unprivileged and died inside parted.
    elevate_or_exit()
    PortableOSAdminGUI().mainloop()


if __name__ == "__main__":
    run()
