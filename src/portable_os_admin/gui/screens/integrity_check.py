"""Проверка целостности незашифрованной части носителя (задача 13).

Открывается двойным щелчком по строке носителя, а не кнопкой в ряду: проверка
относится к одному конкретному носителю, а не к контейнеру вообще, и выбор
носителя -- это и есть выбор строки. Седьмая кнопка в тот ряд к тому же не
помещается, и это ловит существующий тест.

Что здесь происходит и чего здесь не происходит
-----------------------------------------------

Считываются суммы всех файлов на ESP и `/boot` подключённого носителя и
сравниваются с описью, снятой при записи и лежащей в контейнере
администратора. Совпало -- загрузчик и initramfs те же, что записывали.
Не совпало -- незашифрованную часть меняли.

Это обнаружение, а не защита. Подменённый initramfs спрашивает пароль так же,
как настоящий, и если человек уже загрузился с такого носителя, пароль украден
до всякой проверки. Помешать может только Secure Boot с собственным ключом или
TPM, а их у носителя, который несут на чужую машину, нет по условию задачи.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...admin import MediaActions
from ...errors import PortableOSAdminError
from ...install import integrity
from .. import theme
from ..widgets import (
    MessageBar,
    button_row,
    run_in_background,
    section_card,
    section_header,
    widget_alive,
)


class IntegrityFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app, media_id: str = ""):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self.media_id = media_id
        self._actions = MediaActions(app.session)

        record = self._actions.find(media_id)
        title = "Проверка носителя"
        if record is not None:
            title += f": {record.label or record.media_id}"
        section_header(
            self,
            title,
            "Суммы файлов на ESP и /boot сверяются с описью из контейнера.",
        )

        self.record = record
        if record is None:
            card = section_card(self, grow=False)
            bar = MessageBar(card, on_card=True)
            bar.pack(fill="x")
            bar.error("Носитель не найден в контейнере.")
            ttk.Button(card, text="Назад", command=app.show_dashboard).pack(
                anchor="w", pady=(theme.SPACE_SM, 0)
            )
            return

        card = section_card(self)

        # Полей здесь нет, и это правка 11.09.2026.
        #
        # Прежде экран спрашивал два готовых пути и объяснял это нежеланием
        # требовать прав root. Объяснение было неверным дважды: программа и так
        # работает от root -- она размечает диски, -- а носитель к этому моменту
        # уже опознан по UUID своих томов. То есть администратору, которому
        # принесли флешку с вопросом «почему не грузится», предлагалось помнить
        # синтаксис `mount` ради того, что программа могла сделать сама.
        #
        # Теперь она и делает: находит носитель среди подключённых, монтирует
        # ESP и /boot только на чтение, считает суммы и размонтирует обратно.
        _row, buttons = button_row(
            card,
            (
                ("Проверить", self._check),
                ("Назад", app.show_dashboard),
            ),
            pady=theme.SPACE_SM,
        )
        self.check_btn = buttons[0]

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self.details = tk.Text(card, height=12, state="disabled")
        self.details.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))

    def _check(self) -> None:
        self.check_btn.configure(state="disabled")
        self.message_bar.info("Ищу носитель среди подключённых…")
        # Монтирование и подсчёт сумм всего, что лежит на ESP и /boot, --
        # операции блокирующие, а носитель может быть медленной флешкой. На
        # потоке интерфейса это означало бы замерший экран на всё время работы.
        run_in_background(
            self, self._compare, on_success=self._show_result, on_error=self._failed
        )

    def _compare(self) -> "dict[str, list[str]]":
        """Разбор подписи, опознание носителя и сверка сумм -- в службе.

        Здесь остаётся одна строка намеренно: тело проверки вынимало из
        контейнера секрет администратора прямо в экран. Экран, который умеет
        достать секрет, рано или поздно покажет его, запишет в журнал или
        отдаст в отчёт -- не по злому умыслу, а потому что он рядом (зона И3).
        """
        return self._actions.check_integrity(self.record)

    def _show_result(self, difference: "dict[str, list[str]]") -> None:
        if not widget_alive(self):
            return
        self.check_btn.configure(state="normal")
        text = integrity.describe(difference)
        if integrity.is_intact(difference):
            self.message_bar.success(text)
        else:
            self.message_bar.error(text)
        self._show_details(difference)

    def _failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.check_btn.configure(state="normal")
        if isinstance(exc, PortableOSAdminError):
            self.message_bar.error(str(exc))
        else:
            self.message_bar.error(f"Не удалось проверить носитель: {exc}")

    def _show_details(self, difference: "dict[str, list[str]]") -> None:
        """Поимённо, а не числом.

        Администратору, которому принесли носитель, важно не «десять
        отличий», а то, что среди них `boot:initramfs-*.img`.
        """
        lines = []
        for title, key in (
            ("Изменено", "changed"),
            ("Появилось лишнего", "added"),
            ("Пропало", "removed"),
        ):
            for path in difference[key]:
                lines.append(f"{title}: {path}")
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(lines) or "Расхождений нет.")
        self.details.configure(state="disabled")
