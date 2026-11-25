"""Выбор носителя для записи образа. Сведения о диске читает операционная система."""
from __future__ import annotations

import os
import signal
import tkinter as tk
from tkinter import ttk

from ...install import BlockDevice, DeviceLister, leftovers
from .. import theme
from ..widgets import Column, add_field, data_table, run_in_background, widget_alive

#: Используется для текста «это будет уничтожено». Обычный красный, а не цвет
#: темы: предупреждение должно читаться как предупреждение под любой ttk-темой,
#: которую подставит рабочий стол.
DANGER_COLOR = "#c0362c"

NOT_ROOT_MESSAGE = (
    "Реальная установка требует прав root: разметка диска, cryptsetup и mkfs "
    "обычному пользователю недоступны. Закройте программу и запустите её от root "
    "(например, «sudo portable-os-admin»), иначе установка прервётся на первой же команде. "
    "Режим плана (без изменений) работает и без root."
)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


class DeviceStepMixin:
    """Шаг выбора блочного устройства для мастера выпуска."""

    def _build_step_device(self, body: ttk.Frame) -> None:
        ttk.Button(body, text="Обновить список устройств", command=self._refresh_devices).pack(anchor="w")
        ttk.Label(
            body,
            text="Если нужного носителя нет в списке, укажите путь вручную ниже.",
            foreground=theme.TEXT_MUTED,
            wraplength=theme.TEXT_WRAP,
            justify="left",
        ).pack(anchor="w", pady=(6, 8))

        # Модель тянется за счёт свободного места: имена вроде «Kingston
        # DataTraveler 3.0» длиннее любой разумной ширины, а путь и размер
        # известной длины.
        self.devices_table, _empty = data_table(
            body,
            (
                Column("path", "устройство", 180, 130),
                Column("model", "модель", 180, 130, stretch=True),
                Column("size", "размер (байт)", 150, 120, numeric=True),
            ),
            height=6,
            expand=False,
            pady=(0, 8),
        )
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

        self._refresh_devices()

    def _refresh_devices(self) -> None:
        run_in_background(
            self,
            lambda: DeviceLister().list_removable(),
            on_success=self._on_devices_loaded,
            on_error=self._on_devices_error,
        )

    def _on_devices_loaded(self, devices: list[BlockDevice]) -> None:
        self._devices = devices
        # The step's widgets are gone if the user moved on while lsblk ran.
        if not widget_alive(self.devices_table):
            return
        for item in self.devices_table.get_children():
            self.devices_table.delete(item)
        for device in devices:
            self.devices_table.insert("", "end", values=(device.path, device.model, device.size_bytes))
        if not devices:
            self.message_bar.info("Съёмные устройства не найдены автоматически -- укажите путь вручную ниже.")

    def _on_devices_error(self, exc: BaseException) -> None:
        if not widget_alive(self.devices_table):
            return
        self.message_bar.info(f"Список устройств недоступен ({exc}); укажите путь вручную ниже.")

    def _on_device_row_selected(self, _event) -> None:
        selection = self.devices_table.selection()
        if not selection:
            return
        index = self.devices_table.index(selection[0])
        device = self._devices[index]
        self._selected_row = device
        self.device_path_var.set(device.path)

    def _save_step_device(self) -> None:
        self.device_path = self.device_path_var.get().strip()

    def _validate_step_device(self) -> bool:
        self._selected_device = DeviceLister().describe(self.device_path)
        if self._selected_device is None or self._selected_device.diskseq <= 0 or not self._selected_device.mounts_known:
            self.message_bar.error(f"Не удалось определить диск {self.device_path}. Проверьте путь.")
            return False
        row = getattr(self, "_selected_row", None)
        if row and row.path == self.device_path and row != self._selected_device:
            self.message_bar.error("Устройство изменилось. Обновите список и выберите носитель заново.")
            return False
        return True


class TerminationGuardMixin:
    """Останавливает начатую запись, если программу просят завершиться.

    Тоже добыто живым прогоном: приложение убили посреди установки, и
    запущенный им bootstrap ещё девять минут писал на флешку осиротевшим,
    с открытым томом. Нужен обоим мастерам, которые пишут на носитель.
    """

    def _install_signal_guard(self) -> None:
        """Releases the medium if this process is asked to die mid-install.

        Observed for real: the application was killed while installing, and the
        bootstrap process it had started went on writing to the stick for nine
        minutes, orphaned, with the volume left open. A terminated process
        cannot finish the install, but it can at least stop it cleanly instead
        of leaving the drive spinning.

        SIGKILL cannot be caught -- that case is covered by the check for
        leftovers when the application next starts.
        """
        self._previous_handlers = {}
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                self._previous_handlers[sig] = signal.signal(sig, self._on_termination_signal)
            except (ValueError, OSError):
                # Not the main thread, or the platform has no such signal.
                pass

    def _restore_signal_guard(self) -> None:
        for sig, handler in getattr(self, "_previous_handlers", {}).items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        self._previous_handlers = {}

    def _on_termination_signal(self, _signum, _frame):
        leftovers.clear()
        os._exit(1)
