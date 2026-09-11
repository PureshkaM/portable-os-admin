"""UC-1 "Открытие контейнера и создание сеанса" / UC-2 "Создание контейнера
администратора"."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from .. import theme
from ..elevate import invoking_user_home
from ..widgets import MessageBar, add_field, error_guard

DEFAULT_CONTAINER_NAME = "admin.container"


class StartupFrame(ttk.Frame):
    """The first screen, and the only one with nothing behind it yet.

    Laid out as a single card in the middle of the window rather than a form
    pinned to the top-left corner. There is exactly one thing to do here, and
    centring it puts that thing where the eye already is instead of leaving
    most of the window as empty grey.
    """

    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        # An empty frame above and below, both expanding: the card between them
        # ends up vertically centred and stays centred as the window resizes.
        ttk.Frame(self, style="Window.TFrame").pack(side="top", fill="both", expand=True)
        ttk.Frame(self, style="Window.TFrame").pack(side="bottom", fill="both", expand=True)

        card = ttk.Frame(self, style="Card.TFrame", padding=theme.SPACE_XL)
        card.pack(padx=theme.SPACE_XL)

        ttk.Label(card, text="Контейнер администратора", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="Если файла ещё нет — «Создать новый» создаст его по этому пути.",
            style="Muted.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_LG))

        form = ttk.Frame(card)
        form.pack(anchor="w")
        self.path_var = tk.StringVar()
        _, row = add_field(
            form,
            0,
            "Путь к контейнеру:",
            self.path_var,
            hint=f"по умолчанию: {DEFAULT_CONTAINER_NAME}",
            width=40,
        )
        # A container lives wherever the administrator put it. Typing that path
        # from memory is slower and invites a typo that turns "открыть" into
        # "создать новый" somewhere unintended.
        browse = ttk.Frame(form)
        browse.grid(row=0, column=2, sticky="w", padx=(theme.SPACE_SM, 0))
        ttk.Button(browse, text="Выбрать файл…", command=self._browse_open).pack(side="left")
        ttk.Button(browse, text="Куда сохранить…", command=self._browse_create).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )
        self.password_var = tk.StringVar()
        password, _ = add_field(form, row, "Пароль:", self.password_var, show="*", width=40)
        # Enter submits, the way every other password field in the world does.
        password.bind("<Return>", lambda _event: self._open())
        password.focus_set()

        buttons = ttk.Frame(card)
        buttons.pack(anchor="w", pady=(theme.SPACE_LG, 0))
        # Opening an existing container is the everyday case; creating one
        # happens once. The accent goes on the former.
        ttk.Button(
            buttons, text="Открыть существующий", style="Accent.TButton", command=self._open
        ).pack(side="left")
        ttk.Button(buttons, text="Создать новый", command=self._create).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x", pady=(theme.SPACE_MD, 0))

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
            # Запись после успеха, а не до попытки: журнал отвечает на вопрос
            # «когда с контейнером работали», и неудачный ввод пароля ответом на
            # него не является. Неудачные попытки -- отдельный вопрос, и писать
            # их сюда значило бы утверждать, что журнал их видит все, тогда как
            # подбирать пароль к файлу можно и не запуская эту программу.
            from .. import app_journal

            app_journal.record(app_journal.CONTAINER_OPENED, self._path())
            self.app.open_session(container, self._path())

    def _create(self) -> None:
        with error_guard(self.message_bar):
            container = self.app.container_manager.create(self._path(), self.password_var.get())
            from .. import app_journal

            app_journal.record(app_journal.CONTAINER_CREATED, self._path())
            self.app.open_session(container, self._path())
