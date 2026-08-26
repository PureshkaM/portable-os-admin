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
from ..widgets import MessageBar, add_field, run_in_background, widget_alive


class LogsFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Просмотр журналов операционной системы", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        ttk.Label(
            self,
            text="Открывается ключом из контейнера; пароль пользователя не нужен.",
            wraplength=760,
            justify="left",
        ).pack(anchor="w", padx=16)

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(form, text="Носитель:").grid(row=0, column=0, sticky="w", pady=4)
        self.media_var = tk.StringVar()
        self.media_box = ttk.Combobox(form, textvariable=self.media_var, width=52, state="readonly")
        self.media_box.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)
        self.filter_var = tk.StringVar()
        add_field(form, 1, "Фильтр (необязательно):", self.filter_var, width=40)

        actions = ttk.Frame(self)
        actions.pack(anchor="w", padx=16, pady=8)
        self.load_btn = ttk.Button(actions, text="Загрузить журналы", command=self._load)
        self.load_btn.pack(side="left")
        ttk.Button(actions, text="Обновить список носителей", command=self._refresh_media).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="Назад", command=self.app.show_dashboard).pack(side="left", padx=(8, 0))

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

        self.log_view = tk.Text(self, height=20, state="disabled")
        self.log_view.pack(fill="both", expand=True, padx=16, pady=(4, 16))

        self._records = []
        self._refresh_media()

    def _refresh_media(self) -> None:
        self._records = list(getattr(self.app.session.container, "media_records", []))
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
