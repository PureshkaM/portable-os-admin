"""Обслуживание: пароль контейнера и следы прерванных установок.

Два действия, которые делают редко и по случаю, а не в ходе работы. Раньше
они стояли отдельными кнопками в общем ряду главного экрана, вперемешку с
разделами и с тем, ради чего программу открывают, -- и именно этот ряд
однажды не поместился в окно.

Здесь они вместе не потому, что похожи, а потому что одинаково относятся к
работе: ни то, ни другое не нужно, пока всё идёт как надо.
"""
from __future__ import annotations

import pathlib
import tkinter as tk
from tkinter import messagebox, ttk

from ...install import leftovers
from .. import theme
from ..widgets import MessageBar, ScrollableArea, section_card, section_header


class MaintenanceFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD))

        section_header(
            self,
            "Обслуживание",
            "Действия над самим контейнером и следами прерванных установок.",
        )

        # Прокрутка, а не «авось поместится»: разделов стало три, и в окне
        # 900x600 кнопка последнего съезжала за нижний край -- это поймала
        # проверка «ни один элемент не сплющен», написанная после того, как
        # однажды не поместился ряд кнопок главного экрана. Приём тот же, что и
        # у шагов мастера: содержимое кладётся в `.content`.
        outer = section_card(self, grow=True)
        scroller = ScrollableArea(outer)
        scroller.pack(fill="both", expand=True)
        card = scroller.content

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

        ttk.Separator(card).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(card, text="Ресурс отзыва носителей", font=theme.FONT_SECTION).pack(anchor="w")
        ttk.Label(
            card,
            text="Серверу, который отвечает носителям на вопрос «не отозван ли я», "
            "нужны закрытый ключ подписи и список состояний. Ключ отзыва отдельный от "
            "всего прочего в контейнере: сервер общедоступен, и его взлом должен стоить "
            "не больше, чем возможность отозвать носители.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(theme.SPACE_XS, theme.SPACE_SM))
        ttk.Button(
            card, text="Выгрузить для сервера…", command=self._export_revocation
        ).pack(anchor="w")

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

    def _export_revocation(self) -> None:
        """Выгружает то, без чего сервер отзыва не может ответить носителю.

        Два файла, а не один: ключ меняется раз в жизни контейнера, а список
        состояний -- при каждом отзыве, и обновлять их придётся по-разному.
        """
        from tkinter import filedialog

        from ...install import revocation

        directory = filedialog.askdirectory(parent=self, title="Куда выгрузить")
        if not directory:
            return

        container = self.app.session.container
        key_path = pathlib.Path(directory) / "revocation-key.pem"
        states_path = pathlib.Path(directory) / "revocation-states.txt"
        try:
            key_path.write_text(
                revocation.private_key_pem(container.revocation_secret), encoding="ascii"
            )
            # Права до того, как файл уедет куда-либо: закрытый ключ, лежащий с
            # правами по умолчанию, читается любым пользователем машины.
            key_path.chmod(0o600)
            states_path.write_text(
                revocation.server_states(container.data.media_records), encoding="utf-8"
            )
        except OSError as exc:
            self.message_bar.error(f"Не удалось выгрузить: {exc}")
            return

        # Ключ мог быть заведён только что -- контейнеры, созданные до отзыва,
        # получают семя при первом обращении. Без сохранения носители получили
        # бы открытую часть от одного ключа, а сервер -- закрытую от другого.
        self.app.save_container()
        self.message_bar.success(
            f"Выгружено: {key_path.name} и {states_path.name}. Ключ закрытый — "
            "передайте его серверу и не оставляйте копий там, где он не нужен."
        )
