"""UC-3 "Управление файлом конфигурации установки" -- list/manage screen.

The wizard (config_wizard.py) is where a config is actually authored; this
screen is the hub you land on before/after that: pick one to edit, validate,
delete, browse its repository packages, or jump straight to installing it.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ...config import exchange
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
            pady=(0, theme.SPACE_XS),
        )

        # Обмен с другим администратором стоит отдельным рядом: это не
        # действие над выбранной строкой, а работа с чужим контейнером, и
        # смешивать их в одном ряду значило бы предлагать «Удалить» и
        # «Отправить» как равные по последствиям.
        button_row(
            card,
            (
                ("Мой код обмена…", self._show_my_code),
                ("Отправить…", self._send_selected),
                ("Отправить все…", self._send_all),
                ("Принять…", self._receive),
            ),
            pady=(0, theme.SPACE_SM),
        )

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self._refresh_table()

    # -- обмен с другим администратором -------------------------------------
    def _show_my_code(self) -> None:
        """Свой код -- то, что администратор диктует или пересылает коллеге.

        Код публичный: перехват не даёт ничего, расшифровать присланное может
        только этот контейнер. Поэтому он показывается прямо на экране, а не
        под подтверждением.
        """
        from tkinter import messagebox

        with error_guard(self.message_bar):
            identity = exchange.identity_of(self.app.container)
            self.app.save_container()
            self.clipboard_clear()
            self.clipboard_append(identity.code)
            messagebox.showinfo(
                "Мой код обмена",
                f"Отпечаток: {identity.fingerprint}\n\n{identity.code}\n\n"
                "Код скопирован в буфер обмена. Его можно передавать открыто: "
                "он только шифрует присланное вам, но не расшифровывает.",
                parent=self,
            )

    def _send_selected(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        self._send([name])

    def _send_all(self) -> None:
        names = self.app.config_manager.list_configs(self.app.container)
        if not names:
            self.message_bar.error("Отправлять нечего: в контейнере нет конфигураций.")
            return
        self._send(names)

    def _send(self, names: "list[str]") -> None:
        from tkinter import filedialog, simpledialog

        code = simpledialog.askstring(
            "Код получателя",
            "Код администратора, которому предназначена посылка:",
            parent=self,
        )
        if not code:
            return
        with error_guard(self.message_bar):
            payload = [
                (name, self.app.container.find_config(name).data)
                for name in names
                if self.app.container.find_config(name) is not None
            ]
            blob = exchange.pack(self.app.container, payload, code)
            self.app.save_container()
            target = filedialog.asksaveasfilename(
                parent=self,
                title="Куда сохранить посылку",
                defaultextension=exchange.EXCHANGE_SUFFIX,
                initialfile=("configs" if len(payload) > 1 else payload[0][0])
                + exchange.EXCHANGE_SUFFIX,
            )
            if not target:
                return
            Path(target).write_bytes(blob)
            self.message_bar.success(
                f"Посылка сохранена: {target}. Конфигураций внутри: {len(payload)}. "
                "Открыть её сможет только владелец указанного кода."
            )

    def _receive(self) -> None:
        from tkinter import filedialog, messagebox

        source = filedialog.askopenfilename(
            parent=self,
            title="Посылка от другого администратора",
            filetypes=[("Посылка", "*" + exchange.EXCHANGE_SUFFIX), ("Все файлы", "*")],
        )
        if not source:
            return
        with error_guard(self.message_bar):
            sender, configs = exchange.unpack(self.app.container, Path(source).read_bytes())
            names = ", ".join(name for name, _ in configs)
            # Отправитель называется до того, как что-то попадёт в контейнер:
            # по присланной конфигурации собирают носитель, и «от кого» здесь
            # такой же вопрос, как «что внутри».
            if not messagebox.askyesno(
                "Принять конфигурации",
                f"Отправитель, отпечаток {sender.fingerprint}.\n\n"
                f"В посылке: {names}.\n\n"
                "Одноимённые конфигурации в контейнере будут заменены. Принять?",
                parent=self,
            ):
                return
            for name, data in configs:
                self.app.container.put_config(name, data)
            self.app.save_container()
            self.message_bar.success(
                f"Принято конфигураций: {len(configs)} (от {sender.fingerprint})."
            )
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
        # Подтверждение, потому что возвращать неоткуда.
        #
        # Конфигурация -- это семишаговый мастер, а с 09.09.2026 она живёт
        # только внутри контейнера: файла рядом, который раньше можно было
        # достать обратно, больше не существует. Соседние разрушающие действия
        # давно прикрыты -- стирание носителя требует повторить путь устройства,
        # уборка следов спрашивает, -- и удаление оставалось единственным, что
        # срабатывало от одного нажатия.
        from tkinter import messagebox

        if not messagebox.askyesno(
            "Удалить конфигурацию",
            f"«{name}» будет удалена из контейнера безвозвратно: копии рядом с "
            "контейнером не существует, и собрать её заново можно только пройдя "
            "мастер целиком.\n\nУдалить?",
            icon="warning",
            parent=self,
        ):
            return
        with error_guard(self.message_bar):
            self.app.config_manager.delete(self.app.container, name)
            self.app.save_container()
            self.message_bar.success(f"«{name}» удалена.")
        self._refresh_table()
