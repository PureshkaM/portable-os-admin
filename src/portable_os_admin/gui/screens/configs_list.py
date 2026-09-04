"""UC-3 "Управление файлом конфигурации установки" -- list/manage screen.

The wizard (config_wizard.py) is where a config is actually authored; this
screen is the hub you land on before/after that: pick one to edit, validate,
delete, browse its repository packages, or jump straight to installing it.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ...config.validator import ConfigValidationUnavailableError
from .. import theme
from ..widgets import MessageBar, error_guard, section_card, section_header


class ConfigsListFrame(ttk.Frame):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, style="Window.TFrame")
        self.app = app
        self._paths: list[Path] = []

        section_header(
            self,
            "Конфигурации",
            "Из чего собирается система. Собирать можно только из проверенной.",
        )

        card = section_card(self)

        columns = ("file", "target_os", "release", "status")
        headings = {"file": "файл", "target_os": "цель", "release": "релиз", "status": "статус проверки"}
        self.table = ttk.Treeview(card, columns=columns, show="headings", height=12)
        for col in columns:
            self.table.heading(col, text=headings[col])
            self.table.column(col, width=160, anchor="w")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<Double-1>", lambda _event: self._edit_selected())

        create_row = ttk.Frame(card)
        create_row.pack(fill="x", pady=(theme.SPACE_SM, theme.SPACE_XS))
        ttk.Button(create_row, text="Создать новую конфигурацию", command=self._create_new).pack(side="left")

        action_row = ttk.Frame(card)
        action_row.pack(fill="x", pady=(0, theme.SPACE_SM))
        ttk.Button(action_row, text="Редактировать", command=self._edit_selected).pack(side="left")
        ttk.Button(action_row, text="Проверить", command=self._validate_selected).pack(side="left", padx=(theme.SPACE_SM, 0))
        ttk.Button(action_row, text="Установить…", command=self._install_selected).pack(side="left", padx=(theme.SPACE_SM, 0))
        ttk.Button(action_row, text="Удалить", command=self._delete_selected).pack(side="left", padx=(theme.SPACE_SM, 0))
        ttk.Button(action_row, text="Назад", command=self.app.show_dashboard).pack(side="left", padx=(theme.SPACE_SM, 0))

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self._refresh_table()

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        self._paths = self.app.config_manager.list_configs(self.app.configs_dir)
        for path in self._paths:
            config = self.app.config_manager.open(path)
            self.table.insert("", "end", values=(path.name, config.target_os, config.release, config.validation_status))

    def _selected_path(self) -> Path | None:
        selection = self.table.selection()
        if not selection:
            self.message_bar.error("Выберите конфигурацию в таблице.")
            return None
        index = self.table.index(selection[0])
        return self._paths[index]

    def _create_new(self) -> None:
        from .config_wizard import ConfigWizardFrame

        self.app.show_frame(ConfigWizardFrame)

    def _edit_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        from .config_wizard import ConfigWizardFrame

        self.app.show_frame(ConfigWizardFrame, config_path=path)

    def _validate_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        config = self.app.config_manager.open(path)
        try:
            self.app.config_manager.validate(config)
        except ConfigValidationUnavailableError as exc:
            self.message_bar.error(f"Проверка недоступна: {exc}")
            return
        self.app.config_manager.save(config, path)
        if config.validation_status == "valid":
            self.message_bar.success(f"«{path.name}»: конфигурация корректна.")
        else:
            self.message_bar.error(f"«{path.name}»: " + "; ".join(config.validation_errors))
        self._refresh_table()

    def _install_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        config = self.app.config_manager.open(path)
        if config.validation_status != "valid":
            self.message_bar.error(
                f"«{path.name}» ещё не прошла проверку (статус: {config.validation_status}). "
                "Нажмите «Проверить» перед установкой."
            )
            return
        from .install_wizard import InstallWizardFrame

        self.app.show_frame(InstallWizardFrame, config_path=path)

    def _delete_selected(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        with error_guard(self.message_bar):
            path.unlink()
            self.message_bar.success(f"«{path.name}» удалена.")
        self._refresh_table()
