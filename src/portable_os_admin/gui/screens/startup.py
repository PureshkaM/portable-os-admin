"""UC-1 "Открытие контейнера и создание сеанса" / UC-2 "Создание контейнера
администратора"."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ..widgets import MessageBar, add_field, error_guard

DEFAULT_CONTAINER_NAME = "admin.container"


class StartupFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Контейнер администратора", font=("TkDefaultFont", 14, "bold")).pack(
            anchor="w", padx=16, pady=(16, 4)
        )
        ttk.Label(
            self,
            text=(
                "Укажите файл контейнера и пароль. Если файла ещё нет — нажмите "
                '"Создать новый", он будет создан по этому пути.'
            ),
            foreground="#555555",
            wraplength=600,
            justify="left",
        ).pack(anchor="w", padx=16)

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16, pady=16)
        self.path_var = tk.StringVar()
        add_field(
            form, 0, "Путь к контейнеру:", self.path_var, hint=f"по умолчанию: {DEFAULT_CONTAINER_NAME}", width=40
        )
        self.password_var = tk.StringVar()
        add_field(form, 2, "Пароль:", self.password_var, show="*", width=40)

        buttons = ttk.Frame(self)
        buttons.pack(anchor="w", padx=16)
        ttk.Button(buttons, text="Открыть существующий", command=self._open).pack(side="left")
        ttk.Button(buttons, text="Создать новый", command=self._create).pack(side="left", padx=(8, 0))

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16, pady=(8, 0))

    def _path(self) -> Path:
        raw = self.path_var.get().strip()
        return Path(raw) if raw else Path(DEFAULT_CONTAINER_NAME)

    def _open(self) -> None:
        with error_guard(self.message_bar):
            container = self.app.container_manager.open(self._path(), self.password_var.get())
            self.app.open_session(container, self._path())

    def _create(self) -> None:
        with error_guard(self.message_bar):
            container = self.app.container_manager.create(self._path(), self.password_var.get())
            self.app.open_session(container, self._path())
