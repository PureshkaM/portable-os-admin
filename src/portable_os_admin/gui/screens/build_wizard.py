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
from ..widgets import KNOWN_ERRORS, UiMailbox, WizardFrame, add_field, run_in_background, widget_alive


class BuildWizardFrame(WizardFrame):
    def __init__(self, parent: tk.Widget, app):
        self.app = app
        self.image_path = ""
        self.destination = ""
        self.selected_config_path: Path | None = None
        self._valid_paths: list[Path] = []
        self._running = False
        self._result = None

        super().__init__(
            parent,
            app,
            step_titles=["Образ ОС", "Конфигурация", "Куда сохранить", "Сборка"],
            step_hints=[
                "",
                "",
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
        self.config_table = ttk.Treeview(
            body, columns=("name", "os", "profile"), show="headings", height=8
        )
        for column, title, width in (
            ("name", "Конфигурация", 260),
            ("os", "ОС", 100),
            ("profile", "Режим", 180),
        ):
            self.config_table.heading(column, text=title)
            self.config_table.column(column, width=width, anchor="w")
        self.config_table.pack(fill="both", expand=True)

        self._valid_paths = []
        for path in self.app.config_manager.list_configs(self.app.configs_dir):
            config = self.app.config_manager.open(path)
            if config.validation_status != "valid":
                continue
            self._valid_paths.append(path)
            profile = "киоск" if config.kiosk.enabled else "консоль"
            if config.kiosk.enabled:
                profile += f" / {config.kiosk.desktop_environment}"
            self.config_table.insert("", "end", values=(path.name, config.target_os, profile))

        if not self._valid_paths:
            ttk.Label(
                body,
                text="Нет проверенных конфигураций. Создайте конфигурацию и нажмите «Проверить».",
                style="Warning.TLabel",
                wraplength=theme.TEXT_WRAP,
            ).pack(anchor="w", pady=(theme.SPACE_SM, 0))

    def _validate_step_config(self) -> bool:
        selection = self.config_table.selection()
        if not self._valid_paths or not selection:
            self.message_bar.error("Выберите конфигурацию.")
            return False
        index = self.config_table.index(selection[0])
        self.selected_config_path = self._valid_paths[index]
        return True

    # -- step 2: destination ------------------------------------------------
    def _build_step_destination(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w", fill="x")
        self.destination_var = tk.StringVar(value=self.destination or str(self.app.configs_dir))
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
        config = self.app.config_manager.open(self.selected_config_path)
        summary = ttk.Frame(body)
        summary.pack(fill="x")
        for label, value in (
            ("Образ:", Path(self.image_path).name),
            ("Конфигурация:", self.selected_config_path.name),
            ("Режим:", "киоск" if config.kiosk.enabled else "консольная система"),
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
        config = self.app.config_manager.open(self.selected_config_path)
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
    _STEP_BUILDERS = [_build_step_image, _build_step_config, _build_step_destination, _build_step_run]
    _STEP_SAVERS = [_save_step_image, None, _save_step_destination, None]
    _STEP_VALIDATORS = [
        _validate_step_image,
        _validate_step_config,
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
