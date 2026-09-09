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
from ..widgets import (
    Column,
    MessageBar,
    button_row,
    data_table,
    error_guard,
    section_card,
    section_header,
)


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

        # Имя файла тянется за счёт свободного места: оно длиннее остальных
        # колонок и единственное, чего в узком окне жаль.
        self.table, self.placeholder = data_table(
            card,
            (
                Column("file", "файл", 220, 140, stretch=True),
                Column("target_os", "цель", 120, 90),
                Column("release", "релиз", 120, 90),
                Column("status", "статус проверки", 180, 140),
            ),
            height=12,
            empty_text="Конфигураций пока нет.\nНажмите «Создать новую конфигурацию».",
        )
        self.table.bind("<Double-1>", lambda _event: self._edit_selected())

        create_row = ttk.Frame(card)
        create_row.pack(fill="x", pady=(theme.SPACE_SM, theme.SPACE_XS))
        ttk.Button(create_row, text="Создать новую конфигурацию", command=self._create_new).pack(side="left")

        # Пять кнопок в строку помещаются не в любом окне: при наименьшем
        # (900x600) «Назад» оставалось 37 пикселей от нужных 126, то есть её
        # не было видно. Ряд переносит не поместившееся на следующую строку.
        button_row(
            card,
            (
                ("Редактировать", self._edit_selected),
                ("Проверить", self._validate_selected),
                ("Установить…", self._install_selected),
                ("Удалить", self._delete_selected),
                ("Назад", self.app.show_dashboard),
            ),
            pady=(0, theme.SPACE_SM),
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self._refresh_table()

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        # Список берётся из контейнера -- там же, где носители и записи
        # восстановления. Файлов рядом с ним больше нет.
        self._names = self.app.config_manager.list_configs(self.app.container)
        self.placeholder.update(not self._names)
        for name in self._names:
            config = self.app.config_manager.open(self.app.container, name)
            self.table.insert(
                "", "end",
                values=(name, config.target_os, config.release, config.validation_status),
            )

    def _selected_name(self) -> "str | None":
        selection = self.table.selection()
        if not selection:
            self.message_bar.error("Выберите конфигурацию в таблице.")
            return None
        return self._names[self.table.index(selection[0])]

    def _create_new(self) -> None:
        from .config_wizard import ConfigWizardFrame

        self.app.show_frame(ConfigWizardFrame)

    def _edit_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        from .config_wizard import ConfigWizardFrame

        self.app.show_frame(ConfigWizardFrame, config_name=name)

    def _validate_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        config = self.app.config_manager.open(self.app.container, name)
        try:
            self.app.config_manager.validate(config)
        except ConfigValidationUnavailableError as exc:
            self.message_bar.error(f"Проверка недоступна: {exc}")
            return
        self.app.config_manager.save(self.app.container, config)
        self.app.save_container()
        if config.validation_status == "valid":
            self.message_bar.success(f"«{name}»: конфигурация корректна.")
        else:
            self.message_bar.error(f"«{name}»: " + "; ".join(config.validation_errors))
        self._refresh_table()

    def _install_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        config = self.app.config_manager.open(self.app.container, name)
        if config.validation_status != "valid":
            self.message_bar.error(
                f"«{name}» ещё не прошла проверку (статус: {config.validation_status}). "
                "Нажмите «Проверить» перед установкой."
            )
            return
        from .install_wizard import InstallWizardFrame

        self.app.show_frame(InstallWizardFrame, config_name=name)

    def _delete_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        with error_guard(self.message_bar):
            self.app.config_manager.delete(self.app.container, name)
            self.app.save_container()
            self.message_bar.success(f"«{name}» удалена.")
        self._refresh_table()
