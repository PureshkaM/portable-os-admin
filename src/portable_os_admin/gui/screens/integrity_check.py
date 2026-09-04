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

import os
import tkinter as tk
from tkinter import ttk

from ...install import integrity
from .. import theme
from ..widgets import MessageBar, add_field, error_guard, section_card, section_header


class IntegrityFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app, media_id: str = ""):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self.media_id = media_id

        record = app.session.container.find_media(media_id) if media_id else None
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

        form = ttk.Frame(card)
        form.pack(anchor="w")
        self.esp_var = tk.StringVar()
        self.boot_var = tk.StringVar()
        # Пути, а не устройство: разделы монтирует администратор своими
        # средствами, и требовать здесь прав root ради того, чтобы посчитать
        # суммы, незачем.
        add_field(form, 0, "Раздел EFI смонтирован в:", self.esp_var, width=40)
        add_field(form, 1, "Раздел /boot смонтирован в:", self.boot_var, width=40)

        actions = ttk.Frame(card)
        actions.pack(fill="x", pady=theme.SPACE_SM)
        ttk.Button(actions, text="Проверить", command=self._check).pack(side="left")
        ttk.Button(actions, text="Назад", command=app.show_dashboard).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self.details = tk.Text(card, height=12, state="disabled")
        self.details.pack(fill="both", expand=True, pady=(theme.SPACE_SM, 0))

    def _check(self) -> None:
        esp = self.esp_var.get().strip()
        boot = self.boot_var.get().strip()
        if not esp or not boot:
            self.message_bar.error("Укажите, куда смонтированы оба раздела.")
            return
        for path in (esp, boot):
            if not os.path.isdir(path):
                self.message_bar.error(f"Каталог не найден: {path}")
                return

        expected = dict(self.record.boot_manifest or {})
        if not expected:
            self.message_bar.error(
                "У этого носителя нет описи: он записан версией программы, "
                "которая её ещё не снимала. Сравнивать не с чем."
            )
            return

        # Подпись проверяется до сравнения, а не после. Опись лежит в
        # зашифрованном контейнере, но проверить её всё равно стоит: файл
        # мог быть склеен из двух, и тогда сравнение шло бы с чужим эталоном.
        secret = self.app.session.container.admin_secret
        if not integrity.verify(expected, self.record.boot_manifest_signature, secret):
            self.message_bar.error(
                "Подпись описи не сходится: она составлена не этим контейнером."
            )
            return

        with error_guard(self.message_bar):
            actual = integrity.collect({"boot": boot, "esp": esp})

        difference = integrity.compare(expected, actual)
        text = integrity.describe(difference)
        if integrity.is_intact(difference):
            self.message_bar.success(text)
        else:
            self.message_bar.error(text)
        self._show_details(difference)

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
