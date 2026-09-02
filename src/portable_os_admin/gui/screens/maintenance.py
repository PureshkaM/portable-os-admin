"""Обслуживание: пароль контейнера и следы прерванных установок.

Два действия, которые делают редко и по случаю, а не в ходе работы. Раньше
они стояли отдельными кнопками в общем ряду главного экрана, вперемешку с
разделами и с тем, ради чего программу открывают, -- и именно этот ряд
однажды не поместился в окно.

Здесь они вместе не потому, что похожи, а потому что одинаково относятся к
работе: ни то, ни другое не нужно, пока всё идёт как надо.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ...install import leftovers
from .. import theme
from ..widgets import MessageBar


class MaintenanceFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD))

        header = ttk.Frame(self, style="Window.TFrame")
        header.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM))
        ttk.Label(header, text="Обслуживание", style="Window.TLabel", font=theme.FONT_TITLE).pack(
            anchor="w"
        )

        card = ttk.Frame(self, style="Card.TFrame", padding=theme.SPACE_MD)
        card.pack(side="top", fill="x", padx=theme.SPACE_LG)

        ttk.Label(card, text="Пароль контейнера", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            text="Контейнер хранит ключи всех выданных носителей: сменить пароль стоит "
            "тогда же, когда меняют любой другой ключ от хранилища.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        ttk.Button(card, text="Сменить пароль…", command=self._open_password).pack(anchor="w")

        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(card, text="Следы прерванных установок", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            # Подпись объясняет не кнопку, а то, чего без неё не увидеть:
            # прерванная установка оставляет систему в состоянии, которое
            # снаружи ничем себя не выдаёт.
            text="Установка, которую прервали, оставляет носитель смонтированным, его "
            "том открытым, а процесс сборки — работающим. Сама программа об этом "
            "потом не узнает, поэтому смотреть сюда.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        ttk.Button(card, text="Проверить состояние", command=self._check_leftovers).pack(anchor="w")

    def _open_password(self) -> None:
        from .password import PasswordFrame

        self.app.show_frame(PasswordFrame)

    def _check_leftovers(self) -> None:
        """Сообщает, что осталось от прерванной установки, и предлагает убрать."""
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
            self.message_bar.success(
                "Всё освобождено, носитель можно извлекать или размечать заново."
            )
