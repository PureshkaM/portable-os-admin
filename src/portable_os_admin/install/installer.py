"""OSInstaller: orchestrates UC-4 "Установка ОС на внешний носитель" end to end.

Mirrors the activity diagram (Рисунок 2, "Сеанс администратора" swimlane):
check possible -> partition -> LUKS format -> user + recovery keyslot ->
boot environment -> OS install -> service partitions -> save records.

Every destructive step is dry-run by default (see command_runner.py); this
orchestrator has been exercised end-to-end only in dry-run mode against a
fake device (see tests/test_installer.py) -- it has NOT been run against a
real external medium. Treat dry_run=False as unverified until confirmed on
real hardware, per the project's working agreement.
"""
from __future__ import annotations

import base64
import importlib.resources
import uuid
from dataclasses import dataclass

from .. import crypto
from ..config.schema import InstallConfig
from ..container.models import MediaRecord, RecoveryRecord
from ..errors import ConfigValidationError, InstallError
from ..session import AdminSession
from .backends import get_backend
from .command_runner import CommandPlan, CommandResult, CommandRunner
from .devices import BlockDevice, check_install_eligible
from .disk import DiskManager, plan_partitions
from .luks import LuksManager

MIB = 1024 * 1024
MIN_SYSTEM_PARTITION_MB = 4096
RECOVERY_KEYSLOT = 7


@dataclass
class InstallResult:
    media_record: MediaRecord
    recovery_record: RecoveryRecord
    plan: CommandPlan


