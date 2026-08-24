"""UC-6 "Взаимодействие с разделом обмена" (administrator side).

The exchange volume is encrypted, so it cannot be mounted by hand any more:
it is opened with the key from the administrator container and only by this
program. The user of the portable system still sees it as an ordinary folder,
because their system opens the same volume with a key file kept on its own
encrypted root.

The volume is connected explicitly rather than per operation. Opening a LUKS
container runs the key-derivation function, which takes a noticeable moment;
doing that on every button press would make the screen feel broken. It is
closed on the way out, including when the window is destroyed, so a medium is
never left open by forgetting.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ...admin import ExchangeManager
from ...admin.service_volumes import ServiceVolumeError, open_exchange
from ...errors import ExchangeError, InsufficientSpaceError
from ..widgets import MessageBar, run_in_background, widget_alive


class ExchangeFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent)
        self.app = app
        self.manager = ExchangeManager()
        self._volume = None
        self._records = []

        ttk.Label(self, text="Раздел обмена", font=("TkDefaultFont", 13, "bold")).pack(
            anchor="w", padx=16, pady=(16, 8)
        )
        ttk.Label(
            self,
            text=(
                "Раздел зашифрован и открывается ключом из контейнера администратора. "
                "Пользователю переносной системы он виден как обычная папка."
            ),
            wraplength=760,
            justify="left",
        ).pack(anchor="w", padx=16)

        form = ttk.Frame(self)
        form.pack(anchor="w", padx=16, pady=(8, 0))
        ttk.Label(form, text="Носитель:").grid(row=0, column=0, sticky="w", pady=4)
        self.media_var = tk.StringVar()
        self.media_box = ttk.Combobox(form, textvariable=self.media_var, width=52, state="readonly")
        self.media_box.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)

        connect_row = ttk.Frame(self)
        connect_row.pack(anchor="w", padx=16, pady=(6, 4))
        self.connect_btn = ttk.Button(connect_row, text="Подключить носитель", command=self._connect)
        self.connect_btn.pack(side="left")
        self.disconnect_btn = ttk.Button(
            connect_row, text="Отключить", command=self._disconnect, state="disabled"
        )
        self.disconnect_btn.pack(side="left", padx=(8, 0))
        ttk.Button(connect_row, text="Обновить список файлов", command=self._refresh).pack(
            side="left", padx=(8, 0)
        )

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
        ttk.Button(back_row, text="Назад", command=self._go_back).pack(side="left")

        self.message_bar = MessageBar(self)
        self.message_bar.pack(fill="x", padx=16)

        self.bind("<Destroy>", self._on_destroy)
        self._refresh_media()

    # ------------------------------------------------------------------
    # Medium selection and connection
    # ------------------------------------------------------------------
    def _refresh_media(self) -> None:
        self._records = list(getattr(self.app.session.container, "media_records", []))
        self.media_box["values"] = [f"{r.media_id} — {r.label} ({r.config_name})" for r in self._records]
        if self._records and not self.media_var.get():
            self.media_box.current(0)
        if not self._records:
            self.message_bar.error(
                "В контейнере нет ни одного носителя. Раздел обмена доступен только "
                "на носителе, созданном этой программой."
            )

    def _selected_record(self):
        index = self.media_box.current()
        if index < 0 or index >= len(self._records):
            return None
        return self._records[index]

    def _connect(self) -> None:
        if self._volume is not None:
            self.message_bar.info("Носитель уже подключён.")
            return
        record = self._selected_record()
        if record is None:
            self.message_bar.error("Выберите носитель.")
            return
        self.connect_btn.configure(state="disabled")
        self.message_bar.info("Открывается зашифрованный раздел обмена…")
        run_in_background(
            self,
            lambda: open_exchange(record).__enter__(),
            on_success=self._connected,
            on_error=self._connect_failed,
        )

    def _connected(self, volume) -> None:
        if not widget_alive(self):
            # The screen went away while the volume was opening; leaving it
            # open would keep the medium busy with nothing to close it.
            volume.close()
            return
        self._volume = volume
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.message_bar.success("Раздел обмена подключён.")
        self._refresh()

    def _connect_failed(self, exc: BaseException) -> None:
        if not widget_alive(self):
            return
        self.connect_btn.configure(state="normal")
        self.message_bar.error(
            str(exc) if isinstance(exc, ServiceVolumeError) else f"Не удалось подключить раздел: {exc}"
        )

    def _disconnect(self) -> None:
        if self._volume is None:
            return
        self._volume.close()
        self._volume = None
        for item in self.files_table.get_children():
            self.files_table.delete(item)
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.message_bar.success("Раздел обмена закрыт, носитель можно извлечь.")

    def _go_back(self) -> None:
        self._disconnect()
        self.app.show_dashboard()

    def _on_destroy(self, event) -> None:
        if event.widget is self and self._volume is not None:
            self._volume.close()
            self._volume = None

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------
    def _mount_point(self) -> str | None:
        if self._volume is None:
            self.message_bar.error("Сначала подключите носитель.")
            return None
        return self._volume.mountpoint

    def _on_row_selected(self, _event) -> None:
        selection = self.files_table.selection()
        if not selection:
            return
        name = self.files_table.item(selection[0], "values")[0]
        self.selected_name_var.set(name)

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
            self._refresh()
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
            self._refresh()
        except (ExchangeError, OSError) as exc:
            self.message_bar.error(str(exc))
