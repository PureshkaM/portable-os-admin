"""UC-5 "Просмотр журналов операционной системы".

The log volume is encrypted and can only be opened with the key kept in the
administrator container, so this screen no longer asks for a mount point the
administrator prepared by hand -- there is no way to prepare one. It picks a
registered medium, opens that medium's log volume read-only for as long as it
takes to read it, and closes it again.

Read-only is enforced here as well as in the mount options: the specification
requires that the logs cannot be altered from the portable system, and an
administrator tool that opened them writable would be the obvious way around
that requirement.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...admin import LogViewer
from ...admin.service_volumes import ServiceVolumeError, open_logs
from ...errors import LogsCorruptedError, LogsUnavailableError
from .. import theme
from ..widgets import (
    MessageBar,
    add_field,
    button_row,
    run_in_background,
    section_card,
    section_header,
    widget_alive,
)


class LogsFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app, media_id: str = ""):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        #: Носитель, выбранный до входа сюда -- со строки списка носителей.
        #: Спрашивать второй раз то, на что ответ уже дан, значит заставлять
        #: администратора искать в выпадающем списке строку, которую он только
        #: что выделил.
        self._preselected = media_id

        section_header(
            self,
            "Журналы",
            "Открывается ключом из контейнера; пароль пользователя не нужен.",
        )

        card = section_card(self)

        form = ttk.Frame(card)
        form.pack(anchor="w")
        ttk.Label(form, text="Носитель:").grid(row=0, column=0, sticky="w", pady=theme.SPACE_XS)
        self.media_var = tk.StringVar()
        self.media_box = ttk.Combobox(form, textvariable=self.media_var, width=52, state="readonly")
        self.media_box.grid(row=0, column=1, sticky="w", padx=(theme.SPACE_SM, 0), pady=theme.SPACE_XS)
        self.filter_var = tk.StringVar()
        add_field(form, 1, "Фильтр (необязательно):", self.filter_var, width=40)

        # Три кнопки в строку помещаются не в любом окне: при наименьшем
        # (900x600) «Сохранить в файл…» не хватало восьми пикселей, и подпись
        # не рисовалась вовсе. Ряд переносит не поместившееся на свою строку.
        _actions, buttons = button_row(
            card,
            (
                ("Загрузить журналы", self._load),
                ("Обновить список носителей", self._refresh_media),
                ("Сохранить в файл…", self._save_to_file),
            ),
            pady=theme.SPACE_SM,
        )
        self.load_btn = buttons[0]

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self.log_view = tk.Text(card, height=20, state="disabled")
        self.log_view.pack(fill="both", expand=True, pady=(theme.SPACE_XS, 0))

        self._records = []
        self._refresh_media()
        if self._preselected:
            for index, record in enumerate(self._records):
                if record.media_id == self._preselected:
                    self.media_box.current(index)
                    break

    def _save_to_file(self) -> None:
        """Выгружает показанное в файл.

        Журнал нужен не только на экране: его прикладывают к заявке, шлют
        коллеге, хранят до разбора. Пересказывать его словами или снимать с
        экрана -- худший из способов.
        """
        from tkinter import filedialog

        lines = self.log_view.get("1.0", "end").strip()
        if not lines:
            self.message_bar.error("Нечего сохранять: журнал не загружен.")
            return
        record = self._selected_record()
        suggested = f"journal-{record.media_id}.txt" if record else "journal.txt"
        chosen = filedialog.asksaveasfilename(
            parent=self,
            title="Сохранить журнал",
            initialfile=suggested,
            defaultextension=".txt",
        )
        if not chosen:
            return
        try:
            with open(chosen, "w", encoding="utf-8") as handle:
                handle.write(lines + "\n")
        except OSError as exc:
            self.message_bar.error(f"Не удалось сохранить: {exc}")
            return
        self.message_bar.success(f"Журнал сохранён: {chosen}")

    def _refresh_media(self) -> None:
        # `container.data.media_records`, а не `container.media_records`.
        #
        # Найдено 02.09.2026 сразу после того, как разделы переехали в боковую
        # навигацию: экран журналов сообщал «в контейнере нет ни одного
        # носителя», когда их было восемь. Записи лежат в `data`, а `getattr`
        # с умолчанием превращал промах в пустой список -- ошибка выглядела
        # как честный ответ, и заметить её можно было только зная, что
        # носители есть.
        self._records = list(self.app.session.container.data.media_records)
        self.media_box["values"] = [f"{r.media_id} — {r.label} ({r.config_name})" for r in self._records]
        if self._records and not self.media_var.get():
            self.media_box.current(0)
        if not self._records:
            self.message_bar.error(
                "В контейнере нет ни одного носителя. Журналы читаются только "
                "с носителя, созданного этой программой."
            )

    def _selected_record(self):
        index = self.media_box.current()
        if index < 0 or index >= len(self._records):
            return None
        return self._records[index]

    def _load(self) -> None:
        record = self._selected_record()
        if record is None:
            self.message_bar.error("Выберите носитель.")
            return

        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")
        filter_text = self.filter_var.get().strip() or None

        self.load_btn.configure(state="disabled")
        self.message_bar.info(f"Открывается том журналов носителя {record.media_id}…")

        # Opening a LUKS volume and reading it are both blocking, and the
        # medium may be a slow stick. On the UI thread that freezes the window
        # for as long as it takes.
        run_in_background(
            self,
            lambda: self._read(record, filter_text),
            on_success=self._show,
            on_error=self._failed,
        )

    @staticmethod
    def _read(record, filter_text):
        with open_logs(record) as volume:
            return LogViewer().read_logs(volume.mountpoint, filter_substring=filter_text)

    def _show(self, lines) -> None:
        if not widget_alive(self):
            return
        self.load_btn.configure(state="normal")
        self.log_view.configure(state="normal")
        for line in lines:
            self.log_view.insert("end", line + "\n")
        self.log_view.configure(state="disabled")
        self.message_bar.success(f"Прочитано строк: {len(lines)}. Том закрыт.")

    def _failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.load_btn.configure(state="normal")
        if isinstance(exc, (ServiceVolumeError, LogsUnavailableError, LogsCorruptedError)):
            self.message_bar.error(str(exc))
        else:
            self.message_bar.error(f"Не удалось прочитать журналы: {exc}")
