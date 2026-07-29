"""UC-4 "Установка ОС на внешний носитель".

Runs only in dry-run mode from the TUI: it plans and prints every command
that would run, but never touches a real block device. This is a deliberate
v0.1 scope limit (see docs/ARCHITECTURE.md "Known limitations") -- the
underlying OSInstaller does support dry_run=False for a real deployment, but
that path has not been exercised against real hardware yet, so it is not
exposed here.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, RichLog, Static

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
            self._run_dry_install(message, log)

    def _run_dry_install(self, message: Static, log: RichLog) -> None:
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

        device = BlockDevice(
            name=device_path.rsplit("/", 1)[-1],
            path=device_path,
            size_bytes=int(size_gb * 1024**3),
            model="(введено вручную)",
            removable=True,
            mountpoints=(None,),
        )

        installer = OSInstaller(self.app.session)
        log.clear()
        try:
            result = installer.run(device, config, passphrase.encode("utf-8"), dry_run=True)
        except (DeviceNotEligibleError, InstallError) as exc:
            message.update(f"[red]{exc}[/red]")
            return

        for entry in result.plan.results:
            log.write(" ".join(entry.argv))
        self.app.save_container()
        message.update(
            f"[green]План построен. media_id={result.media_record.media_id}, "
            f"код восстановления: {result.recovery_record.code}[/green]"
        )
