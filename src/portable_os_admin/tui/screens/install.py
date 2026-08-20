"""UC-4 "Установка ОС на внешний носитель".

Dry-run by default: builds and prints the command plan without touching any
device. Real execution (dry_run=False) is available but gated behind an
explicit switch plus a repeat-the-device-path confirmation, mirroring the
confirm_device gate already enforced lower down in CommandRunner.run -- the
UI-level gate does not replace that one, it just means a user has to commit
to "real" mode deliberately before OSInstaller.run() is even called with
dry_run=False. See docs/ARCHITECTURE.md "Known limitations" for status: this
path is newly enabled, not yet confirmed by an actual successful run against
real hardware -- treat every install backend as unverified until the user
confirms one worked end to end.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static, Switch

from ...errors import DeviceNotEligibleError, InstallError
from ...install import BlockDevice, DeviceLister, OSInstaller


class InstallScreen(Screen):
    BINDINGS = [("escape", "app.pop_screen", "Назад")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(
            Static("Установка ОС на внешний носитель (режим предпросмотра команд)", id="title"),
            Horizontal(
                Button("Обновить список устройств", id="refresh_devices"),
            ),
            DataTable(id="devices_table"),
            Input(placeholder="Путь к устройству, например /dev/sdb", id="device_path"),
            Input(placeholder="Размер устройства, ГБ (для проверки места)", id="device_size_gb", value="16"),
            Static("Проверенные конфигурации:", id="configs_label"),
            DataTable(id="configs_table"),
            Input(placeholder="Пароль пользователя (LUKS)", password=True, id="passphrase"),
            Horizontal(
                Switch(id="real_run_switch", value=False),
                Static("Реальное выполнение (ОПАСНО: необратимо стирает устройство)", id="real_run_label"),
            ),
            Input(
                placeholder="Для реального запуска: повторите путь к устройству",
                id="confirm_device",
            ),
            Button("Построить план установки (dry-run)", id="run", variant="primary"),
            RichLog(id="plan_log", markup=True),
            Static("", id="message"),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._configs = []
        devices_table = self.query_one("#devices_table", DataTable)
        devices_table.add_columns("устройство", "модель", "размер (байт)")
        configs_table = self.query_one("#configs_table", DataTable)
        configs_table.add_columns("файл", "цель", "релиз")
        self._refresh_configs()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id != "real_run_switch":
            return
        button = self.query_one("#run", Button)
        if event.value:
            button.label = "ВЫПОЛНИТЬ РЕАЛЬНУЮ УСТАНОВКУ (необратимо)"
            button.variant = "error"
        else:
            button.label = "Построить план установки (dry-run)"
            button.variant = "primary"

    def _refresh_configs(self) -> None:
        table = self.query_one("#configs_table", DataTable)
        table.clear()
        self._configs = []
        for path in self.app.config_manager.list_configs(self.app.configs_dir):
            config = self.app.config_manager.open(path)
            if config.validation_status != "valid":
                continue
            self._configs.append(path)
            table.add_row(path.name, config.target_os, config.release)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        message = self.query_one("#message", Static)
        log = self.query_one("#plan_log", RichLog)

        if event.button.id == "refresh_devices":
            table = self.query_one("#devices_table", DataTable)
            table.clear()
            try:
                devices = DeviceLister().list_removable()
            except Exception as exc:  # lsblk missing/non-Linux host, etc.
                message.update(f"[yellow]Список устройств недоступен ({exc}); введите путь вручную[/yellow]")
                return
            self._devices = devices
            for device in devices:
                table.add_row(device.path, device.model, str(device.size_bytes))
            if not devices:
                message.update("[yellow]Съёмные устройства не найдены[/yellow]")
            return

        if event.button.id == "run":
            self._run_install(message, log)

    def _run_install(self, message: Static, log: RichLog) -> None:
        configs_table = self.query_one("#configs_table", DataTable)
        if not self._configs or configs_table.cursor_row is None:
            message.update("[red]Выберите проверенную конфигурацию[/red]")
            return
        config_path = self._configs[configs_table.cursor_row]
        config = self.app.config_manager.open(config_path)

        device_path = self.query_one("#device_path", Input).value.strip()
        if not device_path:
            message.update("[red]Укажите путь к устройству[/red]")
            return
        try:
            size_gb = float(self.query_one("#device_size_gb", Input).value.strip() or "0")
        except ValueError:
            message.update("[red]Некорректный размер устройства[/red]")
            return

        passphrase = self.query_one("#passphrase", Input).value
        if not passphrase:
            message.update("[red]Введите пользовательский пароль (LUKS)[/red]")
            return

        real_run = self.query_one("#real_run_switch", Switch).value
        confirm_device: str | None = None
        if real_run:
            confirm_device = self.query_one("#confirm_device", Input).value.strip()
            if confirm_device != device_path:
                message.update(
                    "[red]Подтверждение устройства не совпадает с путём выше — "
                    "реальная установка отменена[/red]"
                )
                return

        # Describe the target as the system currently reports it. Building it
        # from the form fields alone throws away its mountpoints, and those are
        # what the installer uses to unmount the medium before repartitioning:
        # a desktop automounter mounts a stick as soon as it is plugged in, so
        # an invented "no mountpoints" device meant partitioning over a live
        # filesystem. Falls back to the typed values only if it cannot be read.
        device = DeviceLister().describe(device_path) or BlockDevice(
            name=device_path.rsplit("/", 1)[-1],
            path=device_path,
            size_bytes=int(size_gb * 1024**3),
            model="(введено вручную)",
            removable=True,
            mountpoints=(),
        )

        installer = OSInstaller(self.app.session)
        log.clear()
        try:
            result = installer.run(
                device,
                config,
                passphrase.encode("utf-8"),
                dry_run=not real_run,
                confirm_device=confirm_device,
            )
        except (DeviceNotEligibleError, InstallError) as exc:
            message.update(
                f"[red]{exc}"
                + ("[/red]" if not real_run else " -- часть шагов уже могла реально выполниться[/red]")
            )
            return

        for entry in result.plan.results:
            line = " ".join(entry.argv)
            if entry.executed:
                line += f"  [rc={entry.returncode}]"
                if entry.stderr.strip():
                    line += f" stderr: {entry.stderr.strip()}"
            log.write(line)
        self.app.save_container()
        mode_note = "выполнено реально" if real_run else "план построен (dry-run)"
        message.update(
            f"[green]{mode_note}. media_id={result.media_record.media_id}, "
            f"код восстановления: {result.recovery_record.code}[/green]"
        )
