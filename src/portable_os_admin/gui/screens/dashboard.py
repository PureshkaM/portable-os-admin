"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .. import theme
from ..widgets import MessageBar


class DashboardFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app

        # Bottom-up, so the actions and the message line keep their space and
        # the table gives up its own when the window is short.
        self.message_bar = MessageBar(self)
        self.message_bar.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(0, theme.SPACE_MD))

        # Ряд действий, и только действий.
        #
        # Разделы («Журналы», «Обмен», «Восстановление», «Обслуживание») ушли
        # в боковую навигацию: кнопка отвечает на вопрос «что сделать», раздел
        # -- на вопрос «где я», и в одном ряду они не отвечали ни на один. Тот
        # ряд к тому же дважды не помещался в окно.
        actions = ttk.Frame(self, style="Window.TFrame")
        actions.pack(side="bottom", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_MD, 0))

        # Установка -- то, ради чего программу открывают; одна акцентная
        # кнопка говорит об этом без единого слова пояснения.
        ttk.Button(
            actions, text="Установка ОС", style="Accent.TButton", command=self._open_install
        ).pack(side="left")
        # Порядок повторяет порядок работы: сначала собрать дистрибутив, потом
        # разворачивать его на носители -- по одному или тиражом.
        for text, command in (
            ("Собрать дистрибутив", self._open_build),
            ("Развернуть дистрибутив", self._open_deploy),
        ):
            ttk.Button(actions, text=text, command=command).pack(
                side="left", padx=(theme.SPACE_SM, 0)
            )

        header = ttk.Frame(self, style="Window.TFrame")
        header.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_LG, theme.SPACE_SM))
        # Заголовок раздела, а не программы: имя программы уже стоит над
        # списком разделов слева, и повторять его здесь значит занимать
        # строку тем, что читатель только что прочитал.
        ttk.Label(
            header,
            text="Носители",
            style="Window.TLabel",
            font=theme.FONT_TITLE,
        ).pack(anchor="w")
        ttk.Label(
            header,
            # Двойной щелчок сам себя не объясняет, и это тот случай, когда
            # подпись несёт новое, а не пересказывает очевидное.
            text=(
                "Носители, зарегистрированные в этом контейнере администратора. "
                "Двойной щелчок — проверка загрузчика и initramfs."
            ),
            style="Window.TLabel",
            foreground=theme.TEXT_MUTED,
            # Перенос, а не обрезание: без него строка уходила за правый край
            # окна и обрывалась на полуслове.
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(theme.SPACE_XS, 0))

        card = ttk.Frame(self, style="Card.TFrame", padding=theme.SPACE_MD)
        card.pack(side="top", fill="both", expand=True, padx=theme.SPACE_LG)

        columns = ("media_id", "label", "os", "config", "created")
        headings = {
            "media_id": "Идентификатор",
            "label": "Метка",
            "os": "ОС",
            "config": "Конфигурация",
            "created": "Создан",
        }
        # Сумма ширин влезает в область содержимого при окне минимальной
        # ширины: до 02.09.2026 она была больше, и колонка «Создан» уходила за
        # край -- дата создания носителя не читалась вовсе.
        widths = {"media_id": 150, "label": 130, "os": 80, "config": 150, "created": 180}
        self.table = ttk.Treeview(card, columns=columns, show="headings", height=10)
        for col in columns:
            self.table.heading(col, text=headings[col])
            self.table.column(col, width=widths[col], anchor="w")
        self.table.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(card, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        # Проверка целостности относится к одному носителю, а не к контейнеру
        # вообще, поэтому она открывается со строки, а не кнопкой: седьмая
        # кнопка в ряд уже не помещается, и это ловит тест ширины.
        self.table.bind("<Double-1>", self._open_selected_integrity)

        # Действия над конкретным носителем стоят под таблицей, а не в общем
        # ряду: они относятся к выбранной строке, и место рядом с ней говорит
        # об этом лучше любой подписи.
        row_actions = ttk.Frame(self, style="Window.TFrame")
        row_actions.pack(side="top", fill="x", padx=theme.SPACE_LG, pady=(theme.SPACE_SM, 0))
        ttk.Button(
            row_actions, text="Проверить целостность", command=self._open_selected_integrity
        ).pack(side="left")
        ttk.Button(
            row_actions, text="Журналы носителя", command=self._open_selected_logs
        ).pack(side="left", padx=(theme.SPACE_SM, 0))

        self._refresh_table()
        # Said plainly rather than left as an empty grid, which reads as a
        # screen that failed to load rather than as a container with nothing in
        # it yet.
        if not self.app.session.container.data.media_records:
            self.message_bar.info(
                "В контейнере пока нет носителей. Начните с «Установка ОС»."
            )

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        for record in self.app.session.container.data.media_records:
            self.table.insert(
                "", "end", values=(record.media_id, record.label, record.target_os, record.config_name, record.created_at)
            )

    def _open_selected_integrity(self, _event=None) -> None:
        from .integrity_check import IntegrityFrame

        selection = self.table.selection()
        if not selection:
            return
        media_id = self.table.item(selection[0], "values")[0]
        self.app.show_frame(IntegrityFrame, media_id=media_id)

    def _open_selected_logs(self) -> None:
        """Журналы того носителя, который выбран в таблице.

        Раздел «Журналы» спрашивает носитель сам; отсюда ответ уже есть, и
        спрашивать второй раз незачем.
        """
        selection = self.table.selection()
        media_id = self.table.item(selection[0], "values")[0] if selection else ""
        self.app.show_section("logs", media_id=media_id)

    def _open_build(self) -> None:
        from .build_wizard import BuildWizardFrame

        self.app.show_frame(BuildWizardFrame)

    def _open_deploy(self) -> None:
        from .deploy_wizard import DeployWizardFrame

        self.app.show_frame(DeployWizardFrame)

    def _open_install(self) -> None:
        from .install_wizard import InstallWizardFrame

        self.app.show_frame(InstallWizardFrame)

