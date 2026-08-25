"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ...install import leftovers
from .. import theme
from ..widgets import MessageBar


class DashboardFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        # Bottom-up, so the actions and the message line keep their space and
        # the table gives up its own when the window is short.
        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD))

        actions = ttk.Frame(self, style="Window.TFrame")
        actions.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0))
        # Packed before the left-hand buttons on purpose: Tk's packer serves
        # whoever asks first, so a right-aligned widget added last gets only
        # what the others left over -- which on this row meant it was clipped
        # by the window edge.
        ttk.Button(
            actions, text="Закрыть сеанс", style="Quiet.TButton", command=self.app.close_session
        ).pack(side="right")

        # Installing is what this application is for; everything else supports
        # it. One accented button says so without a word of explanation.
        ttk.Button(
            actions, text="Установка ОС", style="Accent.TButton", command=self._open_install
        ).pack(side="left")
        for text, command in (
            # Building a distribution comes before installing in the list
            # because it comes before it in the work: one build, then a medium
            # per person.
            ("Собрать дистрибутив", self._open_build),
            ("Конфигурации", self._open_configs),
            ("Журналы", self._open_logs),
            ("Обмен", self._open_exchange),
            ("Восстановление", self._open_recovery),
            ("Состояние", self._check_leftovers),
        ):
            ttk.Button(actions, text=text, command=command).pack(
                side="left", padx=(theme.SPACE_SM, 0)
            )

        header = ttk.Frame(self, style="Window.TFrame")
        header.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM))
        ttk.Label(
            header,
            text="Переносные системы",
            style="Window.TLabel",
            font=theme.FONT_TITLE,
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="Носители, зарегистрированные в этом контейнере администратора.",
            style="Window.TLabel",
            foreground=theme.TEXT_MUTED,
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))

        card = ttk.Frame(self, style="Card.TFrame", padding=theme.SPACE_MD)
        card.pack(side="top", fill="both", expand=True, padx=theme.SPACE_LG)

        columns = ("media_id", "label", "os", "config", "created")
        headings = {
            "media_id": "Идентификатор",
            "label": "Метка",
            "os": "ОС",
            "config": "Конфигурация",
            "created": "Создан",
        }
        widths = {"media_id": 220, "label": 200, "os": 100, "config": 180, "created": 180}
        self.table = ttk.Treeview(card, columns=columns, show="headings", height=10)
        for col in columns:
            self.table.heading(col, text=headings[col])
            self.table.column(col, width=widths[col], anchor="w")
        self.table.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        self._refresh_table()
        # Said plainly rather than left as an empty grid, which reads as a
        # screen that failed to load rather than as a container with nothing in
        # it yet.
        if not self.app.session.container.data.media_records:
            self.message_bar.info(
                "В контейнере пока нет носителей. Начните с «Установка ОС»."
            )

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for record in self.app.session.container.data.media_records:
            self.table.insert(
                "", "end", values=(record.media_id, record.label, record.target_os, record.config_name, record.created_at)
            )

    def _check_leftovers(self) -> None:
        """Reports, and offers to clear, what an interrupted run left behind.

        An install that was killed leaves the medium mounted, its encrypted
        volume open and its bootstrap process orphaned and still writing. The
        program cannot notice that by itself after the fact, so this is where an
        administrator can look and put it right.
        """
        found = leftovers.find()
        if not found:
            self.message_bar.success("Незавершённых установок нет, носители не заняты.")
            return
        if not messagebox.askyesno(
            "Найдена незавершённая установка",
            "От прерванной установки остались занятые ресурсы:\n\n"
            + found.describe()
            + "\n\nОсвободить их? Носитель после этого придётся размечать заново — "
            "установка, которую прервали, всё равно не может быть продолжена.",
            icon="warning",
            parent=self,
        ):
            self.message_bar.info("Ресурсы оставлены как есть.")
            return
        remaining = leftovers.clear()
        if remaining:
            self.message_bar.error("Освободить удалось не всё:\n" + remaining.describe())
        else:
            self.message_bar.success("Всё освобождено, носитель можно извлекать или размечать заново.")

    def _open_build(self) -> None:
        from .build_wizard import BuildWizardFrame

        self.app.show_frame(BuildWizardFrame)

    def _open_configs(self) -> None:
        from .configs_list import ConfigsListFrame

        self.app.show_frame(ConfigsListFrame)

    def _open_install(self) -> None:
        from .install_wizard import InstallWizardFrame

        self.app.show_frame(InstallWizardFrame)

    def _open_logs(self) -> None:
        from .logs import LogsFrame

        self.app.show_frame(LogsFrame)

    def _open_exchange(self) -> None:
        from .exchange import ExchangeFrame

        self.app.show_frame(ExchangeFrame)

    def _open_recovery(self) -> None:
        from .recovery import RecoveryFrame

        self.app.show_frame(RecoveryFrame)
