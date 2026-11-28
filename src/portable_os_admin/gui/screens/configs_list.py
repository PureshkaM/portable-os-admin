"""UC-3 "Управление файлом конфигурации установки" -- list/manage screen.

The wizard (config_wizard.py) is where a config is actually authored; this
screen is the hub you land on before/after that: pick one to edit, validate,
delete, browse its repository packages, or jump straight to installing it.
"""
from __future__ import annotations

from ..ui import tk
from pathlib import Path
from ..ui import ttk

from ...config.validator import ConfigValidationUnavailableError
from .. import theme
from .config_properties import VALIDATION_TEXT
from ..widgets import (
    Column,
    MessageBar,
    data_table,
    error_guard,
    section_card,
    section_header,
    table_menu,
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
        # Заголовки с большой буквы -- как в списке носителей. До 17.09.2026
        # здесь было «файл, цель, релиз, статус проверки», а там «Носитель,
        # Владелец, Состояние»: два списка в одной программе выглядели
        # сделанными разными руками.
        self.table, self.placeholder = data_table(
            card,
            (
                Column("file", "Конфигурация", 220, 140, stretch=True),
                Column("target_os", "Система", 120, 90),
                Column("release", "Выпуск", 120, 90),
                Column("status", "Проверка", 180, 140),
            ),
            height=12,
            empty_text="Конфигураций пока нет.\nНажмите «Создать новую конфигурацию».",
        )
        # Действия -- в меню строки, свойства -- по двойному щелчку. Ряда из
        # шести кнопок под таблицей больше нет: пять из них были действиями
        # над выбранной строкой, и среди них «Удалить» стояла рядом с
        # «Редактировать» -- как равная ей по последствиям.
        table_menu(
            self.table,
            (
                ("Свойства", self._open_properties),
                ("-", None),
                ("Редактировать…", self._edit),
                ("Проверить", self._validate),
                ("Выпустить носитель…", self._release),
                ("-", None),
                ("Поделиться…", self._share),
                ("Удалить", self._delete),
            ),
            on_open=self._open_properties,
        )

        create_row = ttk.Frame(card)
        create_row.pack(fill="x", pady=(theme.SPACE_SM, theme.SPACE_XS))
        ttk.Button(create_row, text="Создать новую конфигурацию", command=self._create_new).pack(side="left")

        self.message_bar = MessageBar(card, on_card=True)
        self.message_bar.pack(fill="x")

        self._refresh_table()

    def _open_properties(self, name: str) -> None:
        """Свойства конфигурации -- окном, а не экраном.

        Список остаётся на месте: это взгляд на одну вещь, а не переход в
        другой раздел программы.
        """
        from .config_properties import open_config_properties

        open_config_properties(self, self.app, name)
        # Окно могло запустить проверку: состояние в списке должно догнать.
        self._refresh_table()

    def _share(self, name: str = "") -> None:
        """Обмен с другим администратором -- на своём экране.

        Выбранная строка уезжает туда вместе с переходом: выбор -- уже ответ,
        и спрашивать его второй раз значит искать в списке то, что только что
        выделили. Ничего не выбрано -- тоже законно: отправить можно все.
        """
        from .configs_share import open_configs_share

        open_configs_share(self, self.app, name)

    def _refresh_table(self) -> None:
        for item in self.table.get_children():
            self.table.delete(item)
        # Список берётся из контейнера -- там же, где носители и записи
        # восстановления. Файлов рядом с ним больше нет.
        self._names = self.app.config_manager.list_configs(self.app.container)
        self.placeholder.update(not self._names)
        for name in self._names:
            config = self.app.config_manager.open(self.app.container, name)
            # `iid=name` -- имя строки и есть имя конфигурации.
            #
            # Без этого Tk выдаёт свои «I001», и меню строки передавало бы
            # действиям идентификатор виджета вместо имени: «Удалить» удаляло
            # бы не то. В списке носителей именем строки давно служит
            # `media_id`, и здесь сделано так же.
            self.table.insert(
                "", "end", iid=name,
                values=(
                    name,
                    config.target_os,
                    config.release,
                    # По-русски, как и в свойствах: `valid` в одном месте и
                    # «проверена» в соседнем -- это два словаря на одну вещь.
                    VALIDATION_TEXT.get(config.validation_status, config.validation_status),
                ),
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

    def _edit(self, name: str) -> None:
        from .config_wizard import ConfigWizardFrame

        self.app.show_frame(ConfigWizardFrame, config_name=name)

    def _validate(self, name: str) -> None:
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

    def _release(self, name: str) -> None:
        config = self.app.config_manager.open(self.app.container, name)
        if config.validation_status != "valid":
            self.message_bar.error(
                f"«{name}» ещё не прошла проверку (статус: {config.validation_status}). "
                "Нажмите «Проверить» перед установкой."
            )
            return
        from .release_wizard import ReleaseWizardFrame

        self.app.show_frame(ReleaseWizardFrame, config_name=name)

    def _delete(self, name: str) -> None:
        # Подтверждение, потому что возвращать неоткуда.
        #
        # Конфигурация -- это семишаговый мастер, а с 09.09.2026 она живёт
        # только внутри контейнера: файла рядом, который раньше можно было
        # достать обратно, больше не существует. Соседние разрушающие действия
        # давно прикрыты -- стирание носителя требует повторить путь устройства,
        # уборка следов спрашивает, -- и удаление оставалось единственным, что
        # срабатывало от одного нажатия.
        from ..ui import messagebox

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
