"""Main window after a session is open ("Интерфейсы пользователя-2")."""
from __future__ import annotations

import tkinter as tk
from tkinter import simpledialog, ttk

from ...admin import media_detect
from .. import theme
from ..widgets import MessageBar, section_card, section_header


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

        # Заголовок раздела, а не программы: имя программы уже стоит над
        # списком разделов слева, и повторять его здесь значит занимать
        # строку тем, что читатель только что прочитал.
        #
        # Двойной щелчок сам себя не объясняет, и это тот случай, когда
        # подпись несёт новое, а не пересказывает очевидное.
        section_header(
            self,
            "Носители",
            "Носители, зарегистрированные в этом контейнере администратора. "
            "Двойной щелчок — проверка загрузчика и initramfs.",
        )

        card = section_card(self)

        # Первым -- имя, которое дал администратор, а не идентификатор.
        #
        # `46aed677` отвечает на вопрос «тот ли это носитель», но не на вопрос
        # «чей он», а список из десяти строк читают именно ради второго.
        # Столбец «Подключён» заполняется сам: носитель, воткнутый в порт,
        # узнаётся по UUID своих томов, без пароля и без монтирования.
        columns = ("name", "connected", "media_id", "os", "config", "created")
        headings = {
            "name": "Носитель",
            "connected": "Подключён",
            "media_id": "Идентификатор",
            "os": "ОС",
            "config": "Конфигурация",
            "created": "Создан",
        }
        # Сумма ширин влезает в область содержимого при окне минимальной
        # ширины: до 02.09.2026 она была больше, и колонка «Создан» уходила за
        # край -- дата создания носителя не читалась вовсе.
        # Ширина проверяется по заголовку, а не по значению: идентификатор --
        # восемь знаков и в сотню пикселей помещается, а слово
        # «Идентификатор» над ним обрезалось до «Идентификат».
        widths = {
            "name": 170,
            "connected": 110,
            "media_id": 130,
            "os": 70,
            "config": 130,
            "created": 150,
        }
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
        ttk.Button(
            row_actions, text="Переименовать", command=self._rename_selected
        ).pack(side="left", padx=(theme.SPACE_SM, 0))

        self._poll_job = None
        self._poll_connected()
        # Опрос прекращается вместе с экраном: `after` переживает виджет и
        # продолжал бы дёргать уничтоженную таблицу.
        self.bind("<Destroy>", self._stop_polling)
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
        records = self.app.session.container.data.media_records
        connected = media_detect.detect_map(records)
        for record in records:
            found = connected.get(record.media_id)
            self.table.insert(
                "",
                "end",
                values=(
                    record.display_name,
                    found.device_path if found else "",
                    record.media_id,
                    record.target_os,
                    record.config_name,
                    record.created_at,
                ),
            )

    def _poll_connected(self) -> None:
        """Опрашивает порты, пока экран открыт.

        Носитель втыкают уже после того, как список открыт, -- и если не
        опрашивать, администратору придётся догадаться нажать «обновить».
        Пять секунд -- достаточно редко, чтобы не мешать, и достаточно часто,
        чтобы флешка «появилась» пока рука ещё на ней.
        """
        if not self.winfo_exists():
            return
        try:
            self._refresh_table()
        finally:
            self._poll_job = self.after(5000, self._poll_connected)

    def _rename_selected(self) -> None:
        """Имя носителя живёт в контейнере, а не на флешке.

        Поэтому переименование ничего не пишет на носитель: его можно дать
        уже выданному, потерянному и даже сломанному -- лишь бы запись о нём
        была здесь.
        """
        selection = self.table.selection()
        if not selection:
            self.message_bar.error("Выберите носитель в списке.")
            return
        media_id = self.table.item(selection[0], "values")[2]
        record = next(
            (r for r in self.app.session.container.data.media_records if r.media_id == media_id),
            None,
        )
        if record is None:
            self.message_bar.error("Запись о носителе не найдена.")
            return
        chosen = simpledialog.askstring(
            "Имя носителя",
            "Как называть этот носитель? Например: «Иванов, бухгалтерия».\n"
            "Имя хранится в контейнере администратора и на носитель не пишется.",
            initialvalue=record.name or record.label,
            parent=self,
        )
        if chosen is None:
            return
        record.name = chosen.strip()
        self.app.save_container()
        self._refresh_table()
        self.message_bar.success(
            f"Носитель {media_id} теперь называется «{record.display_name}»."
        )

    def _stop_polling(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        job = getattr(self, "_poll_job", None)
        if job is not None:
            try:
                self.after_cancel(job)
            except Exception:  # noqa: BLE001 -- окно уже уничтожено
                pass
            self._poll_job = None

    def _open_selected_integrity(self, _event=None) -> None:
        from .integrity_check import IntegrityFrame

        selection = self.table.selection()
        if not selection:
            return
        media_id = self.table.item(selection[0], "values")[2]
        self.app.show_frame(IntegrityFrame, media_id=media_id)

    def _open_selected_logs(self) -> None:
        """Журналы того носителя, который выбран в таблице.

        Раздел «Журналы» спрашивает носитель сам; отсюда ответ уже есть, и
        спрашивать второй раз незачем.
        """
        selection = self.table.selection()
        media_id = self.table.item(selection[0], "values")[2] if selection else ""
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

