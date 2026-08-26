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
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import crypto
from ..config.schema import InstallConfig
from ..container.models import MediaRecord, RecoveryRecord
from ..errors import ConfigValidationError, InstallError
from ..session import AdminSession
from .backends import get_backend
from .backends.base import (
    EXCHANGE_MOUNTPOINT,
    RECOVERY_DIR,
    SERVICE_KEY_DIR,
    SERVICE_KEY_PATH,
)
from .command_runner import BOOTSTRAP_TIMEOUT, CommandPlan, CommandResult, CommandRunner
from . import distribution
from .devices import BlockDevice, check_install_eligible
from .disk import DiskManager, MediaLayout, build_media_layout, plan_partitions
from .image_source import MountedImage, inspect_mounted
from .luks import LuksManager
from .emulation import probe as probe_emulation
from .tooling import check_tools_available, resolve_argv

MIB = 1024 * 1024
MIN_SYSTEM_PARTITION_MB = 4096
RECOVERY_KEYSLOT = 7
#: Keyslot the day-to-day user password lives in. cryptsetup puts the first
#: passphrase in slot 0; recovery needs to name it to replace it.
USER_KEYSLOT = 0
#: Keyslot on the log/exchange volumes holding the administrator's key. A fixed
#: slot so a medium can be re-keyed later without guessing which one it is.
ADMIN_KEYSLOT = 7

#: Which image family can install which target. Astra Linux is a Debian
#: derivative, so its images look like Debian images.
IMAGE_FAMILY = {"debian": "debian", "astra": "debian", "redos": "redos"}

#: Directory on the (unencrypted) ESP holding the recovery module and the
#: metadata that lets the admin tool recognise this medium later.
ESP_METADATA_DIR = "poa"

#: Resolved once, here, on the main thread. `importlib.resources.files()` does a
#: lazy import of its own machinery on first use, and the install runs in a
#: worker thread -- doing it there raced the interpreter's import lock and
#: aborted the whole process. Resolving a package path has no side effects, so
#: doing it at import time costs nothing.
RECOVERY_MODULE_SRC = str(
    importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh")
)


@dataclass
class InstallResult:
    media_record: MediaRecord
    recovery_record: RecoveryRecord
    plan: CommandPlan
    #: Things that went right but the operator still has to know about --
    #: currently a medium built under emulation, which has to be tried on a
    #: machine of the target architecture before it is handed to anyone.
    warnings: tuple[str, ...] = ()


