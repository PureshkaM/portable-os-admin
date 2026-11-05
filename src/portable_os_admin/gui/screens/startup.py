"""UC-1 "Открытие контейнера и создание сеанса" / UC-2 "Создание контейнера
администратора".

Экран идёт двумя шагами: сперва действие, потом пароль. До 11.09.2026 он был
формой -- поле пути, поле пароля и две кнопки, -- и у такой формы два изъяна,
оба найдены первым же взглядом на чистую машину.

**Акцент стоял не на том.** Кнопка «Открыть существующий» была главной, а
«Создать новый» -- второстепенной. Но на машине, где программу запустили
впервые, контейнера нет по определению: единственное возможное действие было
оформлено как запасное.

**Пустое поле пути означало неизвестно что.** Подсказка говорила «по умолчанию:
admin.container», не говоря где: путь относительный, то есть файл ложился в
текущий каталог процесса, а процесс запущен через polkit от root. Администратор
создавал контейнер и не знал, где он.

Оба изъяна снимает один и тот же порядок: кнопка спрашивает, что сделать, файл
выбирается диалогом, и только потом спрашивается пароль -- к уже названному
файлу. Пути «по умолчанию» не существует, и знать его неоткуда не нужно.

Программный вход при этом прежний: `path_var`, `password_var` и методы `_open`
/ `_create` работают как раньше. На них опираются тесты, которым диалог выбора
файла показать некому, и ломать их ради переделки внешнего вида незачем.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ... import ADMIN_APP_NAME
from .. import theme
from ..elevate import invoking_user_home
from ..widgets import MessageBar, add_field, error_guard

DEFAULT_CONTAINER_NAME = "admin.container"

#: Что делает экран прямо сейчас: спрашивает действие или пароль к выбранному
#: файлу. Хранится строкой, а не флагом: шагов может стать больше, а «не первый»
#: ничего не сказало бы о том, какой именно.
STAGE_CHOICE = "choice"
STAGE_PASSWORD = "password"

MODE_OPEN = "open"
MODE_CREATE = "create"


class StartupFrame(ttk.Frame):
    """Первый экран, и единственный, за которым ещё ничего нет.

    Карточка по центру окна, а не форма, прижатая к левому верхнему углу: здесь
    ровно одно дело, и по центру оно там, где уже находится взгляд.
    """

    FILE_TYPES = [("Контейнер администратора", "*.container"), ("Все файлы", "*")]

    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        self.path_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.confirm_var = tk.StringVar()
        self.stage = STAGE_CHOICE
        self.mode = MODE_OPEN

        # Пустые рамки сверху и снизу, обе растягиваются: карточка между ними
        # оказывается по центру по вертикали и остаётся там при любом размере.
        ttk.Frame(self, style="Window.TFrame").pack(side="top", fill="both", expand=True)
        ttk.Frame(self, style="Window.TFrame").pack(side="bottom", fill="both", expand=True)

        self.card = ttk.Frame(self, style="Card.TFrame", padding=theme.SPACE_XL)
        self.card.pack(padx=theme.SPACE_XL)

        self.message_bar = MessageBar(self.card, on_card=True)
        self._render()

    # -- отрисовка ----------------------------------------------------------
    def _render(self) -> None:
        for child in self.card.winfo_children():
            if child is not self.message_bar:
                child.destroy()
        self.message_bar.pack_forget()

        if self.stage == STAGE_CHOICE:
            self._render_choice()
        else:
            self._render_password()

        self.message_bar.pack(fill="x", pady=(theme.SPACE_MD, 0))

    def _render_choice(self) -> None:
        ttk.Label(self.card, text=f"{ADMIN_APP_NAME} — контейнер администратора", style="Title.TLabel").pack(
            anchor="w", pady=(0, theme.SPACE_LG)
        )

        buttons = ttk.Frame(self.card)
        buttons.pack(anchor="w")
        # Обе кнопки с многоточием: каждая ведёт к выбору файла, а не делает
        # что-то немедленно. Акцента нет ни на одной -- какая из них главная,
        # зависит от того, первый это запуск или сотый, и программе это
        # неизвестно. Прежде акцент стоял на «Открыть», то есть ровно на том,
        # что невозможно на чистой машине.
        self.open_btn = ttk.Button(buttons, text="Открыть…", command=self._choose_open)
        self.open_btn.pack(side="left")
        self.create_btn = ttk.Button(buttons, text="Создать…", command=self._choose_create)
        self.create_btn.pack(side="left", padx=(theme.SPACE_SM, 0))

    def _render_password(self) -> None:
        creating = self.mode == MODE_CREATE
        ttk.Label(
            self.card,
            text="Новый контейнер" if creating else "Открытие контейнера",
            style="Title.TLabel",
        ).pack(anchor="w")
        # Путь показывается, а не редактируется: он уже выбран, и менять его
        # здесь значило бы вернуть то самое поле, из-за которого контейнер
        # оказывался неизвестно где.
        ttk.Label(
            self.card,
            text=str(self._path()),
            style="Muted.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_LG))

        form = ttk.Frame(self.card)
        form.pack(anchor="w")
        password, row = add_field(
            form,
            0,
            "Пароль:" if not creating else "Придумайте пароль:",
            self.password_var,
            show="*",
            width=40,
            hint=("не короче 12 знаков, буквы и цифры" if creating else ""),
        )
        if creating:
            # Повтор только при создании. Опечатка в пароле к существующему
            # контейнеру обнаружится сразу -- он не откроется; опечатка при
            # создании запирает контейнер навсегда, и узнается это при
            # следующем запуске, когда в нём уже лежит работа.
            add_field(form, row, "Повторите пароль:", self.confirm_var, show="*", width=40)
        password.focus_set()
        password.bind("<Return>", lambda _event: self._confirm())

        buttons = ttk.Frame(self.card)
        buttons.pack(anchor="w", pady=(theme.SPACE_LG, 0))
        ttk.Button(
            buttons,
            text="Создать" if creating else "Открыть",
            style="Accent.TButton",
            command=self._confirm,
        ).pack(side="left")
        ttk.Button(buttons, text="Назад", command=self._back).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

    # -- выбор файла --------------------------------------------------------
    def _choose_open(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Выберите контейнер администратора",
            filetypes=self.FILE_TYPES,
            initialdir=self._initial_dir(),
        )
        if chosen:
            self._chosen(chosen, MODE_OPEN)

    def _choose_create(self) -> None:
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Куда сохранить новый контейнер",
            filetypes=self.FILE_TYPES,
            initialdir=self._initial_dir(),
            initialfile=DEFAULT_CONTAINER_NAME,
            defaultextension=".container",
            # Перезапись отклоняет сам ContainerManager, и сообщение у него
            # понятнее: «файл уже существует» против «перезаписать?».
            confirmoverwrite=False,
        )
        if chosen:
            self._chosen(chosen, MODE_CREATE)

    def _chosen(self, path: "str | Path", mode: str) -> None:
        """Файл выбран -- дальше спрашиваем пароль к нему."""
        self.path_var.set(str(path))
        self.mode = mode
        self.stage = STAGE_PASSWORD
        self.password_var.set("")
        self.confirm_var.set("")
        self.message_bar.clear()
        self._render()

    def _back(self) -> None:
        self.stage = STAGE_CHOICE
        self.password_var.set("")
        self.confirm_var.set("")
        self.message_bar.clear()
        self._render()

    # -- подтверждение ------------------------------------------------------
    def _confirm(self) -> None:
        if self.mode == MODE_CREATE:
            if self.password_var.get() != self.confirm_var.get():
                self.message_bar.error("Пароли не совпадают.")
                return
            self._create()
        else:
            self._open()

    # -- то, на что опираются тесты ----------------------------------------
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
