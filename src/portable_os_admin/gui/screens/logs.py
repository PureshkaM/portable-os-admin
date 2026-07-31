"""UC-5 "Просмотр журналов операционной системы".

Operates on an already-mounted log-partition directory (the admin mounts
POA_LOGS beforehand); see docs/ARCHITECTURE.md for why this doesn't drive
real mount/umount itself.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ...admin import LogViewer, find_media_by_boot_metadata
from ...errors import LogsCorruptedError, LogsUnavailableError, MediaNotRegisteredError
from ..widgets import MessageBar, add_field


class LogsFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app

        ttk.Label(self, text="Просмотр журналов операционной системы", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16)
        self.boot_mount_var = tk.StringVar()
        _, row = add_field(
            form, 0, "Точка монтирования загрузочного раздела:", self.boot_mount_var,
            hint="необязательно -- для сопоставления носителя", width=40,
        )
        self.logs_mount_var = tk.StringVar()
        _, row = add_field(form, row, "Точка монтирования раздела логов:", self.logs_mount_var, width=40)
        self.filter_var = tk.StringVar()
        add_field(form, row, "Фильтр (необязательно):", self.filter_var, width=40)

        actions = ttk.Frame(self)
        actions.pack(anchor="w", padx=16, pady=8)
        ttk.Button(actions, text="Загрузить журналы", command=self._load).pack(side="left")
        ttk.Button(actions, text="Назад", command=self.app.show_dashboard).pack(side="left", padx=(8, 0))

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

        self.log_view = tk.Text(self, height=20, state="disabled")
        self.log_view.pack(fill="both", expand=True, padx=16, pady=(4, 16))

    def _load(self) -> None:
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")

        boot_mount = self.boot_mount_var.get().strip()
        logs_mount = self.logs_mount_var.get().strip()
        filter_text = self.filter_var.get().strip() or None

        if boot_mount:
            try:
                record = find_media_by_boot_metadata(self.app.session.container, boot_mount)
            except MediaNotRegisteredError as exc:
                self.message_bar.error(str(exc))
                return
            self.message_bar.success(f"Носитель опознан: {record.media_id} ({record.label})")

        if not logs_mount:
            self.message_bar.error("Укажите точку монтирования раздела логов.")
            return

        try:
            lines = LogViewer().read_logs(logs_mount, filter_substring=filter_text)
        except (LogsUnavailableError, LogsCorruptedError) as exc:
            self.message_bar.error(str(exc))
            return

        self.log_view.configure(state="normal")
        for line in lines:
            self.log_view.insert("end", line + "\n")
        self.log_view.configure(state="disabled")