@dataclass
class DistributionResult:
    """What building a distribution produced.

    The archive and its metadata live in `directory`; `metadata.sha256` is the
    digest an administrator can verify with `sha256sum -c` before deploying.
    """

    directory: str
    metadata: "distribution.DistributionMetadata"
    plan: CommandPlan
    warnings: tuple[str, ...] = ()


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
        self._progress = _Progress(None)
        #: True once anything has been written to the medium. Read by the
        #: interface when a run fails, so it can say whether the stick's former
        #: contents are gone -- several failures (a mismatched image, a device
        #: too small, no emulator for the target architecture) happen before
        #: the first write, and telling someone their medium was erased when it
        #: was not is its own kind of damage.
        self.medium_modified = False

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
        image_path: str = "",
        dry_run: bool = True,
        confirm_device: str | None = None,
        on_step: "Callable[[int, int, str], None] | None" = None,
    ) -> InstallResult:
        """`image_path` is the installation image the system is taken from.

        It is a parameter of the run rather than a field of the configuration:
        the configuration describes *what* to install and is meant to be
        reusable across admin machines, while the path of an ISO belongs to one
        particular machine and would be a dead path anywhere else.

        `on_step(index, total, text)` is called before each stage.

        Installing takes the better part of an hour, most of it inside one
        command, so a caller that shows nothing until the end leaves a person
        watching a still window with no idea whether anything is happening.
        """
        self.session.require_open()
        self._progress = _Progress(on_step)

        # "Конфигурация.Использование.Запрет" / "Установка.Конфигурация.Запрет"
        if config.validation_status != "valid":
            raise ConfigValidationError(
                ["установка запрещена: файл конфигурации не прошёл проверку"]
            )

        backend = get_backend(config.target_os)
        self._source_components: tuple[str, ...] = ("main",)
        self._source_core_packages: tuple[str, ...] = ()
        warnings = self._check_arch(backend, config)
        if not dry_run:
            check_tools_available(backend.required_tools())

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
        image = MountedImage(image_path, mountpoint=f"/mnt/poa-image-{media_id}")
        state.image = image

        try:
            release, source_url = self._prepare_source(image, image_path, config, backend, dry_run=dry_run)
            # Every later package command draws from this same repository, not
            # from whatever the administrator's own machine has configured.
            backend.bind_install_source(
                source_url, release, self._source_core_packages,
                backend.normalize_arch(config.arch),
            )
            recovery_code, recovery_salt, service_key = self._install(
                device, config, backend, layout, media_id, user_passphrase, plan, state,
                image_url=source_url, release=release, dry_run=dry_run, confirm_device=confirm_device,
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
            # Without this key the log and exchange volumes of this medium can
            # never be opened from an administrator machine again -- there is
            # no second copy anywhere.
            service_key_b64=base64.b64encode(service_key).decode("ascii"),
            logs_luks_uuid=layout.logs_luks_uuid,
            exchange_luks_uuid=layout.exchange_luks_uuid,
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

        return InstallResult(
            media_record=media_record,
            recovery_record=recovery_record,
            plan=plan,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # Building a distribution once, deploying it many times
    # ------------------------------------------------------------------
    def build_distribution(
        self,
        config: InstallConfig,
        destination: str,
        *,
        image_path: str = "",
        dry_run: bool = True,
        on_step: "Callable[[int, int, str], None] | None" = None,
    ) -> "DistributionResult":
        """Builds the system once, into an archive, without touching a medium.

        This is the long half of the work -- unpacking packages and running
        their configuration scripts -- and it is exactly the half that does not
        differ between ten identical sticks. Doing it here, into a directory on
        the administrator's own disk, means it is done once and it is fast:
        the medium is USB flash behind dm-crypt, and this is not.

        Nothing is erased and no device is opened, so this operation is safe to
        run at any time. What it produces is a build artefact: it holds no
        keys, no passphrase and no recovery data -- see distribution.py.
        """
        self.session.require_open()
        self._progress = _Progress(on_step, expected=_Progress.BUILD_STAGES)

        if config.validation_status != "valid":
            raise ConfigValidationError(
                ["сборка запрещена: файл конфигурации не прошёл проверку"]
            )

        backend = get_backend(config.target_os)
        self._source_components = ("main",)
        self._source_core_packages = ()
        warnings = self._check_arch(backend, config)
        if not dry_run:
            # Сборке нужен упаковщик, а не распаковщик: носителя здесь нет.
            check_tools_available([*backend.required_tools(), "mksquashfs"])

        build_id = crypto.new_media_id()
        root = os.path.join(destination, f"rootfs-{build_id}")
        plan = CommandPlan(device=root)
        state = _TeardownState()
        image = MountedImage(image_path, mountpoint=f"/mnt/poa-image-{build_id}")
        state.image = image

        try:
            release, source_url = self._prepare_source(image, image_path, config, backend, dry_run=dry_run)
            backend.bind_install_source(
                source_url, release, self._source_core_packages,
                backend.normalize_arch(config.arch),
            )
            metadata = self._build_root(
                config, backend, root, destination, plan, state,
                image_url=source_url, release=release, dry_run=dry_run,
                emulated=bool(warnings),
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below
            self._teardown_build(root, state, plan, dry_run=dry_run)
            if isinstance(exc, InstallError):
                raise
            raise InstallError(f"сборка дистрибутива прервана: {exc}") from exc
        else:
            self._teardown_build(root, state, plan, dry_run=dry_run)

        return DistributionResult(
            directory=destination, metadata=metadata, plan=plan, warnings=warnings
        )

    def _build_root(
        self,
        config: InstallConfig,
        backend,
        root: str,
        destination: str,
        plan: CommandPlan,
        state: "_TeardownState",
        *,
        image_url: str,
        release: str,
        dry_run: bool,
        emulated: bool,
    ) -> "distribution.DistributionMetadata":
        # The runner refuses to act unless the caller names the target it means
        # to write to -- the check that keeps an install from landing on the
        # wrong disk. Here the target is the build directory, so that is what
        # is confirmed: the guard stays in force rather than being bypassed.
        confirm = root
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm, **kw)  # noqa: E731

        self._progress.step("Подготовка каталога сборки")
        run(["mkdir", "-p", root])

        self._progress.step("Развёртывание базовой системы с образа")
        argv = backend.bootstrap_command(root, config, image_url, release)
        run(argv, timeout=BOOTSTRAP_TIMEOUT)

        # A bootstrapper that exits 0 having produced nothing is a real
        # possibility -- dnf does exactly that when the transaction resolves to
        # an empty set -- and the failure then surfaces two steps later as a
        # missing /etc/resolv.conf, which says nothing about the cause. Checked
        # here, while the command responsible is still the obvious suspect.
        if not dry_run and not os.path.isdir(os.path.join(root, "etc")):
            raise InstallError(
                "развёртывание базовой системы завершилось без ошибки, но система не "
                f"создана: в {root} нет каталога etc.\n"
                f"Команда: {' '.join(argv)}\n"
                "Обычно это значит, что репозиторий образа не содержит запрошенных "
                "пакетов для выбранной архитектуры."
            )

        self._mount_pseudo_filesystems(root, state, plan, dry_run, confirm)
        self._bind_image_into(root, image_url, state, plan, dry_run, confirm)
        self._write_install_time_sources(root, backend, release, image_url, plan, dry_run, confirm)

        temporary_paths = []
        for relative, (content, mode) in backend.temporary_files().items():
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm, mode=mode)
            temporary_paths.append(target)

        # Scripts and units that the chroot setup below is about to run. They
        # are identical on every medium made from this distribution, so they
        # belong to the build and not to the deployment -- and leaving them out
        # is not a cosmetic omission: chroot_setup_commands calls
        # /usr/local/sbin/poa-kiosk-apply, so a build without them dies at the
        # account-and-services step with "no such file", after the base system
        # and the whole desktop have already been installed.
        for relative, (content, mode) in backend.scripted_files(config).items():
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm, mode=mode)

        self._install_packages_and_settings(
            config, backend, root, image_url, plan, dry_run, confirm
        )

        # Package sources for the finished system, written now because the
        # image is still mounted and a backend may read settings off it.
        image_mountpoint = image_url[len("file://"):] if image_url.startswith("file://") else ""
        sources = backend.sources_list(config, release, image_mountpoint)
        if sources is not None:
            target = f"{root}/{backend.sources_path}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, sources, plan, dry_run, confirm)

        if temporary_paths:
            run(["rm", "-f", *temporary_paths])
        cleanup = backend.package_cleanup_command(root)
        if cleanup is not None:
            try:
                run(cleanup, timeout=BOOTSTRAP_TIMEOUT)
            except Exception:  # noqa: BLE001 - a full cache is not a failure
                pass

        # Everything medium-specific comes out before the archive is made, so
        # the artefact cannot carry a key or an identity. This is what makes
        # ten media from one distribution ten systems rather than one.
        self._progress.step("Удаление из образа всего, что уникально для носителя")
        self._unmount_all(state, plan, dry_run, confirm)
        for argv in distribution.sanitise_commands(root):
            self._try(argv, plan, dry_run, confirm)

        self._progress.step("Упаковка дистрибутива")
        compressor, archive_name = _pick_compressor()
        archive = os.path.join(destination, archive_name)
        run(distribution.archive_command(root, archive, compressor=compressor), timeout=BOOTSTRAP_TIMEOUT)

        rootfs_bytes = _directory_size(root) if not dry_run else 0
        metadata = distribution.DistributionMetadata.for_config(
            config,
            release=release,
            arch=backend.normalize_arch(config.arch),
            rootfs_bytes=rootfs_bytes,
            emulated=emulated,
            archive=archive_name,
        )

        self._progress.step("Подсчёт контрольной суммы")
        if not dry_run:
            _, digest = distribution.write_checksum(archive)
            metadata.sha256 = digest
            distribution.write_metadata(destination, metadata)
        return metadata

    def deploy_distribution(
        self,
        device: BlockDevice,
        directory: str,
        user_passphrase: bytes,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        on_step: "Callable[[int, int, str], None] | None" = None,
    ) -> InstallResult:
        """Writes a built distribution onto one medium, with its own encryption.

        The short half of the work, and the only half that has to be repeated
        per stick. Everything unique to this medium is created here and now:
        the LUKS master key, the passphrase, the recovery code and its salt,
        the service key, the media id and the filesystem UUIDs.

        The distribution is verified against its recorded checksum **before**
        the medium is touched, so a damaged archive costs a message rather than
        an erased stick and an hour of waiting.
        """
        self.session.require_open()
        self._progress = _Progress(on_step, expected=_Progress.DEPLOY_STAGES)

        self._progress.step("Проверка дистрибутива")
        metadata = distribution.read_metadata(directory)
        config = InstallConfig.from_dict(metadata.config)
        backend = get_backend(config.target_os)
        warnings = self._check_arch(backend, config)
        if not dry_run:
            unpacking = ["mount", "umount", "cp"] if distribution.is_squashfs(metadata.archive) else ["tar"]
            check_tools_available([*backend.required_tools(), *unpacking])
            # Before anything is erased. This is the whole point of doing it
            # here rather than discovering a truncated archive mid-unpack.
            distribution.verify_archive(directory, metadata)

        self.check_possible(device, config, require_unmounted=False)

        media_id = crypto.new_media_id()
        layout = build_media_layout(
            plan_partitions(device.path, config.disk),
            mapper_name=f"poa-{media_id}",
            mount_root=f"/mnt/poa-install-{media_id}",
        )
        plan = CommandPlan(device=device.path)
        state = _TeardownState()

        try:
            recovery_code, recovery_salt, service_key = self._deploy(
                device, config, backend, layout, media_id, user_passphrase, plan, state,
                directory=directory, metadata=metadata,
                dry_run=dry_run, confirm_device=confirm_device,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below
            self._teardown(layout, state, plan, dry_run=dry_run, confirm_device=confirm_device)
            if isinstance(exc, InstallError):
                raise
            raise InstallError(f"установка прервана: {exc}") from exc
        else:
            self._teardown(layout, state, plan, dry_run=dry_run, confirm_device=confirm_device)

        media_record = MediaRecord(
            media_id=media_id,
            luks_uuid=layout.luks_uuid,
            label=device.model or device.path,
            config_name=config.name,
            target_os=config.target_os,
            service_key_b64=base64.b64encode(service_key).decode("ascii"),
            logs_luks_uuid=layout.logs_luks_uuid,
            exchange_luks_uuid=layout.exchange_luks_uuid,
        )
        recovery_record = RecoveryRecord(
            media_id=media_id,
            slot=RECOVERY_KEYSLOT,
            code=recovery_code,
            salt_b64=base64.b64encode(recovery_salt).decode("ascii"),
        )
        self.session.container.add_media_record(media_record)
        self.session.container.add_recovery_record(recovery_record)

        return InstallResult(
            media_record=media_record,
            recovery_record=recovery_record,
            plan=plan,
            warnings=warnings,
        )

    def _deploy(
        self, device, config, backend, layout, media_id, user_passphrase, plan, state,
        *, directory, metadata, dry_run, confirm_device,
    ) -> tuple[str, bytes, bytes]:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        recovery_code, recovery_salt, service_key, boot_key = self._prepare_medium(
            device, config, layout, user_passphrase, plan, state, dry_run, confirm_device
        )

        self._progress.step("Копирование системы на носитель")
        archive = distribution.archive_path(directory, metadata)
        if distribution.is_squashfs(metadata.archive):
            # Эталон монтируется только на чтение, и система копируется из него
            # в уже открытый том. Копия сохраняет владельцев, права, ACL и
            # xattr — без последних перестают работать программы с
            # capabilities, и понять почему по симптомам трудно.
            source = f"/mnt/poa-dist-{media_id}"
            run(["mkdir", "-p", source])
            run(distribution.mount_command(archive, source))
            state.mounts.append(source)
            run(distribution.copy_command(source, layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)
        else:
            # Дистрибутив версии 1: корень лежит в tar-архиве.
            run(distribution.unpack_command(archive, layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)

        # Копирование считается законченным только когда данные на носителе, а
        # не в кэше страниц. Без этого следующая же команда — чтение UUID —
        # встаёт в очередь за гигабайтами записанного и упирается в таймаут.
        self._progress.step("Сброс кэшей на носитель")
        run(["sync"], timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Подключение служебных разделов")
        self._mount_boot_partitions(layout, state, plan, dry_run, confirm_device)
        self._mount_pseudo_filesystems(layout.mount_root, state, plan, dry_run, confirm_device)

        self._progress.step("Настройка носителя")
        self._write_medium_identity(
            config, backend, layout, media_id, recovery_salt, boot_key, service_key,
            plan, state, dry_run, confirm_device,
        )

        self._progress.step("Сборка initramfs с разблокировкой диска")
        run(backend.initramfs_command(layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Установка загрузчика на носитель")
        for argv in backend.bootloader_commands(layout, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Запись модуля восстановления")
        self._install_recovery_module(
            layout, media_id, recovery_salt, plan, dry_run=dry_run, confirm_device=confirm_device
        )

        self._progress.step("Завершение записи и отключение носителя")
        run(["sync"])
        return recovery_code, recovery_salt, service_key

    def _write_medium_identity(
        self, config, backend, layout, media_id, recovery_salt, boot_key, service_key,
        plan, state, dry_run, confirm_device,
    ) -> None:
        """Everything that makes this medium this medium and not another one."""
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        self._probe_uuids(layout, dry_run)
        for relative, content in backend.config_files(config, layout).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)

        run(["mkdir", "-p", f"{layout.mount_root}/{SERVICE_KEY_DIR}"])
        self._write_file(
            f"{layout.mount_root}/{SERVICE_KEY_PATH}", boot_key, plan, dry_run, confirm_device,
            binary=True, mode=0o400,
        )

        run(["mkdir", "-p", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])
        run(["mount", layout.exchange_mapper_path, f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])
        state.mounts.append(f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}")
        run(["chmod", "1777", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])

        # Before the initramfs is built, never after: these are what the
        # rebuild copies into the image.
        self._install_recovery_boot(
            backend, layout, config, media_id, recovery_salt, plan,
            dry_run=dry_run, confirm_device=confirm_device,
        )

    def _mount_boot_partitions(self, layout, state, plan, dry_run, confirm_device) -> None:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        run(["mkdir", "-p", f"{layout.mount_root}/boot"])
        run(["mount", layout.boot_partition, f"{layout.mount_root}/boot"])
        state.mounts.append(f"{layout.mount_root}/boot")
        run(["mkdir", "-p", f"{layout.mount_root}/boot/efi"])
        run(["mount", layout.esp_partition, f"{layout.mount_root}/boot/efi"])
        state.mounts.append(f"{layout.mount_root}/boot/efi")

    def _install_packages_and_settings(
        self, config, backend, root, image_url, plan, dry_run, confirm_device
    ) -> None:
        """Kernel, bootloader tools, desktop, firmware, account and kiosk.

        Shared by the one-shot install and the distribution build: it is the
        part that is identical on every medium, which is precisely why it is
        worth doing once.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        refresh = backend.package_refresh_command(root)
        if refresh is not None:
            run(refresh, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Установка ядра и загрузчика")
        boot_pkg_cmd = backend.package_install_command(root, backend.boot_packages(config))
        if boot_pkg_cmd is not None:
            run(boot_pkg_cmd, timeout=BOOTSTRAP_TIMEOUT)

        desktop_packages = backend.desktop_packages(config)
        if desktop_packages:
            self._progress.step("Установка графического окружения")
            desktop_cmd = backend.package_install_command(root, desktop_packages)
            if desktop_cmd is not None:
                run(desktop_cmd, timeout=BOOTSTRAP_TIMEOUT)
            if config.kiosk.firejail:
                optional = backend.optional_package_command(root, ["firejail"])
                if optional is not None:
                    run(optional, timeout=BOOTSTRAP_TIMEOUT)

        image_dir = image_url[len("file://"):] if image_url.startswith("file://") else ""
        firmware_cmd = backend.firmware_install_command(root, image_dir)
        if firmware_cmd is not None:
            self._progress.step("Установка прошивок устройств")
            run(firmware_cmd, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Создание учётной записи и служб")
        for argv in backend.chroot_setup_commands(root, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

    def _mount_pseudo_filesystems(self, root, state, plan, dry_run, confirm_device) -> None:
        """/proc, /sys, /dev inside the target.

        Without them apt and the initramfs tools fail in obscure ways. /run is
        deliberately not bound: it would expose this machine's systemd and dbus
        sockets to the chroot.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        for target, argv in (
            (f"{root}/proc", ["mount", "-t", "proc", "proc", f"{root}/proc"]),
            (f"{root}/sys", ["mount", "-t", "sysfs", "sys", f"{root}/sys"]),
            (f"{root}/dev", ["mount", "--bind", "/dev", f"{root}/dev"]),
            (f"{root}/dev/pts", ["mount", "--bind", "/dev/pts", f"{root}/dev/pts"]),
        ):
            run(["mkdir", "-p", target])
            run(argv)
            state.mounts.append(target)

    def _bind_image_into(self, root, image_url, state, plan, dry_run, confirm_device) -> None:
        """Makes the image visible inside the chroot at the same path.

        debootstrap writes the target's sources.list pointing at
        file:///mnt/poa-image-<id>, a path that exists only out here.
        """
        if not image_url.startswith("file://"):
            return
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        inside = image_url[len("file://"):]
        target = f"{root}{inside}"
        run(["mkdir", "-p", target])
        run(["mount", "--bind", inside, target])
        state.mounts.append(target)

    def _write_install_time_sources(
        self, root, backend, release, image_url, plan, dry_run, confirm_device
    ) -> None:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        sources = backend.install_time_sources(release, image_url, self._source_components)
        if sources is not None:
            relative, content = sources
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)
        # A freshly bootstrapped tree has no usable resolver configuration.
        #
        # Two things have to happen before it can be written, and both were
        # learned from failures rather than guessed:
        #
        # `mkdir` because debootstrap always leaves /etc behind and dnf does
        # not necessarily -- that difference should not decide whether this
        # works.
        #
        # `rm -f` because on the RHEL family /etc/resolv.conf is a *symlink* to
        # /run/systemd/resolve/stub-resolv.conf, which does not exist in a root
        # nothing has booted yet. Opening it for writing follows the link and
        # fails with "no such file or directory" -- pointing at /etc/resolv.conf
        # while /etc is plainly there, which is as misleading as an error gets.
        run(["mkdir", "-p", f"{root}/etc"])
        run(["rm", "-f", f"{root}/etc/resolv.conf"])
        self._write_file(f"{root}/etc/resolv.conf", _host_resolv_conf(), plan, dry_run, confirm_device)

    def _unmount_all(self, state: "_TeardownState", plan, dry_run, confirm_device) -> None:
        for mountpoint in reversed(state.mounts):
            self._try(["umount", "-l", mountpoint], plan, dry_run, confirm_device)
        state.mounts.clear()

    def _teardown_build(self, root: str, state: "_TeardownState", plan, *, dry_run: bool) -> None:
        self._unmount_all(state, plan, dry_run, root)
        if state.image is not None:
            state.image.unmount()
            state.image = None
        # The unpacked root is an intermediate: the distribution is the archive.
        self._try(["rm", "-rf", root], plan, dry_run, root)

    # ------------------------------------------------------------------
    # The installation image
    # ------------------------------------------------------------------
    def _prepare_source(
        self, image: MountedImage, image_path: str, config: InstallConfig, backend, *, dry_run: bool
    ) -> tuple[str, str]:
        """Decides where the base system comes from, and reports the release
        together with the repository URL to install from.

        A local image is the intended source and the recommended one: the tool
        is meant for networks with no route to the internet, and an image on
        disk is a fixed, inspectable thing. A network mirror is supported as
        the fallback, for a machine that does have a repository available and
        no image at hand -- it is a choice the administrator makes explicitly
        by leaving the image empty and configuring a mirror, never a silent
        substitution when the image turns out to be missing.

        With an image, the release comes from the image rather than from the
        configuration: that keeps configurations free of distribution version
        numbers, so the same configuration works with next year's image
        without being edited. Everything checked here would otherwise surface
        as a failure deep inside debootstrap or dnf, tens of minutes in, as a
        complaint about a missing Release file rather than about the image
        being the wrong one.
        """
        if dry_run:
            self._progress.step("Подключение установочного образа")
            return config.release, image.url if image_path else config.mirror

        if not image_path:
            if not config.mirror:
                raise InstallError(
                    "не указан установочный образ ОС. Установка возможна либо с локального "
                    "образа (рекомендуется), либо из сетевого репозитория — но тогда в "
                    "конфигурации должен быть задан адрес репозитория."
                )
            self._progress.step(f"Установка из сетевого репозитория {config.mirror}")
            # No image means nothing to inspect, so the release can only come
            # from the configuration here -- the one case where a configuration
            # does have to name a version.
            if not config.release:
                raise InstallError(
                    "для установки из сети в конфигурации должен быть указан выпуск "
                    "дистрибутива: без образа его неоткуда взять"
                )
            return config.release, config.mirror

        self._progress.step("Подключение установочного образа")
        image.mount()
        info = inspect_mounted(image_path, image.mountpoint)

        expected_family = IMAGE_FAMILY.get(config.target_os, config.target_os)
        if info.family != expected_family:
            raise InstallError(
                f"образ не подходит: конфигурация рассчитана на {config.target_os}, "
                f"а образ содержит {info.family} ({info.label})"
            )
        if not info.complete:
            raise InstallError(f"образ не подходит: {info.incomplete_reason}")
        self._validate_against_image(config, image.mountpoint, info)
        # Kept for the install-time sources written into the chroot further
        # down, which need the components the image actually publishes.
        self._source_components = info.components
        # The image's own idea of what its base system is, preferred over any
        # list written into this program -- see ImageInfo.core_packages.
        self._source_core_packages = info.core_packages
        wanted_arch = backend.normalize_arch(config.arch)
        if info.architectures and wanted_arch not in info.architectures:
            raise InstallError(
                f"образ не подходит: эта машина ставит систему для {wanted_arch}, "
                f"а образ собран для {', '.join(info.architectures)}"
            )
        return info.release or config.release, image.url

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
        image_url: str,
        release: str,
        dry_run: bool,
        confirm_device: str | None,
    ) -> tuple[str, bytes, bytes]:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        recovery_code, recovery_salt, service_key, boot_key = self._prepare_medium(
            device, config, layout, user_passphrase, plan, state, dry_run, confirm_device
        )

        self._progress.step("Развёртывание базовой системы с образа")
        self._run_bootstrap(backend, config, layout, image_url, release, plan, dry_run, confirm_device)

        self._progress.step("Подключение служебных разделов")
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

        # The image, visible from inside the chroot at the same path it has
        # outside it.
        #
        # debootstrap writes the target's /etc/apt/sources.list pointing at the
        # URL it was given -- file:///mnt/poa-image-<id> -- and that path exists
        # only on this machine. Without this bind mount `apt-get update` inside
        # the chroot finds nothing and every package install fails with
        # "Unable to locate package linux-image-amd64", which is to say the
        # medium gets no kernel, no bootloader and no way to unlock itself.
        # Found by running an install from a local image to the end for the
        # first time; the earlier successful run had used a network mirror,
        # where the URL means the same thing on both sides.
        if image_url.startswith("file://"):
            image_inside = image_url[len("file://"):]
            target = f"{layout.mount_root}{image_inside}"
            run(["mkdir", "-p", target])
            run(["mount", "--bind", image_inside, target])
            state.mounts.append(target)

        # Where the target's package manager looks while it is being built.
        # debootstrap leaves /etc/apt/sources.list empty for a file:// mirror
        # -- checked on the medium afterwards -- so without this apt in the
        # chroot has no repository at all and cannot install a kernel.
        sources = backend.install_time_sources(release, image_url, self._source_components)
        if sources is not None:
            relative, content = sources
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)

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

        self._progress.step("Настройка устанавливаемой системы")
        self._probe_uuids(layout, dry_run)

        for relative, content in backend.config_files(config, layout).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)

        # Scripts and units, which carry a mode of their own. Written 0644 they
        # would be present, correct and inert -- the kiosk whitelist would
        # never be applied and the update service would never start.
        for relative, (content, mode) in backend.scripted_files(config, layout).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device, mode=mode)

        # The key file crypttab uses to open the log and exchange volumes at
        # boot. It sits on the encrypted root, so it is only reachable once the
        # root itself has been unlocked with the user's passphrase; 0400 and
        # root-owned, because nothing in the running system needs to read it
        # except systemd-cryptsetup.
        run(["mkdir", "-p", f"{layout.mount_root}/{SERVICE_KEY_DIR}"])
        self._write_file(
            f"{layout.mount_root}/{SERVICE_KEY_PATH}",
            boot_key,
            plan,
            dry_run,
            confirm_device,
            binary=True,
            mode=0o400,
        )

        # Mounted here and not only at boot, so the volume's root directory can
        # be made writable for whoever ends up using the portable system. 1777
        # (like /tmp) rather than an owner: the account is created later, in the
        # chroot, and its uid is not known at this point. The volume is
        # encrypted, and inside a booted portable system there is exactly one
        # user, so a permissive mode there costs nothing.
        run(["mkdir", "-p", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])
        run(["mount", layout.exchange_mapper_path, f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])
        state.mounts.append(f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}")
        run(["chmod", "1777", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])

        refresh = backend.package_refresh_command(layout.mount_root)
        if refresh is not None:
            run(refresh, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Установка ядра и загрузчика")
        boot_pkg_cmd = backend.package_install_command(layout.mount_root, backend.boot_packages(config))
        if boot_pkg_cmd is not None:
            run(boot_pkg_cmd, timeout=BOOTSTRAP_TIMEOUT)

        # A step of its own, and named as a long one, because it is: X, its
        # driver metapackages and MATE are several hundred megabytes on top of
        # the base system. Folded into the account/services step below it would
        # be tens of minutes during which the interface names the wrong thing.
        desktop_packages = backend.desktop_packages(config)
        if desktop_packages:
            self._progress.step("Установка графического окружения")
            desktop_cmd = backend.package_install_command(layout.mount_root, desktop_packages)
            if desktop_cmd is not None:
                run(desktop_cmd, timeout=BOOTSTRAP_TIMEOUT)
            # firejail is an extra layer, not a requirement: it is present in
            # the RED OS image and absent from the Debian DVD. Installed by a
            # command that is allowed to fail, so asking for the sandbox on an
            # image that has no such package costs a line in the log rather
            # than an install that dies with the medium half-written.
            if config.kiosk.firejail:
                optional = backend.optional_package_command(layout.mount_root, ["firejail"])
                if optional is not None:
                    run(optional, timeout=BOOTSTRAP_TIMEOUT)

            # Device firmware the image carries outside its package index.
            # Without it a medium built from a Debian image reaches unfamiliar
            # hardware with no wireless and no graphics acceleration -- which
            # for a system whose whole point is being carried elsewhere is the
            # difference between working and not.
            image_dir = image_url[len("file://"):] if image_url.startswith("file://") else ""
            firmware_cmd = backend.firmware_install_command(layout.mount_root, image_dir)
            if firmware_cmd is not None:
                self._progress.step("Установка прошивок устройств")
                run(firmware_cmd, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Создание учётной записи и служб")
        for argv in backend.chroot_setup_commands(layout.mount_root, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        # Before the initramfs is built, never after: the hooks below are what
        # copy the recovery flow *into* that image, so writing them afterwards
        # would leave a medium that boots without any recovery at all.
        self._progress.step("Подключение модуля восстановления к загрузке")
        self._install_recovery_boot(
            backend, layout, config, media_id, recovery_salt, plan, dry_run=dry_run, confirm_device=confirm_device
        )

        self._progress.step("Сборка initramfs с разблокировкой диска")
        # crypttab/fstab are in place by now, so the initramfs built here is the
        # one that knows how to unlock the root volume at boot.
        run(backend.initramfs_command(layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Установка загрузчика на носитель")
        for argv in backend.bootloader_commands(layout, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Запись модуля восстановления")
        self._install_recovery_module(
            layout, media_id, recovery_salt, plan, dry_run=dry_run, confirm_device=confirm_device
        )

        # The image is mounted here, on this machine, and will not exist once the
        # medium is unplugged. Leaving it in the installed system's sources means
        # every apt command there fails on a dead file:// path.
        # The image is still mounted at this point, so a backend can read its
        # own repository settings off it rather than this project writing down
        # values that are right for one release and wrong for the next.
        image_mountpoint = image_url[len("file://"):] if image_url.startswith("file://") else ""
        sources = backend.sources_list(config, release, image_mountpoint)
        if sources is not None:
            target = f"{layout.mount_root}/{backend.sources_path}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, sources, plan, dry_run, confirm_device)

        self._progress.step("Завершение записи и отключение носителя")
        if temporary_paths:
            run(["rm", "-f", *temporary_paths])

        # Last, after every package command has run: the download cache is
        # otherwise carried on the medium for the rest of its life. Allowed to
        # fail -- a medium with a full cache works, it is just needlessly
        # smaller, and nothing here is worth failing a finished install over.
        cleanup = backend.package_cleanup_command(layout.mount_root)
        if cleanup is not None:
            try:
                run(cleanup, timeout=BOOTSTRAP_TIMEOUT)
            except Exception:  # noqa: BLE001 - see above
                pass

        # The resolver configuration was copied from this machine so the chroot
        # could resolve names while being built. On the medium it points at
        # whatever DNS this administrator's network uses, which is neither
        # correct nor anyone else's business.
        self._write_file(
            f"{layout.mount_root}/etc/resolv.conf",
            "# generated by portable-os-admin\n"
            "# Адрес сервера имён на носителе не фиксируется: его выдаёт сеть,\n"
            "# в которую систему принесут (через NetworkManager/systemd-resolved).\n",
            plan,
            dry_run,
            confirm_device,
        )
        run(["sync"])
        return recovery_code, recovery_salt, service_key


    def _prepare_medium(
        self, device, config, layout, user_passphrase, plan, state, dry_run, confirm_device
    ) -> tuple[str, bytes, bytes, bytes]:
        """Erases the medium and builds its encrypted, empty structure.

        Shared by the one-shot install and by deploying a distribution,
        because it is the part that must happen *per medium*: the LUKS
        master key is generated here, and generating it here is what makes
        two media from one distribution two systems rather than copies of
        one. Returns the recovery code, its salt, the administrator's
        service key and the contents of the boot key file.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        self._progress.step("Отключение носителя от системы")
        self._unmount_medium(device, plan, dry_run, confirm_device)

        self._progress.step("Очистка носителя и создание разделов")
        # Set immediately before the first destructive command, not after: if
        # partitioning itself fails halfway, the medium has still been written
        # to and saying otherwise would be worse than saying nothing.
        self.medium_modified = not dry_run
        self.disk_manager.apply(
            plan_partitions(device.path, config.disk),
            dry_run=dry_run,
            confirm_device=confirm_device,
            command_plan=plan,
            # Whatever the medium held before is erased signature by signature,
            # not merely overwritten by a new partition table -- see
            # DiskManager.commands_for.
            existing_partitions=device.partition_paths,
        )

        self._progress.step("Шифрование системного раздела (LUKS)")
        self.luks_manager.format(
            layout.system_partition,
            user_passphrase,
            cipher=config.luks.cipher,
            key_size=config.luks.key_size_bits,
            kdf=config.luks.kdf,
            dry_run=dry_run,
            confirm_device=confirm_device,
            plan=plan,
        )

        self._progress.step("Создание ключа восстановления")
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

        self._progress.step("Открытие зашифрованного тома")
        self.luks_manager.open(
            layout.system_partition,
            layout.mapper_name,
            user_passphrase,
            dry_run=dry_run,
            confirm_device=confirm_device,
            plan=plan,
        )
        state.mapper_opened = True

        # The log and exchange volumes. Two keys, and the difference between
        # them is the whole design:
        #
        #   boot_key    -- a key *file* placed on the encrypted root. The
        #                  portable system opens both volumes with it at boot,
        #                  so the user sees the exchange folder as an ordinary
        #                  directory and never types a second passphrase. It is
        #                  reachable only after the root itself was unlocked.
        #   service_key -- kept in the administrator container and nowhere else.
        #                  It lets the administrator read the logs and the
        #                  exchange volume on their own machine without the
        #                  user's LUKS passphrase.
        #
        # A stolen medium yields neither, so it is ciphertext to an outsider.
        # It is deliberately not proof against the medium's own user, who holds
        # the LUKS passphrase -- that was the chosen threat model, and claiming
        # more would be false.
        self._progress.step("Шифрование разделов журналов и обмена")
        service_key = crypto.new_volume_key()
        boot_key = crypto.new_volume_key()
        for partition, mapper in (
            (layout.logs_partition, layout.logs_mapper_name),
            (layout.exchange_partition, layout.exchange_mapper_name),
        ):
            self.luks_manager.format(
                partition,
                boot_key,
                cipher=config.luks.cipher,
                key_size=config.luks.key_size_bits,
                kdf=config.luks.kdf,
                dry_run=dry_run,
                confirm_device=confirm_device,
                plan=plan,
            )
            self.luks_manager.add_keyslot(
                partition,
                boot_key,
                service_key,
                slot=ADMIN_KEYSLOT,
                dry_run=dry_run,
                confirm_device=confirm_device,
                plan=plan,
            )
            self.luks_manager.open(
                partition, mapper, boot_key, dry_run=dry_run, confirm_device=confirm_device, plan=plan
            )
            state.service_mappers.append(mapper)

        # Filesystems. The root filesystem goes *inside* the opened LUKS
        # mapping, which is the step whose absence made every earlier install
        # write the target system into a directory on the admin host instead.
        self._progress.step("Создание файловых систем")
        # `-E lazy_itable_init=0,lazy_journal_init=0` намеренно НЕ ставится: на
        # медленном носителе это заставило бы mkfs записать все таблицы inode
        # сразу, а ленивая инициализация как раз экономит запись.
        run(["mkfs.ext4", "-F", "-L", "POA_ROOT", layout.mapper_path])
        run(["mkfs.vfat", "-F", "32", "-n", "POA_ESP", layout.esp_partition])
        run(["mkfs.ext4", "-F", "-L", "POA_BOOT", layout.boot_partition])
        run(["mkfs.ext4", "-F", "-L", "POA_LOGS", layout.logs_mapper_path])
        run(["mkfs.ext4", "-F", "-L", "POA_EXCHANGE", layout.exchange_mapper_path])

        self._progress.step("Монтирование носителя")
        # Mount the target tree, outermost first.
        run(["mkdir", "-p", layout.mount_root])
        # Параметры монтирования — только на время записи; в fstab носителя
        # попадают другие. `noatime` убирает запись метки времени на каждое
        # чтение, `commit=600` заставляет журнал сбрасываться раз в десять
        # минут вместо пяти секунд, `barrier=0` снимает принудительный сброс
        # кэша устройства после каждой транзакции. Всё это безопасно ровно
        # потому, что незавершённая установка и так не годится к
        # использованию: терять при сбое питания тут нечего, а выигрыш на
        # дешёвой флешке измеримый.
        run([
            "mount", "-o", "noatime,commit=600,barrier=0",
            layout.mapper_path, layout.mount_root,
        ])
        state.mounts.append(layout.mount_root)

        return recovery_code, recovery_salt, service_key, boot_key

    def _run_bootstrap(self, backend, config, layout, image_url, release, plan, dry_run, confirm_device) -> None:
        argv = backend.bootstrap_command(layout.mount_root, config, image_url, release)
        self._run(argv, plan, dry_run, confirm_device, timeout=BOOTSTRAP_TIMEOUT)

    def _install_recovery_boot(
        self, backend, layout: MediaLayout, config: InstallConfig, media_id: str, recovery_salt: bytes,
        plan: CommandPlan, *, dry_run, confirm_device,
    ) -> None:
        """Puts the recovery flow where the boot environment can reach it.

        The module, its salt and its settings go onto the encrypted root; the
        family-specific hooks copy them into the initramfs when it is built.
        That is the only place they can be useful, because recovery happens
        before the root has been unlocked -- and it is why the medium keeps no
        second copy of the salt anywhere an attacker could read it.
        """
        target_dir = f"{layout.mount_root}/{RECOVERY_DIR}"
        self._run(["mkdir", "-p", target_dir], plan, dry_run, confirm_device)
        self._run(["cp", RECOVERY_MODULE_SRC, f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device)
        self._run(["chmod", "0755", f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device)
        self._write_file(
            f"{target_dir}/recovery_salt.bin", recovery_salt, plan, dry_run, confirm_device,
            binary=True, mode=0o400,
        )
        # Everything the module needs to know about this particular medium.
        # The device is named by UUID: the medium boots on machines where the
        # disk ordering is different, and /dev/sda2 would be someone else's.
        self._write_file(
            f"{target_dir}/env",
            (
                "# generated by portable-os-admin\n"
                f"POA_MEDIA_ID={media_id}\n"
                f"POA_LUKS_UUID={layout.luks_uuid}\n"
                f"POA_RECOVERY_SLOT={RECOVERY_KEYSLOT}\n"
                f"POA_USER_SLOT={USER_KEYSLOT}\n"
                f"POA_SCRYPT_N={crypto.RECOVERY_SCRYPT_N}\n"
                f"POA_SCRYPT_R={crypto.SCRYPT_R}\n"
                f"POA_SCRYPT_P={crypto.SCRYPT_P}\n"
            ),
            plan, dry_run, confirm_device, mode=0o400,
        )

        for relative, (content, mode) in backend.recovery_boot_files(layout, config).items():
            path = f"{layout.mount_root}/{relative}"
            self._run(["mkdir", "-p", path.rsplit("/", 1)[0]], plan, dry_run, confirm_device)
            self._write_file(path, content, plan, dry_run, confirm_device, mode=mode)

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

        self._run(
            ["cp", RECOVERY_MODULE_SRC, f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device
        )
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
        for mapper in reversed(state.service_mappers):
            self._try(["cryptsetup", "luksClose", mapper], plan, dry_run, confirm_device)
        state.service_mappers.clear()
        if state.mapper_opened:
            self._try(["cryptsetup", "luksClose", layout.mapper_name], plan, dry_run, confirm_device)
            state.mapper_opened = False
        self._try(["rmdir", layout.mount_root], plan, dry_run, confirm_device)
        if state.image is not None:
            state.image.unmount()
            state.image = None

    def _try(self, argv: list[str], plan: CommandPlan, dry_run: bool, confirm_device: str | None) -> None:
        try:
            self._run(argv, plan, dry_run, confirm_device)
        except Exception:  # noqa: BLE001 - see _teardown docstring
            pass

    def _validate_against_image(self, config: InstallConfig, mountpoint: str, info) -> None:
        """Asks the target distribution's own parser about the configuration.

        Done here rather than in the interface because the image is already
        mounted at this point -- and because this runs before a single byte of
        the medium has been touched, which is the only moment when refusing is
        free.

        Being unable to check is not a refusal. The built-in rules have already
        passed, and blocking an install because an image happens not to carry a
        parser would invent a requirement the distribution does not have; it is
        reported as a step instead, so it shows up in the log rather than
        vanishing.
        """
        from ..config.validator import ConfigValidationUnavailableError, NativeToolValidator

        try:
            result = NativeToolValidator(mountpoint, info.family, info.release).validate(config)
        except ConfigValidationUnavailableError as exc:
            self._progress.step(f"Проверка конфигурации средством дистрибутива не выполнена: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - never fail an install on the checker itself
            self._progress.step(f"Проверка конфигурации средством дистрибутива не выполнена: {exc}")
            return

        if not result.ok:
            raise InstallError(
                "средство проверки самого дистрибутива отклонило конфигурацию: "
                + "; ".join(result.errors)
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _check_arch(self, backend, config: InstallConfig) -> tuple[str, ...]:
        """Decides whether this machine can build the medium, before erasing it.

        Building for another architecture is allowed: every chroot step runs
        under the emulator the kernel dispatches to through binfmt_misc, which
        was measured end to end (see install/emulation.py). What is not allowed
        is *starting* when no emulator is registered -- that would destroy the
        medium's contents and then fail with "Exec format error" somewhere deep
        inside a bootstrap.

        The caller gets a warning back rather than nothing, because a medium
        assembled under emulation has never executed on real target hardware
        and must be tried there before it is relied on.
        """
        if not config.arch:
            return ()
        emulation = probe_emulation(config.arch)
        if emulation.native:
            return ()
        if not emulation.available:
            raise InstallError(emulation.reason)
        return (
            f"Носитель собран под эмуляцией: {emulation.describe()}. "
            "Все шаги установки выполнены, но ни одна программа целевой системы "
            "ни разу не запускалась на настоящем процессоре этой архитектуры. "
            "Обязательно проверьте загрузку носителя на машине "
            f"с архитектурой {emulation.target_arch}, прежде чем выдавать его.",
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
            layout.logs_luks_uuid = str(uuid.uuid4())
            layout.exchange_luks_uuid = str(uuid.uuid4())
            return

        layout.esp_uuid = _blkid_uuid(layout.esp_partition)
        layout.boot_uuid = _blkid_uuid(layout.boot_partition)
        layout.root_uuid = _blkid_uuid(layout.mapper_path)
        layout.logs_uuid = _blkid_uuid(layout.logs_mapper_path)
        layout.exchange_uuid = _blkid_uuid(layout.exchange_mapper_path)
        layout.luks_uuid = _read_output(LuksManager.uuid_command(layout.system_partition))
        layout.logs_luks_uuid = _read_output(LuksManager.uuid_command(layout.logs_partition))
        layout.exchange_luks_uuid = _read_output(LuksManager.uuid_command(layout.exchange_partition))


@dataclass
class _TeardownState:
    mounts: list[str] = field(default_factory=list)
    mapper_opened: bool = False
    #: log/exchange mappings opened during the install, closed in reverse
    service_mappers: list[str] = field(default_factory=list)
    image: "MountedImage | None" = None


class _Progress:
    """Counts stages so a caller can show "шаг 7 из 17" instead of a spinner
    that says nothing about how far along an hour-long run is."""

    #: Stages the pipeline reports. A constant so the first stage already shows
    #: a sensible total rather than counting up from one out of one.
    EXPECTED_STAGES = 18
    #: Building a distribution skips everything to do with a medium.
    BUILD_STAGES = 10
    #: Deploying one skips everything to do with packages.
    DEPLOY_STAGES = 15

    def __init__(self, callback, expected: int | None = None):
        self._callback = callback
        self._expected = expected or self.EXPECTED_STAGES
        self._index = 0

    def step(self, text: str) -> None:
        self._index += 1
        if self._callback is None:
            return
        try:
            self._callback(self._index, max(self._expected, self._index), text)
        except Exception:  # noqa: BLE001 - a broken display must not stop an install
            pass


def _pick_compressor() -> tuple[str, str]:
    """(алгоритм сжатия squashfs, имя файла образа).

    zstd жмёт корень заметно быстрее gzip при сопоставимом размере, но собран
    ли он в mksquashfs — зависит от сборки пакета, поэтому спрашиваем у самой
    программы, а не предполагаем. Имя файла от выбора не зависит: алгоритм
    записан внутри образа.
    """
    try:
        help_text = subprocess.run(
            ["mksquashfs", "-help"], capture_output=True, text=True, timeout=20
        )
        supported = f"{help_text.stdout}{help_text.stderr}"
    except (OSError, subprocess.SubprocessError):
        supported = ""
    compressor = "zstd" if "zstd" in supported else "gzip"
    return compressor, distribution.ARCHIVE_NAME

def _directory_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return total


def _blkid_uuid(path: str) -> str:
    return _read_output(["blkid", "-s", "UUID", "-o", "value", path])


#: Сколько ждать пробу вроде `blkid`.
#:
#: Раньше здесь стояло 30 секунд, и это убивало развёртывание на шаге 12 из
#: 14 — после часа записи. Проба читающая, но читает она носитель, в очередь
#: которого ещё сливаются гигабайты только что записанного: на дешёвой флешке
#: чтение встаёт за этой очередью и тридцати секунд не хватает. Значение то
#: же, что у обычных команд (DEFAULT_TIMEOUT), потому что ограничение здесь —
#: скорость носителя, а не сложность самой команды.
PROBE_TIMEOUT = 600


def _read_output(argv: list[str]) -> str:
    """Read-only probe, so it does not go through CommandRunner's destructive
    gate -- but a failure here still has to stop the install, because a blank
    UUID would produce an fstab that cannot mount anything."""
    try:
        proc = subprocess.run(
            resolve_argv(argv), capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(
            f"носитель не ответил за {PROBE_TIMEOUT} с на чтение: {' '.join(argv)}.\n"
            "Обычно это значит, что он слишком медленный или неисправен."
        ) from exc
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
