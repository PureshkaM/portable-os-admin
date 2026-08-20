"""OSInstaller: orchestrates UC-4 "Установка ОС на внешний носитель" end to end.

Mirrors the activity diagram (Рисунок 2, "Сеанс администратора" swimlane):
check possible -> partition -> LUKS format -> user + recovery keyslot ->
filesystems -> base system -> kernel/bootloader -> service partitions ->
save records.

Every destructive step is dry-run by default (see command_runner.py). The
Debian backend's real path has been executed against a real USB stick; the
RedOS and Astra backends have not (see docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import base64
import importlib.resources
import os
import subprocess
import uuid
from dataclasses import dataclass, field

from .. import crypto
from ..config.schema import InstallConfig
from ..container.models import MediaRecord, RecoveryRecord
from ..errors import ConfigValidationError, InstallError
from ..session import AdminSession
from .backends import get_backend
from .backends.base import EXCHANGE_MOUNTPOINT, host_arch
from .command_runner import BOOTSTRAP_TIMEOUT, CommandPlan, CommandResult, CommandRunner
from .devices import BlockDevice, check_install_eligible
from .disk import DiskManager, MediaLayout, build_media_layout, plan_partitions
from .luks import LuksManager

MIB = 1024 * 1024
MIN_SYSTEM_PARTITION_MB = 4096
RECOVERY_KEYSLOT = 7

#: Directory on the (unencrypted) ESP holding the recovery module and the
#: metadata that lets the admin tool recognise this medium later.
ESP_METADATA_DIR = "poa"


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

    def check_possible(self, device: BlockDevice, config: InstallConfig, *, require_unmounted: bool = True) -> None:
        """"Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
        must run, and must pass, before anything below touches the device."""
        required_mb = (
            config.disk.esp_partition_mb
            + config.disk.boot_partition_mb
            + config.disk.log_partition_mb
            + config.disk.exchange_partition_mb
            + MIN_SYSTEM_PARTITION_MB
        )
        check_install_eligible(device, required_mb * MIB, require_unmounted=require_unmounted)

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

        backend = get_backend(config.target_os)
        self._check_arch(backend, config)

        # The installer unmounts the medium itself as an explicit, logged step,
        # so an automounted stick is not a hard stop -- the user has already
        # confirmed the device is about to be erased.
        self.check_possible(device, config, require_unmounted=False)

        media_id = crypto.new_media_id()
        partition_plan = plan_partitions(device.path, config.disk)
        layout = build_media_layout(
            partition_plan,
            mapper_name=f"poa-{media_id}",
            mount_root=f"/mnt/poa-install-{media_id}",
        )

        plan = CommandPlan(device=device.path)
        state = _TeardownState()

        try:
            recovery_code, recovery_salt = self._install(
                device, config, backend, layout, media_id, user_passphrase, plan, state,
                dry_run=dry_run, confirm_device=confirm_device,
            )
        except Exception as exc:
            # "Установка.Ошибка.Сохранение": nothing below this point runs, so
            # no MediaRecord/RecoveryRecord is ever added to the container.
            self._teardown(layout, state, plan, dry_run=dry_run, confirm_device=confirm_device)
            if isinstance(exc, InstallError):
                raise
            raise InstallError(f"установка прервана: {exc}") from exc
        else:
            # Teardown on the success path too: leaving the medium mounted and
            # the LUKS mapping open would mean the stick cannot be safely pulled
            # and the next install would collide with a stale mapper name.
            self._teardown(layout, state, plan, dry_run=dry_run, confirm_device=confirm_device)

        media_record = MediaRecord(
            media_id=media_id,
            luks_uuid=layout.luks_uuid,
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

    # ------------------------------------------------------------------
    # The pipeline itself
    # ------------------------------------------------------------------
    def _install(
        self,
        device: BlockDevice,
        config: InstallConfig,
        backend,
        layout: MediaLayout,
        media_id: str,
        user_passphrase: bytes,
        plan: CommandPlan,
        state: "_TeardownState",
        *,
        dry_run: bool,
        confirm_device: str | None,
    ) -> tuple[str, bytes]:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        self._unmount_medium(device, plan, dry_run, confirm_device)

        self.disk_manager.apply(
            plan_partitions(device.path, config.disk),
            dry_run=dry_run,
            confirm_device=confirm_device,
            command_plan=plan,
        )

        self.luks_manager.format(
            layout.system_partition,
            user_passphrase,
            cipher=config.luks.cipher,
            key_size=config.luks.key_size_bits,
            dry_run=dry_run,
            confirm_device=confirm_device,
            plan=plan,
        )

        recovery_code = crypto.generate_recovery_code()
        recovery_salt = crypto.new_salt()
        recovery_passphrase = crypto.derive_recovery_passphrase(recovery_code, recovery_salt)
        self.luks_manager.add_keyslot(
            layout.system_partition,
            user_passphrase,
            recovery_passphrase,
            slot=RECOVERY_KEYSLOT,
            dry_run=dry_run,
            confirm_device=confirm_device,
            plan=plan,
        )

        self.luks_manager.open(
            layout.system_partition,
            layout.mapper_name,
            user_passphrase,
            dry_run=dry_run,
            confirm_device=confirm_device,
            plan=plan,
        )
        state.mapper_opened = True

        # Filesystems. The root filesystem goes *inside* the opened LUKS
        # mapping, which is the step whose absence made every earlier install
        # write the target system into a directory on the admin host instead.
        run(["mkfs.ext4", "-F", "-L", "POA_ROOT", layout.mapper_path])
        run(["mkfs.vfat", "-F", "32", "-n", "POA_ESP", layout.esp_partition])
        run(["mkfs.ext4", "-F", "-L", "POA_BOOT", layout.boot_partition])
        run(["mkfs.ext4", "-F", "-L", "POA_LOGS", layout.logs_partition])
        run(["mkfs.ext4", "-F", "-L", "POA_EXCHANGE", layout.exchange_partition])

        # Mount the target tree, outermost first.
        run(["mkdir", "-p", layout.mount_root])
        run(["mount", layout.mapper_path, layout.mount_root])
        state.mounts.append(layout.mount_root)

        self._run_bootstrap(backend, config, layout, plan, dry_run, confirm_device)

        run(["mkdir", "-p", f"{layout.mount_root}/boot"])
        run(["mount", layout.boot_partition, f"{layout.mount_root}/boot"])
        state.mounts.append(f"{layout.mount_root}/boot")
        run(["mkdir", "-p", f"{layout.mount_root}/boot/efi"])
        run(["mount", layout.esp_partition, f"{layout.mount_root}/boot/efi"])
        state.mounts.append(f"{layout.mount_root}/boot/efi")

        # Pseudo-filesystems, without which apt/dnf and the initramfs tools
        # inside the chroot fail in obscure ways. /run is deliberately NOT
        # bound: it would expose the host's systemd/dbus sockets to the chroot.
        for target, argv in (
            (f"{layout.mount_root}/proc", ["mount", "-t", "proc", "proc", f"{layout.mount_root}/proc"]),
            (f"{layout.mount_root}/sys", ["mount", "-t", "sysfs", "sys", f"{layout.mount_root}/sys"]),
            (f"{layout.mount_root}/dev", ["mount", "--bind", "/dev", f"{layout.mount_root}/dev"]),
            (f"{layout.mount_root}/dev/pts", ["mount", "--bind", "/dev/pts", f"{layout.mount_root}/dev/pts"]),
        ):
            run(["mkdir", "-p", target])
            run(argv)
            state.mounts.append(target)

        # A freshly bootstrapped tree has no resolver configuration, so every
        # package download inside the chroot would fail to resolve a hostname.
        self._write_file(
            f"{layout.mount_root}/etc/resolv.conf", _host_resolv_conf(), plan, dry_run, confirm_device
        )

        temporary_paths = []
        for relative, (content, mode) in backend.temporary_files().items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device, mode=mode)
            temporary_paths.append(target)

        self._probe_uuids(layout, dry_run)

        for relative, content in backend.config_files(config, layout).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)

        run(["mkdir", "-p", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])

        refresh = backend.package_refresh_command(layout.mount_root)
        if refresh is not None:
            run(refresh, timeout=BOOTSTRAP_TIMEOUT)

        boot_pkg_cmd = backend.package_install_command(layout.mount_root, backend.boot_packages(config))
        if boot_pkg_cmd is not None:
            run(boot_pkg_cmd, timeout=BOOTSTRAP_TIMEOUT)

        for argv in backend.chroot_setup_commands(layout.mount_root, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        # crypttab/fstab are in place by now, so the initramfs built here is the
        # one that knows how to unlock the root volume at boot.
        run(backend.initramfs_command(layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)
        for argv in backend.bootloader_commands(layout, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        self._install_recovery_module(
            layout, media_id, recovery_salt, plan, dry_run=dry_run, confirm_device=confirm_device
        )

        if temporary_paths:
            run(["rm", "-f", *temporary_paths])
        run(["sync"])
        return recovery_code, recovery_salt

    def _run_bootstrap(self, backend, config, layout, plan, dry_run, confirm_device) -> None:
        argv = backend.bootstrap_command(layout.mount_root, config)
        self._run(argv, plan, dry_run, confirm_device, timeout=BOOTSTRAP_TIMEOUT)

    def _install_recovery_module(
        self, layout: MediaLayout, media_id: str, recovery_salt: bytes, plan: CommandPlan, *, dry_run, confirm_device
    ) -> None:
        """"Установка.Загрузка": deploys the reference recovery script and its
        metadata onto the unencrypted ESP, where the admin tool can find it
        later without the user's passphrase.

        The script is still not wired into the bootloader/initramfs as an
        automatic recovery entry -- see docs/ARCHITECTURE.md "Known
        limitations"."""
        target_dir = f"{layout.mount_root}/boot/efi/{ESP_METADATA_DIR}"
        self._run(["mkdir", "-p", target_dir], plan, dry_run, confirm_device)

        script_src = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh")
        self._run(["cp", str(script_src), f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device)
        self._write_file(f"{target_dir}/media_id.txt", media_id + "\n", plan, dry_run, confirm_device)
        self._write_file(
            f"{target_dir}/recovery_salt.bin", recovery_salt, plan, dry_run, confirm_device, binary=True
        )

    # ------------------------------------------------------------------
    # Preflight / teardown
    # ------------------------------------------------------------------
    def _unmount_medium(self, device: BlockDevice, plan: CommandPlan, dry_run: bool, confirm_device: str | None) -> None:
        """Desktop automounters mount a stick's partitions the moment it is
        plugged in; partitioning underneath a live filesystem corrupts it, so
        every mountpoint on the device is released first."""
        for mountpoint in device.active_mountpoints:
            self._run(["umount", mountpoint], plan, dry_run, confirm_device)

    def _teardown(
        self, layout: MediaLayout, state: "_TeardownState", plan: CommandPlan, *, dry_run: bool, confirm_device: str | None
    ) -> None:
        """Best-effort release of everything this install opened.

        Deliberately swallows errors: teardown runs on the failure path too,
        where reporting "umount failed" instead of the original cause would
        hide what actually went wrong."""
        for mountpoint in reversed(state.mounts):
            self._try(["umount", "-l", mountpoint], plan, dry_run, confirm_device)
        state.mounts.clear()
        if state.mapper_opened:
            self._try(["cryptsetup", "luksClose", layout.mapper_name], plan, dry_run, confirm_device)
            state.mapper_opened = False
        self._try(["rmdir", layout.mount_root], plan, dry_run, confirm_device)

    def _try(self, argv: list[str], plan: CommandPlan, dry_run: bool, confirm_device: str | None) -> None:
        try:
            self._run(argv, plan, dry_run, confirm_device)
        except Exception:  # noqa: BLE001 - see _teardown docstring
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_arch(self, backend, config: InstallConfig) -> None:
        if not config.arch:
            return
        if backend.normalize_arch(config.arch) != backend.normalize_arch(host_arch()):
            raise InstallError(
                f"архитектура цели ({config.arch}) не совпадает с архитектурой этой машины "
                f"({host_arch()}); установка в чужую архитектуру требует qemu-user-static "
                "и здесь не поддерживается"
            )

    def _run(
        self, argv: list[str], plan: CommandPlan, dry_run: bool, confirm_device: str | None, timeout: int | None = None
    ) -> CommandResult:
        kwargs = {} if timeout is None else {"timeout": timeout}
        return self.runner.run(argv, plan, dry_run=dry_run, confirm_device=confirm_device, **kwargs)

    def _write_file(
        self,
        path: str,
        content,
        plan: CommandPlan,
        dry_run: bool,
        confirm_device: str | None,
        binary: bool = False,
        mode: int | None = None,
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
        open_mode = "wb" if binary else "w"
        with open(path, open_mode) as handle:
            handle.write(content)
        if mode is not None:
            os.chmod(path, mode)

    def _probe_uuids(self, layout: MediaLayout, dry_run: bool) -> None:
        """Fills the layout's UUID fields from the filesystems just created.

        fstab/crypttab must reference UUIDs, not /dev/sdaN: the medium is meant
        to boot on machines where the device ordering is different."""
        if dry_run:
            layout.esp_uuid = str(uuid.uuid4())
            layout.boot_uuid = str(uuid.uuid4())
            layout.root_uuid = str(uuid.uuid4())
            layout.logs_uuid = str(uuid.uuid4())
            layout.exchange_uuid = str(uuid.uuid4())
            layout.luks_uuid = str(uuid.uuid4())
            return

        layout.esp_uuid = _blkid_uuid(layout.esp_partition)
        layout.boot_uuid = _blkid_uuid(layout.boot_partition)
        layout.root_uuid = _blkid_uuid(layout.mapper_path)
        layout.logs_uuid = _blkid_uuid(layout.logs_partition)
        layout.exchange_uuid = _blkid_uuid(layout.exchange_partition)
        layout.luks_uuid = _read_output(LuksManager.uuid_command(layout.system_partition))


@dataclass
class _TeardownState:
    mounts: list[str] = field(default_factory=list)
    mapper_opened: bool = False


def _blkid_uuid(path: str) -> str:
    return _read_output(["blkid", "-s", "UUID", "-o", "value", path])


def _read_output(argv: list[str]) -> str:
    """Read-only probe, so it does not go through CommandRunner's destructive
    gate -- but a failure here still has to stop the install, because a blank
    UUID would produce an fstab that cannot mount anything."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    value = proc.stdout.strip()
    if proc.returncode != 0 or not value:
        raise InstallError(f"не удалось определить UUID: {' '.join(argv)}\n{proc.stderr.strip()}")
    return value


def _host_resolv_conf() -> str:
    """The admin host's working resolver settings, flattened.

    /etc/resolv.conf is a symlink to a systemd-resolved stub on many hosts, so
    the file is read (following the link) rather than copied, and a public
    fallback is used if it turns out to be empty."""
    try:
        with open("/etc/resolv.conf", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        content = ""
    if not any(line.strip().startswith("nameserver") for line in content.splitlines()):
        content = "nameserver 1.1.1.1\nnameserver 8.8.8.8\n"
    return content
