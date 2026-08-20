"""UC-4 "Установка ОС на внешний носитель", as a guided wizard.

Direct response to live RedOS testing: a real USB stick did not appear in
the auto-detected list (lsblk's RM flag is unreliable, fixed separately in
install/devices.py) and the manual-entry fallback wasn't obvious enough to
find. Here both are always shown together on the same step, with explicit
text explaining when/why to use the manual field.

The real (dry_run=False) install run is a single, already-tested, unchanged
call into OSInstaller.run() -- this wizard does not touch installer.py or
command_runner.py at all, it only moves that blocking call off the UI
thread (see gui/widgets.py run_in_background) so the window doesn't appear
to hang for however long partitioning/LUKS/debootstrap take.
"""
from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from ...errors import DeviceNotEligibleError, InstallError
from ...install import BlockDevice, DeviceLister, OSInstaller
from ..widgets import KNOWN_ERRORS, WizardFrame, add_field, run_in_background

#: Used for the "this will be destroyed" copy. A plain red rather than a theme
#: colour, because the warning has to read as a warning under whatever ttk
#: theme the host desktop supplies.
DANGER_COLOR = "#c0362c"

NOT_ROOT_MESSAGE = (
    "Реальная установка требует прав root: разметка диска, cryptsetup и mkfs "
    "обычному пользователю недоступны. Закройте программу и запустите её от root "
    "(например, «sudo portable-os-admin»), иначе установка прервётся на первой же команде. "
    "Режим плана (без изменений) работает и без root."
)


def _is_root() -> bool:
    # getuid is absent on Windows; this application only performs real installs
    # on Linux, so treat "cannot tell" as root and let the OS refuse instead.
    return getattr(os, "geteuid", lambda: 0)() == 0