class OSInstaller:
    def __init__(
        self,
        session: AdminSession,
        disk_manager: DiskManager | None = None,
        luks_manager: LuksManager | None = None,
        runner: CommandRunner | None = None,
    ):
        self.session = session
        self.disk_manager = disk_manager or DiskManager()
        self.luks_manager = luks_manager or LuksManager()
        self.runner = runner or CommandRunner()

    def check_possible(self, device: BlockDevice, config: InstallConfig) -> None:
        """"Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
        must run, and must pass, before anything below touches the device."""
        required_mb = (
            config.disk.boot_partition_mb
            + config.disk.log_partition_mb
            + config.disk.exchange_partition_mb
            + MIN_SYSTEM_PARTITION_MB
        )
        check_install_eligible(device, required_mb * MIB)

    def run(
        self,
        device: BlockDevice,
        config: InstallConfig,
        user_passphrase: bytes,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
    ) -> InstallResult:
        self.session.require_open()

        # "Конфигурация.Использование.Запрет" / "Установка.Конфигурация.Запрет"
        if config.validation_status != "valid":
            raise ConfigValidationError(
                ["установка запрещена: файл конфигурации не прошёл проверку"]
            )

        self.check_possible(device, config)

        plan = CommandPlan(device=device.path)
        partition_plan = plan_partitions(device.path, config.disk)

        try:
            disk_plan = self.disk_manager.apply(partition_plan, dry_run=dry_run, confirm_device=confirm_device)
            plan.results.extend(disk_plan.results)

            boot_partition = partition_plan.partition_path(partition_plan.partitions[0])
            system_partition = partition_plan.partition_path(partition_plan.partitions[1])
            logs_partition = partition_plan.partition_path(partition_plan.partitions[2])
            exchange_partition = partition_plan.partition_path(partition_plan.partitions[3])

            luks_plan = self.luks_manager.format(
                system_partition,
                user_passphrase,
                cipher=config.luks.cipher,
                key_size=config.luks.key_size_bits,
                dry_run=dry_run,
                confirm_device=confirm_device,
            )
            plan.results.extend(luks_plan.results)

            recovery_code = crypto.generate_recovery_code()
            recovery_salt = crypto.new_salt()
            recovery_passphrase = crypto.derive_recovery_passphrase(recovery_code, recovery_salt)

            add_key_plan = self.luks_manager.add_keyslot(
                system_partition,
                user_passphrase,
                recovery_passphrase,
                slot=RECOVERY_KEYSLOT,
                dry_run=dry_run,
                confirm_device=confirm_device,
            )
            plan.results.extend(add_key_plan.results)

            media_id = crypto.new_media_id()
            self._install_boot_environment(
                boot_partition, media_id, recovery_salt, plan, dry_run=dry_run, confirm_device=confirm_device
            )

            mount_root = f"/mnt/poa-install-{media_id}"
            backend = get_backend(config.target_os)
            self._run(backend.bootstrap_command(mount_root, config), plan, dry_run, confirm_device)
            for argv in backend.chroot_setup_commands(mount_root, config):
                self._run(argv, plan, dry_run, confirm_device)

            self._run(["mkfs.ext4", "-F", "-L", "POA_LOGS", logs_partition], plan, dry_run, confirm_device)
            self._run(["mkfs.ext4", "-F", "-L", "POA_EXCHANGE", exchange_partition], plan, dry_run, confirm_device)

            luks_uuid = self._read_luks_uuid(system_partition, dry_run)
        except Exception as exc:
            # "Установка.Ошибка.Сохранение": nothing below this point runs,
            # so no MediaRecord/RecoveryRecord is ever added to the container.
            raise InstallError(f"установка прервана: {exc}") from exc

        media_record = MediaRecord(
            media_id=media_id,
            luks_uuid=luks_uuid,
            label=device.model or device.path,
            config_name=config.name,
            target_os=config.target_os,
        )
        recovery_record = RecoveryRecord(
            media_id=media_id,
            slot=RECOVERY_KEYSLOT,
            code=recovery_code,
            salt_b64=base64.b64encode(recovery_salt).decode("ascii"),
        )

        # "Установка.Контейнер.Сохранение" -- caller still owns flushing the
        # container to disk via ContainerManager.save().
        self.session.container.add_media_record(media_record)
        self.session.container.add_recovery_record(recovery_record)

        return InstallResult(media_record=media_record, recovery_record=recovery_record, plan=plan)

    def _install_boot_environment(
        self,
        boot_partition: str,
        media_id: str,
        recovery_salt: bytes,
        plan: CommandPlan,
        *,
        dry_run: bool,
        confirm_device: str | None,
    ) -> None:
        """"Установка.Загрузка": formats the boot/ESP partition and deploys
        the reference recovery script + its metadata onto it.

        This does NOT yet wire the script into a real bootloader/initramfs
        (GRUB entry, dracut/initramfs-tools hook) -- see
        docs/ARCHITECTURE.md "Known limitations" for why that integration is
        deliberately deferred."""
        mount_point = f"/mnt/poa-boot-{media_id}"
        self._run(["mkfs.vfat", "-F", "32", "-n", "POA_BOOT", boot_partition], plan, dry_run, confirm_device)
        self._run(["mkdir", "-p", mount_point], plan, dry_run, confirm_device)
        self._run(["mount", boot_partition, mount_point], plan, dry_run, confirm_device)
        self._run(["mkdir", "-p", f"{mount_point}/poa"], plan, dry_run, confirm_device)

        script_src = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh")
        self._run(["cp", str(script_src), f"{mount_point}/poa/recovery_module.sh"], plan, dry_run, confirm_device)

        self._write_file(
            f"{mount_point}/poa/media_id.txt", media_id + "\n", plan, dry_run, confirm_device
        )
        self._write_file(
            f"{mount_point}/poa/recovery_salt.bin", recovery_salt, plan, dry_run, confirm_device, binary=True
        )
        self._run(["umount", mount_point], plan, dry_run, confirm_device)

    def _run(self, argv: list[str], plan: CommandPlan, dry_run: bool, confirm_device: str | None) -> CommandResult:
        return self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device)

    def _write_file(
        self,
        path: str,
        content,
        plan: CommandPlan,
        dry_run: bool,
        confirm_device: str | None,
        binary: bool = False,
    ) -> None:
        """File writes onto an already-mounted partition are plain filesystem
        operations from the host's point of view, so they are recorded on
        the plan like any other step but performed directly instead of via
        subprocess."""
        result = CommandResult(argv=["<write-file>", path], executed=not dry_run)
        plan.results.append(result)
        if dry_run:
            return
        if confirm_device != plan.device:
            raise InstallError("подтверждение целевого устройства не совпадает — операция отменена")
        mode = "wb" if binary else "w"
        with open(path, mode) as handle:
            handle.write(content)

    def _read_luks_uuid(self, partition: str, dry_run: bool) -> str:
        if dry_run:
            return str(uuid.uuid4())
        import subprocess

        proc = subprocess.run(
            LuksManager.uuid_command(partition), capture_output=True, text=True, timeout=15, check=True
        )
        return proc.stdout.strip()
