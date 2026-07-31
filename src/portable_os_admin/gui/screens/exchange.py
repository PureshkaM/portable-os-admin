"""UC-6 "Взаимодействие с разделом обмена" (administrator side)."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ...admin import ExchangeManager, find_media_by_boot_metadata
from ...errors import ExchangeError, InsufficientSpaceError, MediaNotRegisteredError
from ..widgets import MessageBar, add_field


class ExchangeFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app
        self.manager = ExchangeManager()

        ttk.Label(self, text="Раздел обмена", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16)
        self.boot_mount_var = tk.StringVar()
        _, row = add_field(
            form, 0, "Точка монтирования загрузочного раздела:", self.boot_mount_var,
            hint="необязательно -- для сопоставления носителя", width=40,
        )
        self.exchange_mount_var = tk.StringVar()
        add_field(form, row, "Точка монтирования раздела обмена:", self.exchange_mount_var, width=40)

        ttk.Button(self, text="Обновить список файлов", command=self._refresh).pack(anchor="w", padx=16, pady=(4, 8))

        self.files_table = ttk.Treeview(self, columns=("name", "size"), show="headings", height=8)
        self.files_table.heading("name", text="файл")
        self.files_table.heading("size", text="размер (байт)")
        self.files_table.pack(fill="both", expand=True, padx=16)
        self.files_table.bind("<<TreeviewSelect>>", self._on_row_selected)

        write_row = ttk.Frame(self)
        write_row.pack(fill="x", padx=16, pady=(10, 4))
        self.write_path_var = tk.StringVar()
        ttk.Label(write_row, text="Файл для записи:").pack(side="left")
        ttk.Entry(write_row, textvariable=self.write_path_var, width=44).pack(side="left", padx=(6, 6))
        ttk.Button(write_row, text="Записать", command=self._write).pack(side="left")

        read_row = ttk.Frame(self)
        read_row.pack(fill="x", padx=16, pady=(0, 4))
        self.selected_name_var = tk.StringVar()
        ttk.Label(read_row, text="Выбранный файл:").pack(side="left")
        ttk.Entry(read_row, textvariable=self.selected_name_var, width=24, state="readonly").pack(
            side="left", padx=(6, 6)
        )
        self.read_dest_var = tk.StringVar()
        ttk.Label(read_row, text="Куда скопировать:").pack(side="left")
        ttk.Entry(read_row, textvariable=self.read_dest_var, width=30).pack(side="left", padx=(6, 6))
        ttk.Button(read_row, text="Прочитать", command=self._read).pack(side="left")
        ttk.Button(read_row, text="Удалить", command=self._delete).pack(side="left", padx=(6, 0))

        back_row = ttk.Frame(self)
        back_row.pack(anchor="w", padx=16, pady=(4, 8))
        ttk.Button(back_row, text="Назад", command=self.app.show_dashboard).pack(side="left")

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

    def _on_row_selected(self, _event) -> None:
        selection = self.files_table.selection()
        if not selection:
            return
        name = self.files_table.item(selection[0], "values")[0]
        self.selected_name_var.set(name)

    def _mount_point(self) -> str | None:
        boot_mount = self.boot_mount_var.get().strip()
        exchange_mount = self.exchange_mount_var.get().strip()
        if boot_mount:
            try:
                record = find_media_by_boot_metadata(self.app.session.container, boot_mount)
                self.message_bar.success(f"Носитель опознан: {record.media_id} ({record.label})")
            except MediaNotRegisteredError as exc:
                self.message_bar.error(str(exc))
                return None
        if not exchange_mount:
            self.message_bar.error("Укажите точку монтирования раздела обмена.")
            return None
        return exchange_mount

    def _refresh(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        for item in self.files_table.get_children():
            self.files_table.delete(item)
        try:
            for info in self.manager.list_files(mount_point):
                self.files_table.insert("", "end", values=(info.name, info.size_bytes))
        except ExchangeError as exc:
            self.message_bar.error(str(exc))

    def _write(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        source = self.write_path_var.get().strip()
        if not source:
            self.message_bar.error("Укажите путь к файлу для записи.")
            return
        try:
            self.manager.write_file(mount_point, Path(source))
            self.message_bar.success(f"Файл {source!r} записан в раздел обмена.")
        except (ExchangeError, InsufficientSpaceError, OSError) as exc:
            self.message_bar.error(str(exc))

    def _read(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        name = self.selected_name_var.get().strip()
        dest = self.read_dest_var.get().strip()
        if not name or not dest:
            self.message_bar.error("Выберите файл в списке и укажите путь назначения.")
            return
        try:
            self.manager.read_file(mount_point, name, Path(dest))
            self.message_bar.success(f"Файл {name!r} скопирован в {dest!r}.")
        except (ExchangeError, OSError) as exc:
            self.message_bar.error(str(exc))

    def _delete(self) -> None:
        mount_point = self._mount_point()
        if mount_point is None:
            return
        name = self.selected_name_var.get().strip()
        if not name:
            self.message_bar.error("Выберите файл для удаления.")
            return
        try:
            self.manager.delete_file(mount_point, name)
            self.message_bar.success(f"Файл {name!r} удалён из раздела обмена.")
        except (ExchangeError, OSError) as exc:
            self.message_bar.error(str(exc))
