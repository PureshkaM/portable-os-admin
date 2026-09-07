"""Выбор носителя — общий шаг мастера установки и мастера развёртывания.

Вынесено в отдельный модуль не ради экономии строк. Это шаг, после которого
носитель стирается, и вся его ценность в мелочах, добытых живыми прогонами:
список съёмных устройств ненадёжен (флаг RM у многих флешек не выставлен),
поэтому рядом всегда есть ручное поле; устройство описывается тем, что о нём
говорит система, а не тем, что набрано в форме, иначе теряются точки
монтирования — а установщик именно по ним отмонтирует носитель перед
разметкой.

Две копии такой проверки — это два места, где можно забыть исправить.
"""
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
    """Шаг «Носитель» для мастера, у которого есть `message_bar`.

    Требует от класса-хозяина двух атрибутов, заведённых до построения шага:
    `device_path` и `device_size_gb` (обе строки, могут быть пустыми).
    """

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
        self.device_size_var = tk.StringVar(value=self.device_size_gb)
        add_field(form, 2, "Размер устройства (ГБ):", self.device_size_var, hint="для проверки достаточности места", width=10)

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

    def _parsed_size_gb(self) -> float:
        try:
            return float(self.device_size_gb or "0")
        except ValueError:
            return 0.0

    def _resolve_device(self, size_gb: float) -> BlockDevice:
        """Описывает цель так, как её сейчас видит система, и только если
        прочитать не удалось вовсе — по полям формы.

        Устройство, собранное из одних текстовых полей (а раньше было именно
        так), теряет свои точки монтирования, а установщик по ним отмонтирует
        носитель перед переразметкой. Автомонтировщик рабочего стола цепляет
        флешку сразу при подключении, поэтому выдуманное «точек монтирования
        нет» означало разметку поверх живой файловой системы — ровно в той
        ситуации, в которой этим мастером и пользуются. Заодно возвращается
        настоящее число байт вместо округлённых гигабайт и настоящее имя
        модели для записи в контейнере.
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
