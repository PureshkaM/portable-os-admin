"""Создание дистрибутива: первая половина разделённой установки.

Отдельный экран, а не режим установки, потому что это операция другого рода.
Она ничего не стирает, не трогает носитель и может идти в фоне: на выходе —
файл, который потом разворачивается на сколько угодно флешек за минуты.

Разделение видно и в интерфейсе: здесь нет ни выбора устройства, ни
подтверждения стирания, ни парольной фразы. Шифрование создаётся не тут.
"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

from ...config.schema import InstallConfig
from ...install import OSInstaller
from .. import theme
from ..widgets import (
    KNOWN_ERRORS,
    Column,
    UiMailbox,
    WizardFrame,
    add_field,
    data_table,
    run_in_background,
    widget_alive,
)


class BuildWizardFrame(WizardFrame):
    def __init__(self, parent: tk.Widget, app):
        self.app = app
        self.image_path = ""
        self.destination = ""
        self.selected_config_name: str | None = None
        self._valid_names: list[str] = []
        self.extra_packages: list[str] = []
        self._running = False
        self._result = None

        super().__init__(
            parent,
            app,
            step_titles=["Образ ОС", "Конфигурация", "Свои пакеты", "Куда сохранить", "Сборка"],
            step_hints=[
                "",
                "",
                # Единственная подсказка на весь мастер, и она несёт неочевидное:
                # почему это делается здесь, а не на собранном носителе.
                "Ставятся при сборке, поэтому зависимости разрешаются по образу "
                "один раз, и каждый носитель из этого дистрибутива получает "
                "одно и то же.",
                "",
                "",
            ],
            finish_label="К панели",
        )

    # -- step 0: image ------------------------------------------------------
    def _build_step_image(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.image_var = tk.StringVar(value=self.image_path)
        add_field(form, 0, "Файл образа (ISO):", self.image_var, width=52)
        ttk.Button(body, text="Выбрать образ…", command=self._browse_image).pack(
            anchor="w", pady=(theme.SPACE_SM, 0)
        )

    def _browse_image(self) -> None:
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Выберите установочный образ",
            filetypes=[("Образы", "*.iso *.img"), ("Все файлы", "*")],
        )
        if chosen:
            self.image_var.set(chosen)

    def _save_step_image(self) -> None:
        self.image_path = self.image_var.get().strip()

    def _validate_step_image(self) -> bool:
        if not self.image_path:
            self.message_bar.error("Укажите установочный образ.")
            return False
        if not Path(self.image_path).is_file():
            self.message_bar.error(f"Файл не найден: {self.image_path}")
            return False
        return True

    # -- step 1: configuration ---------------------------------------------
    def _build_step_config(self, body: ttk.Frame) -> None:
        self.config_table, _empty = data_table(
            body,
            (
                Column("name", "Конфигурация", 240, 150, stretch=True),
                Column("os", "ОС", 90, 70),
                Column("profile", "Режим", 160, 110),
                Column("state", "Состояние", 150, 120),
            ),
            height=8,
        )

        # Показываются все конфигурации, а непроверенные -- с пометкой.
        #
        # До 04.09.2026 непроверенные просто не попадали в список, и это была
        # худшая из возможных подсказок: администратор, только что
        # сохранивший конфигурацию, не находил её здесь и не мог понять,
        # почему. Собирать из непроверенной по-прежнему нельзя -- отказ
        # приходит при попытке выбрать, и он называет причину.
        self._names = []
        self._valid_names = []
        for name in self.app.config_manager.list_configs(self.app.container):
            config = self.app.config_manager.open(self.app.container, name)
            profile = "киоск" if config.kiosk.enabled else "консоль"
            if config.kiosk.enabled:
                profile += f" / {config.kiosk.desktop_environment}"
            state = {
                "valid": "проверена",
                "invalid": "с ошибками",
            }.get(config.validation_status, "не проверена")
            self._names.append((name, config.validation_status))
            if config.validation_status == "valid":
                self._valid_names.append(name)
            self.config_table.insert(
                "", "end", values=(name, config.target_os, profile, state)
            )

        if not self._names:
            ttk.Label(
                body,
                text="Конфигураций пока нет. Создайте её в разделе «Конфигурации».",
                style="Warning.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))
        elif not self._valid_names:
            ttk.Label(
                body,
                text="Ни одна конфигурация не проверена. Откройте раздел «Конфигурации» "
                "и нажмите «Проверить» — сборка идёт только из проверенной.",
                style="Warning.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

    def _validate_step_config(self) -> bool:
        selection = self.config_table.selection()
        if not selection:
            self.message_bar.error("Выберите конфигурацию.")
            return False
        name, status = self._names[self.config_table.index(selection[0])]
        if status != "valid":
            # Причина называется здесь, а не выражается отсутствием строки.
            reason = {
                "invalid": "она не прошла проверку",
            }.get(status, "она ещё не проверена")
            self.message_bar.error(
                f"Из «{name}» собрать нельзя: {reason}. Откройте раздел «Конфигурации», "
                "нажмите «Проверить» и вернитесь сюда."
            )
            return False
        self.selected_config_name = name
        return True

    # -- step 2: the administrator's own packages ---------------------------
    def _build_step_packages(self, body: ttk.Frame) -> None:
        """Файлы .deb/.rpm, которых нет ни в одном репозитории.

        Ради этого шага инструмент во многом и существует. Клиента VPN и
        клиента удалённого рабочего стола в базовой системе нет намеренно: у
        каждой организации свой шлюз и свой клиент, и выбор за администратора
        не сделать. Значит последний шаг до готового рабочего места он делает
        сам, своими пакетами, -- а до этого шага сделать это через интерфейс
        было нельзя: код установки существовал с самого начала и не был
        подключён ни к одной кнопке.
        """
        self.packages_table, _packages_empty = data_table(
            body,
            (
                Column("name", "Файл", 420, 200, stretch=True),
                Column("size", "Размер", 120, 100, numeric=True),
            ),
            height=7,
        )

        buttons = ttk.Frame(body)
        buttons.pack(anchor="w", fill="x", pady=(theme.SPACE_SM, 0))
        ttk.Button(buttons, text="Добавить пакеты…", command=self._browse_packages).pack(side="left")
        ttk.Button(buttons, text="Убрать", command=self._remove_package).pack(
            side="left", padx=(theme.SPACE_SM, 0)
        )
        self._refresh_packages()

    def _package_suffix(self) -> str:
        """Какое расширение подходит выбранной конфигурации.

        Берётся из установщика, а не пишется здесь второй раз: разойдись эти
        два списка -- и интерфейс принимал бы файл, который сборка отвергнет
        через полчаса работы.
        """
        config = self.app.config_manager.open(self.app.container, self.selected_config_name)
        return OSInstaller.PACKAGE_SUFFIX.get(config.target_os, "")

    def _refresh_packages(self) -> None:
        self.packages_table.delete(*self.packages_table.get_children())
        for path in self.extra_packages:
            size = Path(path).stat().st_size if Path(path).is_file() else 0
            self.packages_table.insert(
                "", "end", values=(Path(path).name, f"{size / 1024 / 1024:.1f} МБ")
            )

    def _browse_packages(self) -> None:
        suffix = self._package_suffix()
        chosen = filedialog.askopenfilenames(
            parent=self,
            title="Выберите пакеты",
            filetypes=[(f"Пакеты {suffix}", f"*{suffix}"), ("Все файлы", "*")],
        )
        # Расширение проверяется здесь, а не только перед сборкой: узнать о
        # том, что взят .deb вместо .rpm, следует в тот же миг, а не после
        # получаса распаковки.
        wrong = [name for name in chosen if suffix and not name.lower().endswith(suffix)]
        if wrong:
            names = ", ".join(Path(name).name for name in wrong)
            self.message_bar.error(f"Нужен {suffix}: {names}")
        elif chosen:
            # Снять прошлый отказ. Иначе экран противоречит сам себе: список
            # пополнился, а под ним красным висит «Нужен .rpm» от предыдущей
            # попытки -- замечено прогоном через интерфейс 01.09.2026.
            self.message_bar.clear()
        for name in chosen:
            if name not in self.extra_packages and name not in wrong:
                self.extra_packages.append(name)
        self._refresh_packages()

    def _remove_package(self) -> None:
        selection = self.packages_table.selection()
        if not selection:
            return
        for item in selection:
            index = self.packages_table.index(item)
            del self.extra_packages[index]
        self._refresh_packages()

    # -- step 3: destination ------------------------------------------------
    def _build_step_destination(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        # Умолчание -- каталог рядом с контейнером администратора: там же, где
        # он работает, и туда точно есть право писать. Каталог `configs/` для
        # этого больше не годится -- конфигурации живут в контейнере, и такого
        # каталога может не быть вовсе.
        default = str(self.app.container_path.parent) if self.app.container_path else ""
        self.destination_var = tk.StringVar(value=self.destination or default)
        add_field(
            form,
            0,
            "Каталог для дистрибутива:",
            self.destination_var,
            width=52,
            hint="Появятся файл системы, его контрольная сумма и описание.",
        )
        ttk.Button(body, text="Выбрать каталог…", command=self._browse_destination).pack(
            anchor="w", pady=(theme.SPACE_SM, 0)
        )
    def _browse_destination(self) -> None:
        chosen = filedialog.askdirectory(parent=self, title="Куда сохранить дистрибутив")
        if chosen:
            self.destination_var.set(chosen)

    def _save_step_destination(self) -> None:
        self.destination = self.destination_var.get().strip()

    def _validate_step_destination(self) -> bool:
        if not self.destination:
            self.message_bar.error("Укажите каталог для дистрибутива.")
            return False
        return True

    # -- step 3: build ------------------------------------------------------
    def _build_step_run(self, body: ttk.Frame) -> None:
        config = self.app.config_manager.open(self.app.container, self.selected_config_name)
        summary = ttk.Frame(body)
        summary.pack(fill="x")
        for label, value in (
            ("Образ:", Path(self.image_path).name),
            ("Конфигурация:", self.selected_config_name),
            ("Режим:", "киоск" if config.kiosk.enabled else "консольная система"),
            ("Свои пакеты:", ", ".join(Path(p).name for p in self.extra_packages) or "нет"),
            ("Куда:", self.destination),
        ):
            row = ttk.Frame(summary)
            row.pack(anchor="w", fill="x")
            ttk.Label(row, text=label, width=16).pack(side="left")
            ttk.Label(row, text=value).pack(side="left")

        self.start_btn = ttk.Button(
            body, text="Собрать дистрибутив", style="Accent.TButton", command=self._start
        )
        self.start_btn.pack(anchor="w", pady=(theme.SPACE_MD, theme.SPACE_SM))

        self.stage_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.stage_var, wraplength=theme.TEXT_WRAP).pack(anchor="w")
        self.progress = ttk.Progressbar(body, maximum=10)
        self.progress.pack(fill="x", pady=(theme.SPACE_XS, theme.SPACE_SM))

        self.log_text = tk.Text(body, height=10, width=90, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def _log(self, text: str) -> None:
        if not widget_alive(self.log_text):
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _on_stage(self, index: int, total: int, text: str) -> None:
        # From the worker thread: never touch a widget here, only post.
        self._stages.post((index, total, text))

    def _show_stage(self, payload) -> None:
        index, total, text = payload
        self.stage_var.set(f"Шаг {index} из {total}: {text}")
        self.progress.configure(maximum=total, value=index)
        self._log(f"[{index}/{total}] {text}")

    def _start(self) -> None:
        if self._running:
            return
        config = self.app.config_manager.open(self.app.container, self.selected_config_name)
        self._running = True
        self.start_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self._stages = UiMailbox(self, self._show_stage)
        self._stages.start()
        self.message_bar.info("Идёт сборка.")
        self._log(f"--- СБОРКА ДИСТРИБУТИВА {config.name} ---")

        installer = OSInstaller(self.app.session)

        def work():
            return installer.build_distribution(
                config,
                self.destination,
                image_path=self.image_path,
                extra_packages=list(self.extra_packages),
                dry_run=False,
                on_step=self._on_stage,
            )

        run_in_background(self, work, on_success=self._done, on_error=self._failed)

    def _done(self, result) -> None:
        self._running = False
        if hasattr(self, "_stages"):
            self._stages.stop()
        meta = result.metadata
        self._log(f"Готово. Архив: {meta.archive}")
        self._log(f"Контрольная сумма: {meta.sha256}")
        self.message_bar.success(
            f"Дистрибутив собран: {meta.archive}, сумма записана в {meta.archive}.sha256."
        )
        if widget_alive(self.start_btn):
            self.start_btn.configure(state="disabled")
        for button in (self.back_btn, self.cancel_btn, self.next_btn):
            if widget_alive(button):
                button.configure(state="normal")

    def _failed(self, exc: BaseException) -> None:
        self._running = False
        if hasattr(self, "_stages"):
            self._stages.stop()
        if isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(f"{exc} Носитель не затрагивался.")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}")
        for button in (self.start_btn, self.back_btn, self.cancel_btn, self.next_btn):
            if widget_alive(button):
                button.configure(state="normal")

    # -- wiring -------------------------------------------------------------
    _STEP_BUILDERS = [
        _build_step_image,
        _build_step_config,
        _build_step_packages,
        _build_step_destination,
        _build_step_run,
    ]
    _STEP_SAVERS = [_save_step_image, None, None, _save_step_destination, None]
    _STEP_VALIDATORS = [
        _validate_step_image,
        _validate_step_config,
        # Своих пакетов может не быть вовсе, и это обычный случай: проверять
        # здесь нечего, отобранные проверены при выборе.
        None,
        _validate_step_destination,
        None,
    ]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        self._STEP_BUILDERS[index](self, body)

    def save_step(self, index: int) -> None:
        saver = self._STEP_SAVERS[index]
        if saver is not None:
            saver(self)

    def validate_step(self, index: int) -> bool:
        validator = self._STEP_VALIDATORS[index]
        return True if validator is None else validator(self)

    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
