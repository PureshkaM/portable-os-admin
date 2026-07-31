"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class DashboardFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(
            self, text="Переносные системы, зарегистрированные в контейнере", font=("TkDefaultFont", 13, "bold")
        ).pack(anchor="w", padx=16, pady=(16, 8))

        columns = ("media_id", "label", "os", "config", "created")
        headings = {
            "media_id": "media_id",
            "label": "метка",
            "os": "ОС",
            "config": "конфигурация",
            "created": "создан",
        }
        self.table = ttk.Treeview(self, columns=columns, show="headings", height=10)
        for col in columns:
            self.table.heading(col, text=headings[col])
            self.table.column(col, width=140, anchor="w")
        self.table.pack(fill="both", expand=True, padx=16)

        self._refresh_table()

        actions = ttk.Frame(self)
        actions.pack(fill="x", padx=16, pady=12)
        ttk.Button(actions, text="Конфигурации", command=self._open_configs).pack(side="left")
        ttk.Button(actions, text="Установка ОС", command=self._open_install).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Журналы", command=self._open_logs).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Раздел обмена", command=self._open_exchange).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Восстановление", command=self._open_recovery).pack(side="left", padx=(8, 0))

        close_row = ttk.Frame(self)
        close_row.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(close_row, text="Закрыть сеанс", command=self.app.close_session).pack(side="left")

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for record in self.app.session.container.data.media_records:
            self.table.insert(
                "", "end", values=(record.media_id, record.label, record.target_os, record.config_name, record.created_at)
            )

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
