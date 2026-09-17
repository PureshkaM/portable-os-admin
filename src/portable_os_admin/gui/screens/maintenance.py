"""Обслуживание: пароль контейнера, следы прерванных установок, ресурс отзыва.

Всё это делают редко и по случаю, а не в ходе работы. Раньше эти действия
стояли отдельными кнопками в общем ряду главного экрана, вперемешку с
разделами и с тем, ради чего программу открывают, -- и именно этот ряд
однажды не поместился в окно.

Здесь они вместе не потому, что похожи, а потому что одинаково относятся к
работе: пока всё идёт как надо, сюда не заходят.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ...install import leftovers
from .. import theme
from ..widgets import (
    MessageBar,
    add_field,
    run_in_background,
    section_card,
    section_header,
)


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

        # Вкладки, а не прокрутка.
        #
        # Здесь три разнородные вещи -- пароль контейнера, следы прерванных
        # установок и ресурс отзыва, -- и общего у них только одно: пока всё
        # идёт как надо, сюда не заходят. Сложенные в столбик, они не
        # помещались в окно, и прокрутка была заплаткой: третий блок всё равно
        # оказывался обрезан (снимок 17.09.2026). Каждая вкладка помещается
        # целиком, и читать на ней надо одну вещь, а не искать её среди трёх.
        outer = section_card(self, grow=True)
        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        password_page = ttk.Frame(notebook, style="Card.TFrame", padding=theme.SPACE_MD)
        notebook.add(password_page, text="Пароль контейнера")
        ttk.Label(
            password_page,
            text="Контейнер хранит ключи всех выданных носителей: сменить пароль стоит "
            "тогда же, когда меняют любой другой ключ от хранилища.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))
        ttk.Button(password_page, text="Сменить пароль…", command=self._open_password).pack(
            anchor="w"
        )

        leftovers_page = ttk.Frame(notebook, style="Card.TFrame", padding=theme.SPACE_MD)
        notebook.add(leftovers_page, text="Следы установок")
        ttk.Label(
            leftovers_page,
            # Подпись объясняет не кнопку, а то, чего без неё не увидеть:
            # прерванная установка оставляет систему в состоянии, которое
            # снаружи ничем себя не выдаёт.
            text="Установка, которую прервали, оставляет носитель смонтированным, его "
            "том открытым, а процесс сборки — работающим. Сама программа об этом "
            "потом не узнает, поэтому смотреть сюда.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))
        ttk.Button(
            leftovers_page, text="Проверить состояние", command=self._check_leftovers
        ).pack(anchor="w")

        revocation_page = ttk.Frame(notebook, style="Card.TFrame", padding=theme.SPACE_MD)
        notebook.add(revocation_page, text="Ресурс отзыва")
        self.revocation_state = ttk.Label(
            revocation_page, text="", style="Card.TLabel", wraplength=theme.TEXT_WRAP
        )
        self.revocation_state.pack(anchor="w", pady=(0, theme.SPACE_SM))

        form = ttk.Frame(revocation_page, style="Card.TFrame")
        form.pack(anchor="w", fill="x")
        self.revocation_url = tk.StringVar(
            value=self.app.session.container.data.revocation_server_url
        )
        self.revocation_code = tk.StringVar()
        _, row = add_field(form, 0, "Адрес", self.revocation_url)
        add_field(
            form,
            row,
            "Код знакомства",
            self.revocation_code,
            hint="Печатается при установке сервера и годится один раз. "
            "Уже знакомому ресурсу код не нужен — поле остаётся пустым.",
        )
        ttk.Button(
            revocation_page, text="Подключиться", command=self._connect_revocation
        ).pack(anchor="w", pady=(theme.SPACE_SM, 0))
        self._show_revocation_state()

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

    def _show_revocation_state(self) -> None:
        data = self.app.session.container.data
        if data.revocation_public_key_pem.strip():
            self.revocation_state.config(text=f"Подключён: {data.revocation_server_url}")
        else:
            self.revocation_state.config(
                text="Не подключён. Носители, выпущенные без ресурса отзыва, "
                "нельзя отозвать удалённо: остаётся только забрать флешку."
            )

    def _connect_revocation(self) -> None:
        """Знакомство с ресурсом отзыва: забрать его ключ и отдать свой.

        Ключ сервера запоминается в контейнере и уезжает на каждый выпускаемый
        носитель. Адрес можно спросить заново в любой момент, а ключ должен
        быть один и тот же у всех носителей организации -- поэтому он здесь, а
        не в конфигурации установки.
        """
        from ...admin import revocation_client

        url = self.revocation_url.get().strip()
        code = self.revocation_code.get().strip()
        if not url.lower().startswith("https://"):
            self.message_bar.error(
                "Адрес должен начинаться с https://. Подпись защищает ответ от "
                "подмены, но по открытому каналу видно, какие носители у организации "
                "есть и когда они включаются."
            )
            return

        container = self.app.session.container
        # Семя ключа полномочий заводится здесь же, если контейнер создан до
        # появления отзыва; сохраняется вместе с остальным ниже.
        seed = container.admin_authority_secret

        def work():
            pem = revocation_client.fetch_public_key(url)
            return pem, revocation_client.enroll(url, seed, code) if code else ""

        def done(result) -> None:
            pem, said = result
            known = container.data.revocation_public_key_pem.strip()
            if known and known != pem.strip():
                # Смена ключа -- это не «переподключение», а другой сервер: все
                # выпущенные носители доверяют прежнему и ответы нового не
                # примут. Молча перезаписать значило бы тихо выключить отзыв.
                self.message_bar.error(
                    "Этот ресурс подписывает не тем ключом, что записан в контейнере. "
                    "Выпущенные носители ответов нового ключа не примут — сначала "
                    "убедитесь, что это тот самый сервер."
                )
                return
            container.data.revocation_server_url = url
            container.data.revocation_public_key_pem = pem
            self.app.save_container()
            self.revocation_code.set("")
            self._show_revocation_state()
            self.message_bar.success(said or f"Ресурс отзыва отвечает: {url}")

        self.message_bar.info("Соединяюсь с ресурсом отзыва…")
        run_in_background(
            self, work, done, lambda exc: self.message_bar.error(str(exc))
        )
