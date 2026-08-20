"""UC-1 "Открытие контейнера и создание сеанса" / UC-2 "Создание контейнера
администратора"."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ..elevate import invoking_user_home
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
        _, row = add_field(
            form, 0, "Путь к контейнеру:", self.path_var, hint=f"по умолчанию: {DEFAULT_CONTAINER_NAME}", width=40
        )
        # A container lives wherever the administrator put it. Typing that path
        # from memory is slower and invites a typo that turns "открыть" into
        # "создать новый" somewhere unintended.
        browse = ttk.Frame(form)
        browse.grid(row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Button(browse, text="Выбрать файл…", command=self._browse_open).pack(side="left")
        ttk.Button(browse, text="Куда сохранить…", command=self._browse_create).pack(side="left", padx=(6, 0))
        self.password_var = tk.StringVar()
        add_field(form, row, "Пароль:", self.password_var, show="*", width=40)

        buttons = ttk.Frame(self)
        buttons.pack(anchor="w", padx=16)
        ttk.Button(buttons, text="Открыть существующий", command=self._open).pack(side="left")
        ttk.Button(buttons, text="Создать новый", command=self._create).pack(side="left", padx=(8, 0))

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16, pady=(8, 0))

    FILE_TYPES = [("Контейнер администратора", "*.container"), ("Все файлы", "*")]

    def _browse_open(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Выберите контейнер администратора",
            filetypes=self.FILE_TYPES,
            initialdir=self._initial_dir(),
        )
        if chosen:
            self.path_var.set(chosen)
            self.message_bar.clear()

    def _browse_create(self) -> None:
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Куда сохранить новый контейнер",
            filetypes=self.FILE_TYPES,
            initialdir=self._initial_dir(),
            initialfile=DEFAULT_CONTAINER_NAME,
            defaultextension=".container",
            confirmoverwrite=False,  # overwriting is refused by ContainerManager, with a clearer message
        )
        if chosen:
            self.path_var.set(chosen)
            self.message_bar.clear()

    def _initial_dir(self) -> str:
        current = self.path_var.get().strip()
        if current:
            parent = Path(current).expanduser().parent
            if parent.is_dir():
                return str(parent)
        return str(invoking_user_home())

    def _path(self) -> Path:
        raw = self.path_var.get().strip()
        return Path(raw).expanduser() if raw else Path(DEFAULT_CONTAINER_NAME)

    def _open(self) -> None:
        with error_guard(self.message_bar):
            container = self.app.container_manager.open(self._path(), self.password_var.get())
            self.app.open_session(container, self._path())

    def _create(self) -> None:
        with error_guard(self.message_bar):
            container = self.app.container_manager.create(self._path(), self.password_var.get())
            self.app.open_session(container, self._path())
