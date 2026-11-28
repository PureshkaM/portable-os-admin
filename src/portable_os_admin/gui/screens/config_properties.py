"""Свойства конфигурации: что внутри и что с ней можно сделать.

Открывается двойным щелчком по строке списка -- так же, как свойства
носителя, и по той же причине: свойства объекта это взгляд на одну вещь, а не
ещё один раздел программы.

До 17.09.2026 на экране конфигураций стояло шесть кнопок в трёх рядах, из них
пять -- действия над выбранной строкой, и среди них «Удалить» рядом с
«Редактировать». Теперь действия живут в меню строки, а здесь -- сведения и
то, что делают с конфигурацией чаще всего.

Показывается не всё подряд, а то, от чего зависит собранная система: из чего
ставится, каким будет рабочее место, чем защищён носитель. Остальное правят
мастером, и дублировать его семь шагов здесь незачем.
"""
from __future__ import annotations

from ..ui import tk
from ..ui import ttk

from ...config.validator import ConfigValidationUnavailableError
from .. import theme
from ..widgets import PropertiesWindow, facts_table


#: Как называются состояния проверки по-русски. В контейнере хранится ключ:
#: формулировку правят, а записанное остаётся.
VALIDATION_TEXT = {
    "valid": "проверена",
    "invalid": "с ошибками",
    "unchecked": "не проверена",
}


class ConfigPropertiesWindow(PropertiesWindow):
    """Окно свойств одной конфигурации."""

    def __init__(self, parent: tk.Widget, app, name: str):
        self.app = app
        self.name = name
        self.config = app.config_manager.open(app.container, name)

        super().__init__(parent, f"Конфигурация: {name}", width=760, height=620)

        self._build_facts()
        self._build_system()

    # -- вкладка «Сведения» -------------------------------------------------
    def _build_facts(self) -> None:
        page = self.tab("Сведения")

        self._facts_host = ttk.Frame(page, style="Card.TFrame")
        self._facts_host.pack(fill="both", expand=True, anchor="w")
        self._render_facts()

        ttk.Separator(page).pack(fill="x", pady=theme.SPACE_MD)

        ttk.Label(
            page,
            text="Собирать носитель можно только из проверенной конфигурации: "
            "проверка читает зеркало и состав пакетов, и отказ здесь дешевле "
            "отказа через полчаса сборки.",
            style="Hint.TLabel",
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(0, theme.SPACE_SM))

        row = ttk.Frame(page, style="Card.TFrame")
        row.pack(anchor="w", fill="x")
        ttk.Button(row, text="Проверить", command=self._validate).pack(side="left")
        ttk.Button(row, text="Редактировать…", command=self._edit).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )

    def _render_facts(self) -> None:
        for child in self._facts_host.winfo_children():
            child.destroy()
        config = self.config
        kiosk = config.kiosk
        rows = [
            ("Имя", config.name),
            ("Система", f"{config.target_os} {config.release}".strip()),
            ("Архитектура", config.arch or "как у этой машины"),
            ("Имя в сети", config.hostname),
            (
                "Режим",
                (f"киоск / {kiosk.desktop_environment}" if kiosk.desktop_environment else "киоск")
                if kiosk.enabled
                else "консольная система",
            ),
            ("Проверка", VALIDATION_TEXT.get(config.validation_status, config.validation_status)),
        ]
        if config.validation_errors:
            rows.append(("Ошибки проверки", "; ".join(config.validation_errors)))
        facts_table(self._facts_host, tuple(rows))

    # -- вкладка «Система» --------------------------------------------------
    def _build_system(self) -> None:
        """То, чем защищён носитель и что на нём будет.

        Отдельной вкладкой, потому что читают это в других случаях, чем
        «какая система»: когда решают, годится ли конфигурация для выдачи.
        """
        page = self.tab("Система")
        config = self.config
        # Имена полей взяты у настоящего объекта конфигурации, а не угаданы:
        # у привязки это `slots`, у локализации `keymap` и `x11_layouts`.
        # Проверено 17.09.2026 -- три догадки из семи были неверны, и окно
        # упало бы на первой же строке сведений.
        binding = config.machine_binding
        revocation = config.revocation
        rows = [
            ("Привязка к машинам", f"{binding.slots} мест" if binding.slots else "выключена"),
            (
                "Отзыв",
                f"включён, {revocation.url}" if revocation.enabled and revocation.url
                else ("включён" if revocation.enabled else "выключен"),
            ),
            ("Минимальная длина фразы", f"{config.luks.min_passphrase_length} знаков"),
            ("Шифр тома", config.luks.cipher),
            ("Язык", config.localisation.locale),
            ("Раскладка", config.localisation.x11_layouts or config.localisation.keymap),
            ("Часовой пояс", config.localisation.timezone),
            ("Пакеты сверх базовых", ", ".join(config.packages) or "нет"),
        ]
        facts_table(page, tuple(rows))

    # -- действия -----------------------------------------------------------
    def _validate(self) -> None:
        try:
            self.app.config_manager.validate(self.config)
        except ConfigValidationUnavailableError as exc:
            self.message_bar.error(f"Проверка недоступна: {exc}")
            return
        self.app.config_manager.save(self.app.container, self.config)
        self.app.save_container()
        self._render_facts()
        if self.config.validation_status == "valid":
            self.message_bar.success("Конфигурация корректна.")
        else:
            self.message_bar.error("; ".join(self.config.validation_errors))

    def _edit(self) -> None:
        """Правка -- мастером: семь шагов сюда не переносятся.

        Окно свойств закрывается: мастер занимает окно целиком, и оставлять
        поверх него окно с устаревшими сведениями незачем.
        """
        from .config_wizard import ConfigWizardFrame

        self.destroy()
        self.app.show_frame(ConfigWizardFrame, config_name=self.name)


def open_config_properties(parent: tk.Widget, app, name: str) -> ConfigPropertiesWindow:
    window = ConfigPropertiesWindow(parent, app, name)
    window.take_focus()
    return window
