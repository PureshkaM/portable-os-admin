"""Смена пароля контейнера администратора.

Отдельный экран, а не диалог поверх панели: смена пароля -- операция, после
которой файл на диске становится другим, и ей место там же, где остальные
действия администратора, а не во всплывающем окне, которое закрывают
случайным щелчком.

Носителей эта операция не касается вовсе. Пароль шифрует сам файл, а коды
восстановления выводятся из секрета администратора, который лежит внутри и
остаётся прежним: носитель, собранный вчера, после смены пароля откроется
теми же кодами.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...errors import WeakPasswordError
from ..widgets import MessageBar, add_field, error_guard


class PasswordFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(
            self,
            text="Смена пароля контейнера",
            font=("TkDefaultFont", 13, "bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16)
        self.password_var = tk.StringVar()
        self.repeat_var = tk.StringVar()
        # Старый пароль не спрашивается: контейнер открыт, то есть он уже
        # предъявлен в этом сеансе, и другого способа сюда попасть нет.
        add_field(form, 0, "Новый пароль:", self.password_var, show="*", width=30)
        add_field(form, 1, "Повторите пароль:", self.repeat_var, show="*", width=30)

        actions = ttk.Frame(self)
        actions.pack(anchor="w", padx=16, pady=8)
        ttk.Button(actions, text="Сменить пароль", command=self._change).pack(side="left")
        ttk.Button(actions, text="Назад", command=self.app.show_dashboard).pack(
            side="left", padx=(8, 0)
        )

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

    def _change(self) -> None:
        password = self.password_var.get()
        repeat = self.repeat_var.get()
        if not password:
            self.message_bar.error("Введите новый пароль.")
            return
        if password != repeat:
            self.message_bar.error("Пароли не совпали.")
            return

        # error_guard, а не голый вызов: правило проекта -- пользователю не
        # показывают traceback. Слабый пароль ловится отдельно, потому что это
        # не ошибка программы, а ответ ей.
        with error_guard(self.message_bar):
            try:
                self.app.container_manager.change_password(
                    self.app.session.container, password
                )
            except WeakPasswordError as exc:
                self.message_bar.error(str(exc))
                return

        self.password_var.set("")
        self.repeat_var.set("")
        self.message_bar.success(
            "Пароль изменён. Носители, выданные этим контейнером, "
            "продолжают открываться теми же кодами восстановления."
        )