class InstallWizardFrame(WizardFrame):
    def __init__(self, parent: tk.Widget, app, config_path: Path | None = None):
        self.preselected_config_path = config_path
        self.selected_config_path: Path | None = None
        self.selected_config = None
        self.device_path = ""
        self.device_size_gb = "16"
        self._devices: list[BlockDevice] = []
        self._running = False

        super().__init__(
            parent,
            app,
            step_titles=["Конфигурация", "Носитель", "Пароль", "Подтверждение и установка"],
            step_hints=[
                "Выберите, какую проверенную конфигурацию устанавливать.",
                "Выберите внешний носитель: из автоматически найденных устройств, либо укажите путь вручную.",
                "Пароль пользователя, которым будет защищён системный раздел (LUKS).",
                "Последний шаг перед необратимым стиранием носителя. Проверьте, что указан именно тот диск.",
            ],
            finish_label="К панели",
        )

    # -- step 0: pick a validated config ----------------------------------
    def _build_step_config(self, body: ttk.Frame) -> None:
        ttk.Label(body, text="Проверенные конфигурации:").pack(anchor="w")
        self.config_table = ttk.Treeview(body, columns=("file", "target_os", "release"), show="headings", height=8)
        for col, text in (("file", "файл"), ("target_os", "цель"), ("release", "релиз")):
            self.config_table.heading(col, text=text)
            self.config_table.column(col, width=180, anchor="w")
        self.config_table.pack(fill="both", expand=True, pady=(4, 8))

        all_paths = self.app.config_manager.list_configs(self.app.configs_dir)
        self._valid_paths = []
        for path in all_paths:
            config = self.app.config_manager.open(path)
            if config.validation_status != "valid":
                continue
            self._valid_paths.append(path)
            self.config_table.insert("", "end", values=(path.name, config.target_os, config.release))

        if not self._valid_paths:
            ttk.Label(
                body,
                text="Нет проверенных конфигураций. Сначала создайте конфигурацию и нажмите «Проверить».",
                foreground="#c0362c",
                wraplength=600,
                justify="left",
            ).pack(anchor="w", pady=(4, 4))
            ttk.Button(body, text="Перейти к конфигурациям", command=self._go_to_configs).pack(anchor="w")
            return

        preselect = self.selected_config_path or self.preselected_config_path
        if preselect in self._valid_paths:
            children = self.config_table.get_children()
            row_id = children[self._valid_paths.index(preselect)]
            self.config_table.selection_set(row_id)
            self.config_table.see(row_id)

    def _go_to_configs(self) -> None:
        from .configs_list import ConfigsListFrame

        self.app.show_frame(ConfigsListFrame)

    def _validate_step_config(self) -> bool:
        if not self._valid_paths:
            self.message_bar.error("Сначала создайте и проверьте хотя бы одну конфигурацию.")
            return False
        selection = self.config_table.selection()
        if not selection:
            self.message_bar.error("Выберите конфигурацию в таблице.")
            return False
        index = self.config_table.index(selection[0])
        self.selected_config_path = self._valid_paths[index]
        self.selected_config = self.app.config_manager.open(self.selected_config_path)
        return True

    # -- step 1: device -----------------------------------------------------
    def _build_step_device(self, body: ttk.Frame) -> None:
        ttk.Button(body, text="Обновить список устройств", command=self._refresh_devices).pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Список ниже определяется автоматически по данным системы и может быть неполным -- "
                "многие USB-накопители не всегда опознаются как съёмные. Если нужного носителя нет в "
                "списке, это нормально: просто укажите его путь вручную в поле ниже."
            ),
            foreground="#777777",
            wraplength=700,
            justify="left",
        ).pack(anchor="w", pady=(6, 8))

        self.devices_table = ttk.Treeview(body, columns=("path", "model", "size"), show="headings", height=6)
        for col, text in (("path", "устройство"), ("model", "модель"), ("size", "размер (байт)")):
            self.devices_table.heading(col, text=text)
            self.devices_table.column(col, width=180, anchor="w")
        self.devices_table.pack(fill="both", expand=False, pady=(0, 8))
        self.devices_table.bind("<<TreeviewSelect>>", self._on_device_row_selected)

        form = ttk.Frame(body)
        form.pack(anchor="w", pady=(4, 0))
        self.device_path_var = tk.StringVar(value=self.device_path)
        add_field(
            form,
            0,
            "Путь к устройству:",
            self.device_path_var,
            hint="например /dev/sdb -- заполняется автоматически при выборе строки выше, либо введите вручную",
            width=32,
        )
        self.device_size_var = tk.StringVar(value=self.device_size_gb)
        add_field(form, 2, "Размер устройства (ГБ):", self.device_size_var, hint="для проверки достаточности места", width=10)

        self._refresh_devices()

    def _refresh_devices(self) -> None:
        run_in_background(self, lambda: DeviceLister().list_removable(), on_success=self._on_devices_loaded, on_error=self._on_devices_error)

    def _on_devices_loaded(self, devices: list[BlockDevice]) -> None:
        self._devices = devices
        for item in self.devices_table.get_children():
            self.devices_table.delete(item)
        for device in devices:
            self.devices_table.insert("", "end", values=(device.path, device.model, device.size_bytes))
        if not devices:
            self.message_bar.info("Съёмные устройства не найдены автоматически -- укажите путь вручную ниже.")

    def _on_devices_error(self, exc: BaseException) -> None:
        self.message_bar.info(f"Список устройств недоступен ({exc}); укажите путь вручную ниже.")

    def _on_device_row_selected(self, _event) -> None:
        selection = self.devices_table.selection()
        if not selection:
            return
        index = self.devices_table.index(selection[0])
        device = self._devices[index]
        self.device_path_var.set(device.path)
        self.device_size_var.set(f"{device.size_bytes / 1024**3:.1f}")

    def _save_step_device(self) -> None:
        self.device_path = self.device_path_var.get().strip()
        self.device_size_gb = self.device_size_var.get().strip()

    def _validate_step_device(self) -> bool:
        if not self.device_path:
            self.message_bar.error("Укажите путь к устройству.")
            return False
        try:
            float(self.device_size_gb or "0")
        except ValueError:
            self.message_bar.error("Некорректный размер устройства.")
            return False
        return True

    # -- step 2: passphrase --------------------------------------------------
    def _build_step_passphrase(self, body: ttk.Frame) -> None:
        form = ttk.Frame(body)
        form.pack(anchor="w")
        self.passphrase_var = tk.StringVar(value=getattr(self, "passphrase", ""))
        add_field(form, 0, "Пароль пользователя (LUKS):", self.passphrase_var, show="*", width=30)
        self.passphrase_confirm_var = tk.StringVar(value=getattr(self, "passphrase", ""))
        add_field(form, 2, "Повторите пароль:", self.passphrase_confirm_var, show="*", width=30)

    def _save_step_passphrase(self) -> None:
        self.passphrase = self.passphrase_var.get()

    def _validate_step_passphrase(self) -> bool:
        if not self.passphrase_var.get():
            self.message_bar.error("Введите пользовательский пароль (LUKS).")
            return False
        if self.passphrase_var.get() != self.passphrase_confirm_var.get():
            self.message_bar.error("Пароли не совпадают.")
            return False
        return True

    # -- step 3: what will be destroyed, then install --------------------------
    def _build_step_confirm(self, body: ttk.Frame) -> None:
        """The last thing the administrator sees before the medium is erased.

        There is deliberately no "preview what would happen" mode here. This
        tool exists to install; a second mode that emits a list of shell
        commands is developer scaffolding, and offering both on one screen only
        made it unclear which one the button was about to do. What belongs here
        instead is the thing a person actually needs before an irreversible
        action: exactly what is on the device right now, and what will be left
        of it.
        """
        cfg = self.selected_config
        ttk.Label(
            body,
            text=f"Конфигурация: {cfg.name} ({cfg.target_os}, {cfg.release})",
            wraplength=760,
            justify="left",
        ).pack(anchor="w")

        device = self._resolve_device(self._parsed_size_gb())
        self._confirmed_device = device

        warning = ttk.LabelFrame(body, text=" Носитель будет полностью очищен ")
        warning.pack(fill="x", pady=(10, 6))
        ttk.Label(
            warning,
            text=(
                f"{device.path}"
                + (f"  ·  {device.model}" if device.model else "")
                + f"  ·  {device.size_bytes / 1024**3:.1f} ГБ"
            ),
            font=("TkDefaultFont", 10, "bold"),
            justify="left",
        ).pack(anchor="w", padx=10, pady=(8, 2))

        if device.partitions:
            ttk.Label(
                warning,
                text="Сейчас на носителе, всё это будет уничтожено безвозвратно:",
                foreground=DANGER_COLOR,
                justify="left",
            ).pack(anchor="w", padx=10)
            for partition in device.partitions:
                ttk.Label(warning, text="    • " + partition.describe(), justify="left").pack(anchor="w", padx=10)
        else:
            ttk.Label(
                warning,
                text="Разделов на носителе не обнаружено — он будет размечен заново.",
                justify="left",
            ).pack(anchor="w", padx=10)

        if device.is_mounted:
            ttk.Label(
                warning,
                text=(
                    "Носитель сейчас смонтирован ("
                    + ", ".join(device.active_mountpoints)
                    + ") — он будет отмонтирован автоматически."
                ),
                foreground=DANGER_COLOR,
                wraplength=720,
                justify="left",
            ).pack(anchor="w", padx=10, pady=(4, 0))

        ttk.Label(
            warning,
            text=(
                "Восстановить данные после установки невозможно. Убедитесь, что указан "
                "именно тот носитель, и что всё нужное с него скопировано."
            ),
            foreground=DANGER_COLOR,
            wraplength=720,
            justify="left",
        ).pack(anchor="w", padx=10, pady=(6, 10))

        confirm_frame = ttk.Frame(body)
        confirm_frame.pack(anchor="w", pady=(4, 0))
        self.confirm_device_var = tk.StringVar()
        add_field(
            confirm_frame,
            0,
            "Повторите путь к устройству:",
            self.confirm_device_var,
            hint="это последняя защита от установки не на тот диск",
            width=30,
        )

        self.start_btn = ttk.Button(body, text="Стереть носитель и установить систему", command=self._start_install)
        self.start_btn.pack(anchor="w", pady=(8, 4))

        self.progress = ttk.Progressbar(body, mode="indeterminate", length=300)

        self.log_text = tk.Text(body, height=12, width=95, state="disabled")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

        if not _is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)

    def _parsed_size_gb(self) -> float:
        try:
            return float(self.device_size_gb or "0")
        except ValueError:
            return 0.0

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _resolve_device(self, size_gb: float) -> BlockDevice:
        """Describes the target as the system currently reports it, falling back
        to the form fields only when it cannot be read at all.

        Building the device purely from the text fields (as this used to do)
        threw away its mountpoints, and the installer uses exactly those to
        unmount the medium before repartitioning it. A desktop automounter
        mounts a stick the moment it is plugged in, so the invented
        "no mountpoints" device meant partitioning over a live filesystem --
        precisely the situation this wizard is most often used in. It also
        recovers the real byte count instead of a rounded gigabyte figure, and
        the real model name for the record written into the container.
        """
        probed = DeviceLister().describe(self.device_path)
        if probed is not None:
            return probed
        for device in getattr(self, "_devices", []):
            if device.path == self.device_path:
                return device
        return BlockDevice(
            name=self.device_path.rsplit("/", 1)[-1],
            path=self.device_path,
            size_bytes=int(size_gb * 1024**3),
            model="(введено вручную)",
            removable=True,
            mountpoints=(),
        )

    def _start_install(self) -> None:
        if self._running:
            return
        confirm_device = self.confirm_device_var.get().strip()
        if confirm_device != self.device_path:
            self.message_bar.error(
                "Подтверждение устройства не совпадает с путём выше — установка отменена."
            )
            return
        # Checked here rather than left to fail inside parted: without this the
        # user completes the whole wizard, starts an irreversible operation and
        # gets a permission error from a tool they never invoked. Nothing has
        # been written at this point, so refusing is free.
        if not _is_root():
            self.message_bar.error(NOT_ROOT_MESSAGE)
            return

        device = getattr(self, "_confirmed_device", None) or self._resolve_device(self._parsed_size_gb())

        self._running = True
        self.start_btn.configure(state="disabled")
        # A real install runs in a background thread and may take minutes;
        # if the wizard's own Back/Next/Cancel destroyed this frame mid-run,
        # the completion callback below would fire against dead widgets and
        # OSInstaller's already-recorded media/recovery records would never
        # reach save_container(). Lock navigation until it's done.
        self.back_btn.configure(state="disabled")
        self.next_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.progress.pack(fill="x", pady=(0, 8), before=self.log_text)
        self.progress.start(12)
        self.message_bar.info(
            "Идёт установка — это надолго: очистка носителя, разметка, шифрование, "
            "загрузка базовой системы по сети. Окно останется отзывчивым."
        )
        self._log(f"--- УСТАНОВКА НА {device.path} ---")

        passphrase_bytes = self.passphrase.encode("utf-8")
        installer = OSInstaller(self.app.session)

        def do_install():
            return installer.run(
                device, self.selected_config, passphrase_bytes, dry_run=False, confirm_device=confirm_device
            )

        run_in_background(
            self,
            do_install,
            on_success=self._on_install_done,
            on_error=self._on_install_error,
        )

    def _stop_progress(self) -> None:
        self._running = False
        self.progress.stop()
        self.progress.pack_forget()
        self.start_btn.configure(state="normal")
        self.back_btn.configure(state=("normal" if self.step_index > 0 else "disabled"))
        self.next_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")

    def _on_install_done(self, result) -> None:
        self._stop_progress()
        for entry in result.plan.results:
            line = " ".join(entry.argv)
            if entry.executed:
                line += f"  [rc={entry.returncode}]"
                if entry.stderr.strip():
                    line += f" stderr: {entry.stderr.strip()}"
            self._log(line)
        self.app.save_container()
        self.message_bar.success(
            f"Установка завершена. media_id={result.media_record.media_id}, "
            f"код восстановления: {result.recovery_record.code} — запишите его, "
            "он также сохранён в контейнере администратора."
        )

    def _on_install_error(self, exc: BaseException) -> None:
        self._stop_progress()
        # No hedging about whether anything happened: by the time this runs the
        # medium has been erased, so the honest statement is that its previous
        # contents are gone regardless of where the failure landed.
        suffix = " Носитель уже изменён — прежние данные на нём не сохранились."
        if isinstance(exc, (DeviceNotEligibleError, InstallError)) or isinstance(exc, KNOWN_ERRORS):
            self.message_bar.error(f"{exc}{suffix}")
        else:
            self.message_bar.error(f"Непредвиденная ошибка: {exc}{suffix}")

    # -- WizardFrame hooks ------------------------------------------------
    _STEP_BUILDERS = [_build_step_config, _build_step_device, _build_step_passphrase, _build_step_confirm]
    _STEP_SAVERS = [None, _save_step_device, _save_step_passphrase, None]
    _STEP_VALIDATORS = [_validate_step_config, _validate_step_device, _validate_step_passphrase, None]

    def build_step(self, index: int, body: ttk.Frame) -> None:
        self._STEP_BUILDERS[index](self, body)

    def save_step(self, index: int) -> None:
        saver = self._STEP_SAVERS[index]
        if saver is not None:
            saver(self)

    def validate_step(self, index: int) -> bool:
        validator = self._STEP_VALIDATORS[index]
        if validator is None:
            return True
        return validator(self)

    def on_finish(self) -> None:
        self.app.show_dashboard()

    def on_cancel(self) -> None:
        self.app.show_dashboard()
