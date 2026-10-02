"""OSInstaller: orchestrates UC-4 "Установка ОС на внешний носитель" end to end.

Mirrors the activity diagram (Рисунок 2, "Сеанс администратора" swimlane):
check possible -> partition -> LUKS format -> user + recovery keyslot ->
filesystems -> base system -> kernel/bootloader -> service partitions ->
save records.

Every destructive step is dry-run by default (see command_runner.py). The
Debian backend's real path has been executed against a real USB stick (see
README, раздел «Архитектура»).
"""
from __future__ import annotations

import base64
import importlib.resources
import os
import tempfile
import subprocess
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import crypto
from ..config.schema import InstallConfig
from ..container.models import MediaRecord, RecoveryRecord
from ..recovery.request import RecoveryRequest
from ..errors import ConfigValidationError, InstallError
from ..session import AdminSession
from .backends import get_backend
from .backends.base import (
    EXCHANGE_MOUNTPOINT,
    LOG_MOUNTPOINT,
    PLYMOUTH_THEME_NAME,
    RECOVERY_DIR,
    SERVICE_KEY_DIR,
)
from .command_runner import BOOTSTRAP_TIMEOUT, CommandPlan, CommandResult, CommandRunner
from . import distribution, overlay
from . import integrity
from . import medium
from .devices import BlockDevice, busy_holders, check_install_eligible
from .disk import (
    DiskManager,
    MediaIdentity,
    MediaLayout,
    build_media_identity,
)
from .image_source import MountedImage, inspect_mounted
from . import machine_binding
from .luks import LuksManager
from .emulation import probe as probe_emulation
from .tooling import check_tools_available, resolve_argv

MIB = 1024 * 1024
#: Раздел образов -- два слота (A/B); выпуск РЕД ОС весит 4,5 ГБ, Debian 2,3.
#: С ним и служебными разделами носитель от 16 ГБ.
MIN_IMAGES_PARTITION_MB = 10240
#: Keyslot the day-to-day user password lives in. cryptsetup puts the first
#: passphrase in slot 0; recovery needs to name it to replace it.
USER_KEYSLOT = 0


def _version_key(name: str) -> list:
    """Как `sort -V`: числа сравниваются числами (6.12.101 новее 6.12.21)."""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.split(r"(\d+)", name)]


def slot_files_script(source_boot: str, slot_dir: str) -> str:
    """Самое новое ядро и его initramfs -- в каталог слота под постоянными
    именами `vmlinuz` и `initrd.img` (Э4). Этим же пользуется обновление."""
    return (
        f'set -e; cd "{source_boot}"; '
        'k=$(ls -1 vmlinuz-* 2>/dev/null | sort -V | tail -n 1); '
        '[ -n "$k" ] || { echo "в образе нет ядра" >&2; exit 1; }; '
        'v=${k#vmlinuz-}; '
        'for i in "initrd.img-$v" "initramfs-$v.img"; do [ -f "$i" ] && break; i=; done; '
        '[ -n "$i" ] || { echo "к ядру $v нет initramfs" >&2; exit 1; }; '
        f'cp "$k" "{slot_dir}/vmlinuz"; cp "$i" "{slot_dir}/initrd.img"'
    )


def slot_cfg(root_hash: str, serial: int) -> str:
    """То, что GRUB читает о слоте (`source`): хеш образа и номер выпуска."""
    if not (len(root_hash) == 64 and all(c in "0123456789abcdef" for c in root_hash)):
        raise InstallError("корневой хеш образа не похож на sha256")
    return f"set poa_roothash={root_hash}\nset poa_serial={int(serial)}\n"


#: Где установка с образа собирает дистрибутив (Э10): постоянный временный
#: каталог -- в /tmp он мог бы оказаться в RAM.
BUILD_WORK_ROOT = "/var/tmp"


def _remove_build_dir(path: str) -> None:
    """Убирает временную сборку -- но не поверх смонтированного.

    `rm -rf` по каталогу, где остался bind-монтаж (/dev, /proc, образ), снёс бы
    настоящий /dev машины администратора. Уборка сборки размонтирует всё сама;
    если что-то осталось, каталог не трогается, а причина -- в исключении.
    """
    import shutil

    real = os.path.realpath(path)
    try:
        with open("/proc/self/mounts", encoding="utf-8") as handle:
            busy = [line.split()[1] for line in handle
                    if line.split()[1] == real or line.split()[1].startswith(real + "/")]
    except OSError:
        busy = []
    if busy:
        raise InstallError(f"временная сборка {path} не удалена: под ней смонтировано {busy[0]}")
    shutil.rmtree(path, ignore_errors=True)


def recovery_env(config, media_id: str, luks_uuid: str, mapper_name: str = "",
                 images_uuid: str = "") -> str:
    """Всё, что модуль восстановления знает об этом носителе.

    Отдельной функцией, а не строкой внутри установщика, потому что проверить
    её иначе нечем: содержимое файла в сухом прогоне никуда не записывается,
    и ошибка здесь остаётся невидимой до настоящей загрузки.

    Номер recovery-слота берётся из той же раскладки, что и запись ключей.
    Раньше тут стояла константа `RECOVERY_KEYSLOT = 7`, оставшаяся от
    раскладки до привязки к машинам: в env уезжала семёрка, тогда как код,
    выданный пользователю, лежит в слоте 3 (при двух разрешённых машинах).
    Модуль проверял бы код против чужого слота и его же уничтожал -- то есть
    восстановление не сработало бы ни разу, и молча. Найдено осмотром
    носителя 28.08.2026.

    Устройство названо по UUID: носитель грузится на машинах с другим
    порядком дисков, и `/dev/sda2` там был бы чужим.
    """
    recovery = machine_binding.recovery_keyslots(config)
    machines = machine_binding.machine_keyslots(config)
    return (
        "# generated by portable-os-admin\n"
        f"POA_MEDIA_ID={media_id}\n"
        # Имя тома -- для общего initramfs (Э2): скрипты берут его отсюда.
        f"POA_MAPPER={mapper_name or 'poa-' + media_id}\n"
        f"POA_LUKS_UUID={luks_uuid}\n"
        # Раздел образов слотов (разметка v3): по нему initramfs находит образ.
        f"POA_IMAGES_UUID={images_uuid}\n"
        f"POA_RECOVERY_SLOT={recovery[0]}\n"
        f"POA_USER_SLOT={USER_KEYSLOT}\n"
        # Границы recovery-слотов: коды расходуются по одному, и модуль должен
        # найти первый ещё живой, а не всегда брать первый по счёту.
        f"POA_RECOVERY_FIRST={recovery[0]}\n"
        f"POA_RECOVERY_LAST={recovery[-1]}\n"
        # Машинные слоты -- то, что уничтожает сброс. Пусто, если привязка
        # выключена: тогда и сбрасывать нечего.
        f"POA_MACHINE_FIRST={machines[0] if machines else 0}\n"
        f"POA_MACHINE_LAST={machines[-1] if machines else -1}\n"
        f"POA_SCRYPT_N={crypto.RECOVERY_SCRYPT_N}\n"
        f"POA_SCRYPT_R={crypto.SCRYPT_R}\n"
        f"POA_SCRYPT_P={crypto.SCRYPT_P}\n"
    )
#: Keyslot on the log/exchange volumes holding the administrator's key. A fixed
#: slot so a medium can be re-keyed later without guessing which one it is.
ADMIN_KEYSLOT = 7

#: Which image family can install which target.
IMAGE_FAMILY = {"debian": "debian", "redos": "redos"}

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
#: Общий слой вывода: им разговаривают с человеком все части загрузочной среды
#: -- разблокировка, регистрация машины, восстановление и сброс. Экран один,
#: вызывают его разные компоненты, поэтому он и отдельным файлом.
RECOVERY_UI_SRC = str(
    importlib.resources.files("portable_os_admin.recovery").joinpath("ui.sh")
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


@dataclass(frozen=True)
class _DeployedImage:
    """То немногое от образа, что нужно знать при развёртывании.

    Полного ImageInfo здесь взять неоткуда — образ участвовал в сборке и мог
    остаться на другой машине. Нужны же от него только версии оболочек
    (сведения для описания дистрибутива), поэтому они и едут в метаданных.
    """

    desktop_versions: dict


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
        #: Образ, из которого идёт этот прогон, или None. Передаётся в
        #: генераторы файлов и даёт версии оболочек для метаданных дистрибутива.
        self._image = None
        #: Удостоверение носителя, который пишется сейчас: идентификатор,
        #: раскладка с UUID томов, ключи, выданные коды и опись загрузчика.
        #: Одним объектом, а не россыпью полей установщика: опись, коды и ключ
        #: носителя рождаются на разных шагах записи, и пока каждое лежало
        #: своим полем, добираться до них приходилось через `getattr` с пустым
        #: значением по умолчанию -- то есть молча получать пустоту там, где
        #: шаг не отработал.
        self._identity: "MediaIdentity | None" = None
        #: True once anything has been written to the medium. Read by the
        #: interface when a run fails, so it can say whether the stick's former
        #: contents are gone -- several failures (a mismatched image, a device
        #: too small, no emulator for the target architecture) happen before
        #: the first write, and telling someone their medium was erased when it
        #: was not is its own kind of damage.
        self.medium_modified = False
        #: Тома, которые уборка так и не смогла закрыть. Пусто в норме; если
        #: не пусто, об этом надо сказать вслух — молчаливо занятый носитель
        #: не даст запустить следующую установку, и человек не поймёт почему.
        self._unreleased: list[str] = []

    def abort(self) -> None:
        """Останавливает выполняемую команду и не даёт начать следующую.

        Вызывается из потока интерфейса. Само прерывание идёт как обычный
        отказ: возбуждается исключение, отрабатывает уборка — размонтирование
        и закрытие тома, — и носитель не остаётся занятым.
        """
        self.runner.abort()

    @property
    def _device_public_key_b64(self) -> str:
        """Открытая часть ключа носителя, который пишется сейчас.

        Само значение живёт в удостоверении, рядом с остальным, что уникально
        для этого носителя. Раньше оно было отдельным полем установщика, и
        добираться до него приходилось через `getattr(..., "")`: поле
        появлялось только если отзыв включён, а читали его безусловно.
        """
        return self._identity.device_public_key_b64 if self._identity else ""

    @property
    def unreleased_resources(self) -> tuple[str, ...]:
        """Что осталось занятым после уборки. Читается интерфейсом при отказе."""
        return tuple(self._unreleased)

    def check_possible(self, device: BlockDevice, config: InstallConfig, *, require_unmounted: bool = True) -> None:
        """"Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
        must run, and must pass, before anything below touches the device."""
        required_mb = (
            config.disk.esp_partition_mb
            + config.disk.boot_partition_mb
            + config.disk.state_partition_mb
            + config.disk.log_partition_mb
            + MIN_IMAGES_PARTITION_MB
        )
        check_install_eligible(device, required_mb * MIB, require_unmounted=require_unmounted)

    def _new_identity(self, device: BlockDevice, config: InstallConfig) -> MediaIdentity:
        """Заводит удостоверение носителя -- одинаково для обоих путей записи.

        Одно место, где носитель получает имя. Пока идентификатор, раскладка и
        имя dm-отображения рождались отдельно в `run` и в
        `deploy_distribution`, каждый путь собирал их по-своему, и новое поле
        попадало в один, а во второй -- нет. Отсюда же и приставка
        администратора в ID: она добавляется здесь, и все, кому ID нужен,
        получают его целиком, а не отрезанным от имени тома.
        """
        media_id = crypto.new_media_id(
            distribution.key_fingerprint(self.session.container.admin_secret)[:8]
        )
        identity = build_media_identity(media_id, device.path, config.disk)
        self._identity = identity
        return identity

    def _save_records(
        self,
        identity: MediaIdentity,
        device: BlockDevice,
        config: InstallConfig,
        media_name: str,
        media_owner: str,
    ) -> "tuple[MediaRecord, list[RecoveryRecord]]":
        """Заводит записи контейнера по удостоверению носителя -- обоими путями.

        Раньше эти тридцать строк стояли дважды, в `run` и в
        `deploy_distribution`, и разъезжались: путь установки до 12.09.2026 не
        заполнял имя и владельца, и носитель попадал в список безымянной
        строкой без владельца, хотя записи заводит такие же. Теперь поля
        берутся из одного объекта, и забыть их в одном из путей нечем.
        """
        media_record = MediaRecord(
            media_id=identity.media_id,
            luks_uuid=identity.layout.luks_uuid,
            label=device.model or device.path,
            # Имя даёт администратор при записи: здесь он и решает, кому
            # носитель пойдёт. После записи носитель попадает в список, где
            # строки различаются только идентификаторами.
            name=media_name.strip(),
            # Владелец -- то, чем восстановление опознаёт человека, а не только
            # носитель. Мастер выдачи требует это поле; пустым оно остаётся
            # лишь у носителей, записанных до 11.09.2026, и экран выдачи кода
            # говорит об этом прямо, а не молчит.
            owner=media_owner.strip(),
            config_name=config.name,
            target_os=config.target_os,
            # Without this key the log and exchange volumes of this medium can
            # never be opened from an administrator machine again -- there is
            # no second copy anywhere.
            service_key_b64=base64.b64encode(identity.service_key).decode("ascii"),
            logs_luks_uuid=identity.layout.logs_luks_uuid,
            exchange_luks_uuid=identity.layout.exchange_luks_uuid,
            # Открытая часть ключа носителя: её получает сервер отзыва при
            # публикации записи и по ней проверяет запросы носителя.
            device_public_key_b64=identity.device_public_key_b64,
            # Опись незашифрованной части и подпись к ней. Эталон живёт в
            # контейнере: тот, кто подменит загрузчик, до него не дотянется.
            boot_manifest=dict(identity.boot_manifest),
            boot_manifest_signature=integrity.sign(
                identity.boot_manifest, self.session.container.admin_secret
            ),
        )
        recovery_records = [
            RecoveryRecord(
                media_id=identity.media_id,
                slot=slot,
                # Код не сохраняется: он выводится из секрета администратора и
                # вычисляется в момент выдачи. Хранить его значило бы держать
                # в контейнере то, что и так можно получить, -- и выносить это
                # наружу при каждом обращении.
                code="",
                salt_b64=base64.b64encode(salt).decode("ascii"),
            )
            for slot, _code, salt in identity.issued_recovery
        ]

        # "Установка.Контейнер.Сохранение" -- caller still owns flushing the
        # container to disk via ContainerManager.save().
        self.session.container.add_media_record(media_record)
        for record in recovery_records:
            self.session.container.add_recovery_record(record)
        return media_record, recovery_records

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
        media_name: str = "",
        media_owner: str = "",
    ) -> InstallResult:
        """Носитель прямо с установочного образа: сборка дистрибутива во
        временный каталог и запись его на устройство (LIVE-2.0, Э10).

        Раньше это был третий, самостоятельный путь: система ставилась пакетами
        прямо на носитель, деревом. С LiveCD носитель -- это образ squashfs под
        dm-verity, слоты A/B и общий initramfs выпуска; всё это делают сборка и
        запись, и собирать то же самое третьим способом значило бы держать две
        разметки и два набора дефектов. Цена -- место под сборку на диске
        администратора, то же, что у сборки дистрибутива.

        Носитель проверяется **до** сборки: узнать про занятое устройство
        через час было бы хуже, чем сразу.
        """
        self.session.require_open()
        if config.validation_status != "valid":
            raise ConfigValidationError(
                ["установка запрещена: файл конфигурации не прошёл проверку"]
            )
        self.check_possible(device, config, require_unmounted=False)
        work = tempfile.mkdtemp(prefix="poa-build-", dir=BUILD_WORK_ROOT)
        try:
            built = self.build_distribution(
                config, work, image_path=image_path, dry_run=dry_run, on_step=on_step
            )
            result = self.deploy_distribution(
                device, work, user_passphrase, dry_run=dry_run, confirm_device=confirm_device,
                on_step=on_step, media_name=media_name, media_owner=media_owner,
                _metadata=built.metadata if dry_run else None,
            )
        finally:
            _remove_build_dir(work)
        plan = CommandPlan(device=device.path, results=[*built.plan.results, *result.plan.results])
        return InstallResult(
            media_record=result.media_record,
            recovery_record=result.recovery_record,
            plan=plan,
            warnings=(*built.warnings, *result.warnings),
        )

    def build_distribution(
        self,
        config: InstallConfig,
        destination: str,
        *,
        image_path: str = "",
        extra_packages: "list[str] | None" = None,
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
        self._extra_packages = self._check_extra_packages(extra_packages or [], config)
        self._progress = _Progress(
            on_step,
            expected=_Progress.BUILD_STAGES + (1 if self._extra_packages else 0),
        )

        if config.validation_status != "valid":
            raise ConfigValidationError(
                ["сборка запрещена: файл конфигурации не прошёл проверку"]
            )

        backend = get_backend(config.target_os)
        self._source_components = ("main",)
        self._image = None
        self._source_core_packages = ()
        warnings = self._check_arch(backend, config)
        if not dry_run:
            # Сборке нужен упаковщик, а не распаковщик: носителя здесь нет.
            check_tools_available([*backend.required_tools(), "mksquashfs", "veritysetup"])

        build_id = crypto.new_short_id()
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
        self._run_bootstrap(backend, config, root, image_url, release, plan, dry_run, confirm)

        self._mount_pseudo_filesystems(root, state, plan, dry_run, confirm)
        self._bind_image_into(root, image_url, state, plan, dry_run, confirm)
        self._bind_network(backend, config, image_url, root)
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
        for relative, (content, mode) in backend.scripted_files(config, image=self._image).items():
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm, mode=mode)

        self._install_packages_and_settings(
            config, backend, root, image_url, plan, dry_run, confirm
        )
        generic_initramfs = self._install_generic_boot(config, backend, root, plan, dry_run, confirm)

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
        has_bootloader = (
            os.path.isdir(os.path.join(root, "boot", "efi", "EFI"))
            and bool(os.listdir(os.path.join(root, "boot", "efi", "EFI")))
        ) if not dry_run else True
        metadata = distribution.DistributionMetadata.for_config(
            config,
            release=release,
            arch=backend.normalize_arch(config.arch),
            rootfs_bytes=rootfs_bytes,
            emulated=emulated,
            archive=archive_name,
            desktop_versions=getattr(self._image, "desktop_versions", {}) or {},
        )
        metadata.has_bootloader = has_bootloader
        metadata.generic_initramfs = generic_initramfs

        if generic_initramfs:
            # Дерево dm-verity (Э3b): корень носителя проверяется по нему при
            # каждом чтении. Считается один раз здесь, на диске администратора,
            # а не на каждом носителе по USB.
            self._progress.step("Дерево проверки образа (dm-verity)")
            verity = os.path.join(destination, distribution.VERITY_NAME)
            result = run(["veritysetup", "format", archive, verity], timeout=BOOTSTRAP_TIMEOUT)
            metadata.verity = distribution.VERITY_NAME
            metadata.verity_root_hash = (distribution.parse_root_hash(result.stdout)
                                         if not dry_run else "0" * 64)

        # Номер выпуска (Э4): растёт со временем сборки -- носитель не даст
        # откатиться на выпуск с меньшим номером.
        metadata.release_serial = int(time.time())

        self._progress.step("Подсчёт контрольной суммы")
        if not dry_run:
            _, digest = distribution.write_checksum(archive)
            metadata.sha256 = digest
            distribution.write_metadata(destination, metadata)
            if generic_initramfs:
                self._write_release(destination, root, metadata)
        return metadata

    def _write_release(self, destination: str, root: str, metadata) -> None:
        """`release.json` (Э4): что это за выпуск и чем его проверить.

        Подписывается рабочим ключом администратора с сертификатом корня
        организации, если сертификат есть; без него -- неподписанный, и
        обновление по сети (Э9) такой выпуск не примет.
        """
        import hashlib
        import json

        from ..admin import org_root
        from .access.runtime import poa_trust

        def sha(path: str) -> str:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            return digest.hexdigest()

        boot = os.path.join(root, "boot")
        kernels = sorted((n for n in os.listdir(boot) if n.startswith("vmlinuz-")),
                         key=_version_key)
        kernel = kernels[-1]
        version = kernel[len("vmlinuz-"):]
        initrd = next(n for n in (f"initrd.img-{version}", f"initramfs-{version}.img")
                      if os.path.exists(os.path.join(boot, n)))
        # Ядро и initramfs -- рядом с образом под постоянными именами: так их
        # публикуют на сервере и так кладут в слот (Э9).
        import shutil

        shutil.copyfile(os.path.join(boot, kernel), os.path.join(destination, "vmlinuz"))
        shutil.copyfile(os.path.join(boot, initrd), os.path.join(destination, "initrd.img"))

        def entry(name: str) -> dict:
            path = os.path.join(destination, name)
            return {"file": name, "sha256": sha(path), "size": os.path.getsize(path)}

        payload = {
            "kind": "poa-release", "format": 1, "serial": metadata.release_serial,
            "name": metadata.name, "family": metadata.target_os, "arch": metadata.arch,
            "layout": 3, "kernel_version": version,
            "image": entry(metadata.archive), "verity": entry(metadata.verity),
            "kernel": entry("vmlinuz"), "initrd": entry("initrd.img"),
            "verity_root_hash": metadata.verity_root_hash,
        }
        container = self.session.container
        if container.data.working_key_cert_json:
            document = poa_trust.sign_document(org_root.working_key(container),
                                               json.loads(container.data.working_key_cert_json),
                                               "release", payload)
        else:
            document = {"release": payload}
        with open(os.path.join(destination, distribution.RELEASE_NAME), "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)

    def deploy_distribution(
        self,
        device: BlockDevice,
        directory: str,
        user_passphrase: bytes,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        on_step: "Callable[[int, int, str], None] | None" = None,
        media_name: str = "",
        media_owner: str = "",
        exchange_files: "list[str] | tuple[str, ...]" = (),
        accepted_signature: "distribution.SignatureVerdict | None" = None,
        _metadata: "distribution.DistributionMetadata | None" = None,
    ) -> InstallResult:
        """Writes a built distribution onto one medium, with its own encryption.

        The short half of the work, and the only half that has to be repeated
        per stick. Everything unique to this medium is created here and now:
        the LUKS master key, the passphrase, the recovery code and its salt,
        the service key, the media id and the filesystem UUIDs.

        The distribution is verified against its recorded checksum **before**
        the medium is touched, so a damaged archive costs a message rather than
        an erased stick and an hour of waiting.

        `media_name` -- как этот носитель называть в списке: «Иванов,
        бухгалтерия». Спрашивается здесь потому, что здесь администратор и
        решает, кому носитель пойдёт; идентификатор появится только после
        записи, и назвать носитель постфактум означает искать его в списке
        одинаковых строк.

        `exchange_files` -- файлы и каталоги, которые лягут в раздел обмена
        сразу при записи. Через обмен едут настройки программ, которые
        администратор доустановил в образ, и класть их после сборки означало
        бы второй заход: открыть контейнер, подключить носитель, открыть том,
        скопировать. Раздел в этот момент уже смонтирован -- копирование
        ничего не стоит.

        `accepted_signature` -- вердикт подписи, на который администратор дал
        согласие в мастере. Подпись проверяется здесь ещё раз, в том месте, где
        описание дистрибутива читается для записи: проверка в интерфейсе и
        чтение установщиком разнесены во времени, и подмена между ними не
        должна пройти под чужим согласием. Испорченная подпись отвергается
        всегда. Без переданного вердикта неподписанный дистрибутив не
        отвергается -- так вызывают установщик тесты и сценарии стенда, у
        которых мастера нет; решение о неподписанном принимает человек, и
        принимает его в мастере.
        """
        self.session.require_open()
        self._progress = _Progress(on_step, expected=_Progress.DEPLOY_STAGES)
        # Читается шагом, который монтирует раздел обмена: тащить список
        # через семь параметров вложенных вызовов ради одного `cp` -- хуже.
        self._exchange_files = tuple(exchange_files)
        self._exchange_problems: list[str] = []

        self._progress.step("Проверка дистрибутива")
        # `_metadata` -- только пробный прогон установки с образа (Э10): сборка
        # там ничего не пишет, и читать подпись и описание неоткуда.
        verdict = (distribution.verify_distribution(directory, self.session.container.admin_secret)
                   if _metadata is None else None)
        if verdict is not None and verdict.status == distribution.INVALID:
            raise InstallError(
                f"подпись дистрибутива не прошла проверку: {verdict.reason}.\n"
                "Носитель не изменён."
            )
        if accepted_signature is not None and verdict is not None and verdict != accepted_signature:
            raise InstallError(
                "подпись дистрибутива изменилась после того, как запись была "
                "подтверждена.\nНоситель не изменён."
            )
        metadata = _metadata or distribution.read_metadata(directory)
        config = InstallConfig.from_dict(metadata.config)
        backend = get_backend(config.target_os)
        # Версии оболочек приехали вместе с дистрибутивом: образа здесь уже нет.
        self._image = _DeployedImage(metadata.desktop_versions or {})
        warnings = self._check_arch(backend, config)
        if not dry_run:
            unpacking = ["mount", "umount", "cp"] if distribution.is_squashfs(metadata.archive) else ["tar"]
            check_tools_available([*backend.required_tools(), *unpacking])
            # Before anything is erased. This is the whole point of doing it
            # here rather than discovering a truncated archive mid-unpack.
            distribution.verify_archive(directory, metadata)

        # До стирания, а не на последнем шаге. Дистрибутив без загрузчика для
        # RHEL-семейства бесполезен: собрать его на месте нечем, grub2-install
        # для EFI там отключён. Раньше это выяснялось через час работы, когда
        # носитель уже стёрт.
        if not (metadata.generic_initramfs and metadata.verity and metadata.verity_root_hash
                and metadata.release_serial):
            raise InstallError(
                "дистрибутив собран до перехода на LiveCD-разметку (нет общего "
                "initramfs и дерева проверки dm-verity) — пересоберите его.\n"
                "Носитель не изменён."
            )

        if backend.bootloader_comes_from_packages and not metadata.has_bootloader:
            raise InstallError(
                "в дистрибутиве нет загрузчика, а собрать его на месте для этого "
                "семейства нельзя: подписанные shim и grub приходят готовыми в "
                "пакетах.\nДистрибутив собран версией программы, которая их "
                "выбрасывала, — пересоберите его.\nНоситель не изменён."
            )

        self.check_possible(device, config, require_unmounted=False)

        identity = self._new_identity(device, config)
        layout = identity.layout
        plan = CommandPlan(device=device.path)
        state = _TeardownState()

        try:
            self._deploy(
                device, config, backend, identity, user_passphrase, plan, state,
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

        media_record, recovery_records = self._save_records(
            identity, device, config, media_name, media_owner
        )

        return InstallResult(
            media_record=media_record,
            recovery_record=recovery_records[0],
            plan=plan,
            # Неудача с файлом настроек -- предупреждение, а не отказ: носитель
            # записан и годен, а файл кладётся потом через раздел «Обмен».
            warnings=[*warnings, *self._exchange_problems],
        )

    def _deploy(
        self, device, config, backend, identity, user_passphrase, plan, state,
        *, directory, metadata, dry_run, confirm_device,
    ) -> None:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        layout = identity.layout

        archive = distribution.archive_path(directory, metadata)
        overlay_root = self._overlay_root(config, metadata)
        self._generic_boot = overlay_root and bool(getattr(metadata, "generic_initramfs", False))

        # Ключи и коды не возвращаются кортежем, а ложатся в удостоверение:
        # читают их отсюда и запись контейнера, и тот, кто пишет файлы
        # носителя, -- каждый из одного места.
        self._prepare_medium(
            device, config, identity, user_passphrase, plan, state, dry_run, confirm_device,
            overlay_root=overlay_root,
        )

        # Разделы подключаются ДО копирования, а не после. Иначе всё, что
        # дистрибутив держит в /boot — ядро, initramfs, записи загрузчика —
        # ложится на корневой раздел, а следом поверх монтируется пустой
        # /boot и прячет их. Носитель с пустым /boot не загрузится, и понять
        # это по содержимому корня нельзя: файлы на месте, просто не на том
        # разделе. ESP подключается отдельно, ниже: на FAT нет владельцев, и
        # `cp -a` на нём спотыкается о невозможность их сохранить.
        #
        # Разметка v3 -- только образ, и порядок обратный по той же
        # причине: корня ещё нет. Том смонтирован как контейнер, а корень --
        # overlay поверх образа, и собрать его можно только после того, как
        # образ оказался в контейнере. Служебные разделы подключаются к уже
        # собранному корню, ниже.
        self._progress.step("Копирование системы на носитель")
        source = self._deploy_system_image(
            archive, layout, state, plan, dry_run, confirm_device,
            verity=os.path.join(directory, metadata.verity),
        )
        layout.slot = "a"
        layout.root_hash = metadata.verity_root_hash
        self._progress.step("Подключение служебных разделов")
        self._mount_boot_partition(layout, state, plan, dry_run, confirm_device)
        self._mount_logs_volume(layout, state, plan, dry_run, confirm_device)
        # То, что образ держит на этих путях, надо перенести на сами
        # разделы -- иначе пустой раздел накрывает содержимое образа.
        #
        # Те же грабли, что были с раскладкой деревом, но с другой
        # стороны. Там их обошли порядком: разделы подключали до
        # копирования, и файлы ложились уже на них. Здесь порядок обратный
        # по необходимости -- корня до сборки overlay не существует, -- и
        # обойти нечем: ядро и initramfs лежат в образе, а раздел /boot
        # приходит пустым и прячет их. Запись на настоящую флешку
        # 09.09.2026 остановилась именно на этом: генератор меню не нашёл
        # в /boot ни одного ядра.
        run(["sh", "-c",
             f"cp -a {overlay.LOWER_MOUNT}/var/log/. {layout.mount_root}/var/log/ 2>/dev/null || true"],
            timeout=BOOTSTRAP_TIMEOUT)
        # Раздел загрузки (v3/Э4): ядро и общий initramfs -- в каталог слота
        # под постоянными именами (меню GRUB одно на оба слота), номер
        # выпуска и корневой хеш -- в slot.cfg; остальное /boot образа
        # (каталоги загрузчика) -- в корень раздела.
        slot_boot = f"{layout.mount_root}/boot/slot-{layout.slot}"
        run(["mkdir", "-p", slot_boot])
        run(["sh", "-c", slot_files_script(overlay.LOWER_MOUNT + "/boot", slot_boot)],
            timeout=BOOTSTRAP_TIMEOUT)
        run(["sh", "-c",
             f"cd {overlay.LOWER_MOUNT}/boot && "
             f"for d in */; do [ -d \"$d\" ] && cp -a \"$d\" {layout.mount_root}/boot/; done; true"],
            timeout=BOOTSTRAP_TIMEOUT)
        self._write_file(f"{slot_boot}/slot.cfg",
                         slot_cfg(metadata.verity_root_hash, metadata.release_serial),
                         plan, dry_run, confirm_device, mode=0o644)
        release = os.path.join(directory, distribution.RELEASE_NAME)
        if os.path.exists(release):
            run(["cp", release, f"{slot_boot}/{distribution.RELEASE_NAME}"])

        # Часть того же шага, а не отдельный: копирование не закончено, пока
        # данные не на носителе, а не в кэше страниц. Без этого следующая же
        # команда — чтение UUID — встаёт в очередь за гигабайтами записанного
        # и упирается в таймаут. Для администратора это та же запись, которая
        # ещё идёт, поэтому и показывается как она.
        run(["sync"], timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Запись загрузчика на раздел EFI")
        # Копия дерева положила файлы загрузчика в <корень>/boot/efi, то есть
        # на раздел /boot. Сейчас поверх встанет настоящий ESP и закроет их
        # собой — ровно та ситуация, из-за которой носитель не загружался.
        # Здесь она безвредна, но оставлять спрятанные файлы нельзя: место они
        # занимают, а найти их потом невозможно.
        run(["sh", "-c", f"rm -rf {layout.mount_root}/boot/efi/* || true"])
        self._mount_esp(layout, state, plan, dry_run, confirm_device)
        for argv in backend.esp_population_commands(layout, config, source=source):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)
        self._mount_pseudo_filesystems(layout.mount_root, state, plan, dry_run, confirm_device)

        self._progress.step("Настройка носителя")
        self._write_medium_identity(
            config, backend, identity, plan, state, dry_run, confirm_device,
            overlay_root=overlay_root,
        )

        # Файлы, уникальные для носителя, появились после разметки эталона —
        # у них меток нет.
        restore = backend.restore_labels_command(layout.mount_root, ("/etc", "/boot"))
        if restore is not None:
            run(restore, timeout=BOOTSTRAP_TIMEOUT)

        if self._generic_boot:
            # Initramfs общий и уже в образе (Э2); носителю -- его второй initrd.
            self._progress.step("Запись данных носителя для загрузки")
            self._write_medium_initrd(backend, identity, config, plan, dry_run, confirm_device)
        else:
            self._progress.step("Сборка initramfs с разблокировкой диска")
            run(backend.initramfs_command(layout.mount_root), timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Установка загрузчика на носитель")
        for argv in backend.bootloader_commands(layout, config, overlay_root=overlay_root):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Запись модуля восстановления")
        self._install_recovery_module(
            identity, plan, dry_run=dry_run, confirm_device=confirm_device
        )

        self._progress.step("Опись загрузчика и initramfs")
        identity.boot_manifest = self._collect_boot_manifest(layout, dry_run)
        # Слой изменений передаётся только там, где корень собран из образа:
        # метки по overlay не ложатся, и ставить их надо прямо по нему.
        _, upper_dir, _ = overlay.container_paths(layout.container_root)
        self._install_self_check(
            identity, backend, plan, dry_run, confirm_device,
            overlay_upper=upper_dir if overlay_root else "",
        )

        self._progress.step("Завершение записи и отключение носителя")
        run(["sync"])

    def _write_revocation_key(self, config, identity, plan, dry_run, confirm_device) -> None:
        """Открытый ключ ресурса отзыва -- на носитель, обоими путями.

        Ключ приходит от сервера: его туда не отправляют, а забирают при
        знакомстве, потому что сервер разворачивают раньше, чем появляется
        приложение. На носитель он попадает при записи, а не в образ
        дистрибутива: у разных организаций разные ресурсы, а дистрибутив один.

        Отдельным помощником, а не строчками в одном из путей, -- и это не
        вкусовщина. До 13.09.2026 ключ писал только путь развёртывания.
        Носитель, собранный «Установкой ОС», уезжал со службой отзыва, со
        скриптом, с адресом ресурса -- и без ключа, то есть с проверкой,
        которая при каждой загрузке пишет «нет открытого ключа» и выходит.
        Найдено осмотром собранного носителя, а не тестом, и это тот же класс
        дефекта, что и все прежние: два пути делают одно и то же врозь.
        """
        from . import revocation

        if not revocation.is_enabled(config):
            return

        layout = identity.layout

        key_pem = self.session.container.data.revocation_public_key_pem
        if not key_pem.strip():
            # Носитель без ключа не проверит ни одного ответа и будет молча
            # считать себя действующим -- худший из отказов для такой вещи,
            # поэтому останавливаемся до записи, а не после.
            raise InstallError(
                "в конфигурации включён отзыв носителей, но контейнер не знаком "
                "с ресурсом отзыва. Подключитесь к нему в «Обслуживании» — "
                "приложение заберёт открытый ключ, без которого носитель "
                "не сможет проверить ответ"
            )

        key_dir = revocation.PUBLIC_KEY_PATH.rsplit("/", 1)[0]
        self._run(
            ["mkdir", "-p", f"{layout.mount_root}/{key_dir}"], plan, dry_run, confirm_device
        )
        self._write_file(
            f"{layout.mount_root}/{revocation.PUBLIC_KEY_PATH}",
            key_pem,
            plan, dry_run, confirm_device, mode=0o444,
        )

        # Ключ носителя. Рождается здесь, при записи, а не в дистрибутиве: у
        # каждого носителя свой, иначе сервер не отличил бы один от другого.
        # Закрытая часть -- только на зашифрованный корень, 0400: пользователь
        # киоска её не прочитает, а без неё запрос серверу получает 403.
        # Открытая часть уезжает в запись носителя в контейнере.
        private_pem, identity.device_public_key_b64 = revocation.new_device_key()
        self._write_file(
            f"{layout.mount_root}/{revocation.DEVICE_KEY_PATH}",
            private_pem,
            plan, dry_run, confirm_device, mode=0o400,
        )

        # Опрос включается ссылкой таймера, а не `systemctl enable`, и здесь же,
        # рядом с файлами, -- ровно как служба целостности и по той же причине:
        # systemd целевой системы не запущен, enable в chroot делает эту самую
        # ссылку, а команды chroot выполняются только на одном из двух путей.
        #
        # Найдено живой загрузкой 14.09.2026: на носителе РЕД ОС, собранном
        # развёртыванием дистрибутива, лежали и скрипт, и ключ, а службы в
        # автозапуске не было -- проверка отзыва не выполнялась ни разу.
        # С 1.1.0 службу запускает таймер (раз в пять минут) и диспетчер сети,
        # поэтому в автозапуск ставится таймер, а не сама служба.
        wants = f"{layout.mount_root}/etc/systemd/system/timers.target.wants"
        self._run(["mkdir", "-p", wants], plan, dry_run, confirm_device)
        self._run(
            ["ln", "-sf", f"/{revocation.TIMER_PATH}", f"{wants}/{revocation.TIMER_NAME}"],
            plan, dry_run, confirm_device,
        )
        self._write_access_config(config, identity, key_pem, plan, dry_run, confirm_device)

    def _write_access_config(self, config, identity, key_pem, plan, dry_run, confirm_device) -> None:
        """Настройки доступа этого носителя (Э8) и автозапуск службы.

        Код клиента и юниты уже в образе; здесь -- только то, что своё у
        носителя. Отпечаток TLS сервера снимается сейчас, с машины
        администратора, по обычной проверке УЦ; http-адрес (стенд) остаётся
        без отпечатка -- клиент носителя такой адрес не примет, кроме петли.
        """
        from ..admin import org_root
        from .access import files as access_files

        if not access_files.is_enabled(config):
            return
        layout = identity.layout
        pins: tuple[str, ...] = ()
        if config.revocation.url.startswith("https://") and not dry_run:
            from ..admin.access_client import fetch_tls_spki
            pins = (fetch_tls_spki(config.revocation.url),)
        self._write_file(
            f"{layout.mount_root}/{access_files.CONFIG_PATH}",
            access_files.medium_config(config, identity.media_id, key_pem, pins,
                                       org_root.anchors(self.session.container)),
            plan, dry_run, confirm_device, mode=0o600,
        )
        if pins:
            self._write_file(f"{layout.mount_root}/{access_files.TLS_PIN_PATH}",
                             access_files.curl_pins(pins) + "\n", plan, dry_run, confirm_device,
                             mode=0o644)
        wants = f"{layout.mount_root}/etc/systemd/system/multi-user.target.wants"
        self._run(["mkdir", "-p", wants], plan, dry_run, confirm_device)
        for path, name in ((access_files.SERVICE_PATH, access_files.SERVICE_NAME),
                           (access_files.EVENTS_SERVICE_PATH, access_files.EVENTS_SERVICE_NAME)):
            self._run(["ln", "-sf", f"/{path}", f"{wants}/{name}"], plan, dry_run, confirm_device)
        sysinit = f"{layout.mount_root}/etc/systemd/system/sysinit.target.wants"
        self._run(["mkdir", "-p", sysinit], plan, dry_run, confirm_device)
        self._run(["ln", "-sf", f"/{access_files.FIREWALL_SERVICE_PATH}",
                   f"{sysinit}/{access_files.FIREWALL_SERVICE_NAME}"], plan, dry_run, confirm_device)

    def _write_medium_files(
        self, config, backend, identity, plan, state, dry_run, confirm_device,
        *, overlay_root: bool = False, skip_image_files: bool = False,
    ) -> None:
        """Всё, что кладётся на носитель и зависит от него самого.

        Один помощник на оба пути -- и это не вкусовщина. Раньше развёртывание
        писало эти файлы через `_write_medium_identity`, а установка делала то
        же самое в своём теле, и наборы разъезжались. Цена расхождения --
        четыре дефекта за два дня, каждый из которых виден только живой
        загрузкой: хуки overlay не попадали на носитель (10.09.2026), ключ
        ресурса отзыва не писался путём установки, служба отзыва уезжала
        выключенной, `curl` отсутствовал. Теперь новая вещь на носителе
        физически не может попасть лишь в один путь.

        Различий между путями ровно два, и оба -- параметрами:

        `skip_image_files` -- развёртывание кладёт лишь разницу с образом:
        всё, что одинаково для носителей из одного дистрибутива, туда уже
        положила сборка, и переписывать образ поверх самого себя незачем. На
        пути установки корень собран пакетами, общего набора там не писал
        никто, и пишется он целиком.

        `overlay_root` -- хуки сборки корня из образа нужны только тогда, когда
        корень **действительно** собран overlay поверх образа. Написанные на
        обычный корень, они уносят его из `$rootmnt` и оставляют систему без
        точки монтирования (13.09.2026, Debian + MATE).
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        layout = identity.layout

        self._probe_uuids(layout, dry_run)
        for relative, content in backend.config_files(config, layout, image=self._image).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)

        # Скрипты и юниты несут собственный режим. Написанные 0644, они были бы
        # на месте, верны и мертвы: список разрешённого в киоске не применился
        # бы ни разу, а служба обновления не стартовала бы вовсе.
        common = backend.scripted_files(config, image=self._image) if skip_image_files else {}
        if getattr(self, "_generic_boot", False):
            # Загрузочные хуки -- в образе, общие (Э2); копия с данными носителя
            # в слое носителя лишь заслонила бы их.
            from . import medium_initrd
            common = {**common, **backend.scripted_files(
                config, medium_initrd.GENERIC, image=self._image, overlay_root=True)}
        for relative, (content, mode) in backend.scripted_files(
            config, layout, image=self._image, overlay_root=overlay_root
        ).items():
            if relative in common:
                continue
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device, mode=mode)

        # Ключ, которым crypttab открывает тома журналов и обмена при загрузке.
        # Лежит на зашифрованном корне, то есть доступен только после того, как
        # корень открыт парольной фразой; 0400 и владелец root, потому что в
        # работающей системе его не читает никто, кроме systemd-cryptsetup.
        # Путь спрашивается у вида носителя, а не берётся константой: у токена
        # ключ живёт в чипе, и файла на носителе не будет вовсе (см.
        # `install/medium.py`). Сегодня вид один -- флешка, -- и ответ тот же
        # самый; смысл в том, что при появлении второго вида меняется одно
        # место, а не каждое обращение к константе.
        key_path = medium.profile().service_key_path
        if key_path:
            run(["mkdir", "-p", f"{layout.mount_root}/{SERVICE_KEY_DIR}"])
            self._write_file(
                f"{layout.mount_root}/{key_path}", identity.boot_key, plan, dry_run,
                confirm_device, binary=True, mode=0o400,
            )

        self._write_revocation_key(config, identity, plan, dry_run, confirm_device)

        # Том обмена монтируется здесь, а не только при загрузке, чтобы его
        # корневой каталог стал доступен на запись тому, кто будет носителем
        # пользоваться. 1777 (как у /tmp), а не владелец: учётная запись
        # создаётся позже, в chroot, и её uid здесь ещё не известен. Том
        # зашифрован, а внутри переносной системы пользователь ровно один,
        # поэтому вольный режим ничего не стоит.
        # Раздела обмена нет (разметка v3): файлы администратора ложатся в слой
        # носителя на прежний путь, а fstab подключает его только для чтения.
        run(["mkdir", "-p", f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"])
        self._exchange_problems = self._fill_exchange(
            run, layout, getattr(self, "_exchange_files", ())
        )

    def _write_medium_identity(
        self, config, backend, identity, plan, state, dry_run, confirm_device,
        overlay_root: bool = False,
    ) -> None:
        """Everything that makes this medium this medium and not another one.

        Принимает удостоверение целиком, а не восемь значений подряд. Прежний
        список аргументов содержал и такое, что здесь не нужно вовсе
        (`service_key`): при россыпи одиночных параметров лишнее не видно ни
        глазом, ни проверкой.
        """
        # Сами файлы носителя пишет общий помощник -- тот же, что и на пути
        # установки. Разница между путями ровно одна и выражена флагом:
        # развёртывание кладёт лишь то, чего нет в образе.
        self._write_medium_files(
            config, backend, identity, plan, state, dry_run, confirm_device,
            overlay_root=overlay_root, skip_image_files=True,
        )

        # Before the initramfs is built, never after: these are what the
        # rebuild copies into the image. С общим initramfs (Э2) модуль уже в
        # образе, а данные носителя едут вторым initrd.
        if not getattr(self, "_generic_boot", False):
            self._install_recovery_boot(
                backend, identity, config, plan,
                dry_run=dry_run, confirm_device=confirm_device,
            )

    def _mount_logs_volume(self, layout, state, plan, dry_run, confirm_device) -> None:
        """Том журналов — та же история, что и /boot, этажом ниже.

        Пакеты создают свои каталоги в /var/log при установке: auditd —
        /var/log/audit, и так же sssd, samba, cups. Каждый со своим
        владельцем, правами и меткой SELinux. Служба, не нашедшая своего
        каталога, его не создаёт — она отказывается стартовать.

        Если подключить том после копирования, эти каталоги лягут на корневой
        раздел, а поверх встанет пустая файловая система и закроет их собой.
        Носитель загрузится, но auditd будет падать при каждой попытке, и по
        содержимому корня этого не увидеть: каталоги на месте, просто не на
        том томе. Наблюдалось на РЕД ОС 26.08.2026.

        Подключение до копирования снимает вопрос целиком: дерево каталогов
        приезжает на том вместе со всем остальным, с метками и правами.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        run(["mkdir", "-p", f"{layout.mount_root}{LOG_MOUNTPOINT}"])
        run(["mount", layout.logs_mapper_path, f"{layout.mount_root}{LOG_MOUNTPOINT}"])
        state.mounts.append(f"{layout.mount_root}{LOG_MOUNTPOINT}")

    def _mount_boot_partition(self, layout, state, plan, dry_run, confirm_device) -> None:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        run(["mkdir", "-p", f"{layout.mount_root}/boot"])
        run(["mount", layout.boot_partition, f"{layout.mount_root}/boot"])
        state.mounts.append(f"{layout.mount_root}/boot")

    def _mount_esp(self, layout, state, plan, dry_run, confirm_device) -> None:
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
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

        upgrade = backend.system_upgrade_command(root)
        if upgrade is not None:
            # Этап Э22: база поставлена с образа, теперь всё -- до свежих
            # версий из сети. LiveCD обновляется только выпуском, поэтому
            # свежесть пакетов определяется моментом сборки.
            self._progress.step("Обновление системы из репозитория")
            run(upgrade, timeout=BOOTSTRAP_TIMEOUT)

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

        # Пакеты семейства, без которых носитель работает не полностью. Вне
        # блока рабочего стола намеренно: systemd-cryptsetup нужен и
        # консольному профилю -- служебные тома открываются одинаково.
        extra_packages = backend.optional_packages(config)
        if extra_packages:
            optional = backend.optional_package_command(root, extra_packages)
            if optional is not None:
                run(optional, timeout=BOOTSTRAP_TIMEOUT)

        image_dir = image_url[len("file://"):] if image_url.startswith("file://") else ""
        firmware_cmd = backend.firmware_install_command(root, image_dir)
        if firmware_cmd is not None:
            self._progress.step("Установка прошивок устройств")
            run(firmware_cmd, timeout=BOOTSTRAP_TIMEOUT)

        self._install_extra_packages(root, backend, run, plan, dry_run, confirm_device)

        prune = backend.old_kernels_command(root)
        if prune is not None:
            run(prune, timeout=BOOTSTRAP_TIMEOUT)

        self._progress.step("Создание учётной записи и служб")
        for argv in backend.chroot_setup_commands(root, config):
            run(argv, timeout=BOOTSTRAP_TIMEOUT)

        # Метки мандатного контроля — один раз на дистрибутив. Они живут в
        # расширенных атрибутах, которые сохраняют и squashfs, и копирование,
        # поэтому каждый носитель получает их вместе с системой. Без них
        # система замирает при первой загрузке, не дойдя до служебных ФС.
        relabel = backend.relabel_commands(root)
        if relabel:
            self._progress.step("Расстановка меток безопасности")
            for argv in relabel:
                run(argv, timeout=BOOTSTRAP_TIMEOUT)

    #: Расширение файла пакета для каждого семейства. Проверяется до начала
    #: работы: узнать через сорок минут, что .deb положили в сборку РЕД ОС, --
    #: ровно тот способ терять время, от которого эта программа и уходит.
    PACKAGE_SUFFIX = {"debian": ".deb", "redos": ".rpm"}

    def _check_extra_packages(self, paths: list[str], config: InstallConfig) -> list[str]:
        """Checks the supplied package files before anything long begins."""
        if not paths:
            return []

        suffix = self.PACKAGE_SUFFIX.get(config.target_os)
        problems = []
        for path in paths:
            if not os.path.isfile(path):
                problems.append(f"файла нет: {path}")
            elif suffix and not path.lower().endswith(suffix):
                problems.append(
                    f"{os.path.basename(path)}: для {config.target_os} нужен {suffix}"
                )
        if problems:
            raise InstallError("; ".join(problems))
        return list(paths)

    def _install_extra_packages(self, root, backend, run, plan, dry_run, confirm_device) -> None:
        """Installs the package files the administrator supplied themselves.

        The closed-network case this tool exists for is also the case where the
        needed program is not in any repository: a VPN client, a remote-desktop
        client, an in-house application. Without this the administrator builds
        the medium and then installs by hand on each one.

        Done here, at build time, so dependencies resolve against the image
        once and every medium made from this distribution carries the same set.

        The files are copied *inside* the tree before installing. A path that
        exists only outside means nothing to a package manager working in the
        target -- this project has already been caught by that once, with an
        image whose file:// URL was valid outside the chroot and absent inside.
        """
        if not self._extra_packages:
            return

        self._progress.step("Установка пакетов администратора")
        target_dir = f"{root}/{backend.EXTRA_PACKAGE_DIR}"
        run(["mkdir", "-p", target_dir])
        names = []
        for path in self._extra_packages:
            name = os.path.basename(path)
            run(["cp", path, f"{target_dir}/{name}"])
            names.append(name)

        argv = backend.local_package_install_command(root, names)
        if argv is None:
            raise InstallError(
                f"семейство {backend.name} не умеет ставить принесённые пакеты"
            )
        run(argv, timeout=BOOTSTRAP_TIMEOUT)
        # Файлы не должны уехать на носитель: они уже установлены, а место на
        # переносном носителе -- не то, чем можно разбрасываться.
        run(["rm", "-rf", target_dir])

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

    def _bind_network(self, backend, config: InstallConfig, image_url: str, root: str) -> None:
        """Сетевые репозитории сборки -- только поверх образа (Э22).

        Без образа источник и так сеть (`mirror`), и повторять его нечего.
        """
        if image_url.startswith("file://"):
            backend.bind_network(config, image_url[len("file://"):], root)
        else:
            backend.network_repos = ()

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
        # Образ запоминается для генераторов файлов и версий оболочек.
        self._image = info
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
    def _prepare_medium(
        self, device, config, identity, user_passphrase, plan, state, dry_run,
        confirm_device, overlay_root: bool = False,
    ) -> None:
        """Erases the medium and builds its encrypted, empty structure.

        Shared by the one-shot install and by deploying a distribution,
        because it is the part that must happen *per medium*: the LUKS
        master key is generated here, and generating it here is what makes
        two media from one distribution two systems rather than copies of
        one.

        Всё, что здесь рождается -- коды восстановления с их солями, ключ
        администратора и ключ-файл загрузки, -- ложится в удостоверение
        носителя, а не возвращается кортежем из четырёх значений. Кортеж
        приходилось разбирать у вызывающего и протаскивать дальше по одному,
        и ровно так значения и отрывались от места, где родились.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        layout = identity.layout

        self._progress.step("Отключение носителя от системы")
        self._unmount_medium(device, plan, dry_run, confirm_device)
        self._refuse_if_busy(device, dry_run)

        self._progress.step("Очистка носителя и создание разделов")
        # Set immediately before the first destructive command, not after: if
        # partitioning itself fails halfway, the medium has still been written
        # to and saying otherwise would be worse than saying nothing.
        self.medium_modified = not dry_run
        self.disk_manager.apply(
            # Та самая разметка, по которой посчитана раскладка носителя, а не
            # второй такой же расчёт из тех же входных данных.
            identity.partition_plan,
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

        # Коды восстановления: по одному на каждый оставшийся keyslot.
        #
        # Не один и не пять: код расходуется безвозвратно и тратится теперь на
        # два разных события -- забытый пароль и очистку списка машин. Чем
        # меньше машин разрешил администратор, тем больше кодов помещается, и
        # наоборот; заголовок LUKS2 отдаётся под это целиком.
        #
        # Цена честная и заметная: каждый keyslot -- отдельный расчёт argon2id,
        # замерено 8,4 секунды на слот, то есть около трёх с половиной минут на
        # носитель. Это плата за то, что пользователь не поедет в офис.
        slots = machine_binding.recovery_keyslots(config)
        self._progress.step(f"Создание ключей восстановления ({len(slots)})")
        recovery_codes = []
        recovery_salts = []
        # Коды выводятся из секрета администратора, а не берутся случайными.
        # Случайный код пришлось бы хранить в контейнере готовым, а значит
        # выносить наружу при каждой выдаче -- и он же показывался на экране
        # сразу после развёртывания с припиской «запишите его». Выведенный не
        # хранится нигде: администратор вычислит его в момент звонка.
        admin_secret = self.session.container.admin_secret
        for slot in slots:
            code = crypto.derive_recovery_code(admin_secret, identity.media_id, slot)
            salt = crypto.new_salt()
            self.luks_manager.add_keyslot(
                layout.system_partition,
                user_passphrase,
                crypto.derive_recovery_passphrase(code, salt),
                slot=slot,
                dry_run=dry_run,
                confirm_device=confirm_device,
                plan=plan,
            )
            recovery_codes.append(code)
            recovery_salts.append(salt)
        identity.issued_recovery = tuple(zip(slots, recovery_codes, recovery_salts))
        # Первый код -- тот, что уезжает в результат установки и в initramfs:
        # именно им пользуется модуль восстановления, пока не израсходован.
        identity.recovery_code = recovery_codes[0]
        identity.recovery_salt = recovery_salts[0]

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
        self._progress.step("Шифрование раздела журналов")
        service_key = identity.service_key = crypto.new_volume_key()
        boot_key = identity.boot_key = crypto.new_volume_key()
        for partition, mapper in ((layout.logs_partition, layout.logs_mapper_name),):
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
        run(["mkfs.ext4", "-F", "-O", CONSERVATIVE_EXT4_FEATURES, "-L", "POA_ROOT", layout.mapper_path])
        run(["mkfs.vfat", "-F", "32", "-n", "POA_ESP", layout.esp_partition])
        run(["mkfs.ext4", "-F", "-O", CONSERVATIVE_EXT4_FEATURES, "-L", "POA_BOOT", layout.boot_partition])
        run(["mkfs.ext4", "-F", "-O", CONSERVATIVE_EXT4_FEATURES, "-L", "POA_LOGS", layout.logs_mapper_path])
        run(["mkfs.ext4", "-F", "-O", CONSERVATIVE_EXT4_FEATURES, "-L", "POA_IMAGES", layout.images_partition])

        self._progress.step("Монтирование носителя")
        # Mount the target tree, outermost first.
        #
        # Куда монтируется сам том, зависит от того, как на носителе будет
        # лежать система. Распакованным деревом -- том и есть корень. Сжатым
        # образом -- том становится контейнером, в котором лежат образ и слой
        # изменений, а корнем будет overlay поверх него, и собирается он
        # позже, когда образ уже на месте.
        volume_mount = layout.container_root if overlay_root else layout.mount_root
        run(["mkdir", "-p", volume_mount])
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
            layout.mapper_path, volume_mount,
        ])
        state.mounts.append(volume_mount)

    def _deploy_system_image(
        self, archive, layout, state, plan, dry_run, confirm_device, *, verity: str
    ) -> str:
        """Кладёт образ в слот A раздела образов и собирает поверх него корень.

        Разметка v3 (Э3b): образ и дерево dm-verity -- файлами в `slot-a/` на
        незашифрованном разделе образов (секретов в образе нет), слой носителя
        и состояние -- на зашифрованном томе. Возвращает точку, где смонтирован
        сам образ: файлы загрузчика для ESP берутся оттуда.

        Порядок размонтирования обратный порядку сборки, и он важен: overlay
        держит нижний слой, нижний слой держит петлю, петля -- файл на разделе
        образов. Список `state.mounts` разбирается с конца.
        """
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731

        container = layout.container_root
        images = layout.images_root
        slot_dir = f"{images}/slot-a"
        run(["mkdir", "-p", images])
        run(["mount", "-o", "noatime", layout.images_partition, images])
        state.mounts.append(images)
        run(["mkdir", "-p", slot_dir])
        run(["cp", "--no-preserve=mode", archive, f"{slot_dir}/{overlay.SLOT_IMAGE}"],
            timeout=BOOTSTRAP_TIMEOUT)
        run(["cp", "--no-preserve=mode", verity, f"{slot_dir}/{overlay.SLOT_VERITY}"],
            timeout=BOOTSTRAP_TIMEOUT)
        for argv in overlay.prepare_commands(container):
            run(argv)
        run(["sync"], timeout=BOOTSTRAP_TIMEOUT)
        run(["mkdir", "-p", overlay.LOWER_MOUNT])
        run(["mount", "-t", "squashfs", "-o", "loop,ro", f"{slot_dir}/{overlay.SLOT_IMAGE}",
             overlay.LOWER_MOUNT])
        state.mounts.append(overlay.LOWER_MOUNT)
        run(["mkdir", "-p", layout.mount_root])
        run(overlay.mount_command(container, layout.mount_root))
        state.mounts.append(layout.mount_root)
        for _name, path in overlay.STATE_BINDS:
            run(["mkdir", "-p", f"{layout.mount_root}/{path}"])
        for argv in overlay.state_label_commands(container, layout.mount_root):
            run(argv)
        return overlay.LOWER_MOUNT

    def _overlay_root(self, config, metadata) -> bool:
        """Система на носителе -- всегда образ под overlay (LiveCD, разметка v3).

        `disk.system_image = false` (распакованное дерево) больше не
        поддерживается: в зашифрованном томе v3 нет места под систему, а дерево
        нельзя проверять dm-verity. Поле читается у старых конфигураций и
        ни на что не влияет; формат дистрибутива проверен раньше.
        """
        return distribution.is_squashfs(metadata.archive)

    def _run_bootstrap(self, backend, config, root, image_url, release, plan, dry_run, confirm_device) -> None:
        """Разворачивает базовую систему в `root` -- и на носитель, и в сборку.

        Проверка результата живёт здесь, а не у вызывающего, потому что нужна
        обоим: раньше она стояла только в сборке дистрибутива, и путь установки
        на носитель остался без неё.
        """
        argv = backend.bootstrap_command(root, config, image_url, release)
        self._run(argv, plan, dry_run, confirm_device, timeout=BOOTSTRAP_TIMEOUT)

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

    def _install_recovery_boot(
        self, backend, identity: MediaIdentity, config: InstallConfig,
        plan: CommandPlan, *, dry_run, confirm_device,
    ) -> None:
        """Puts the recovery flow where the boot environment can reach it.

        The module, its salt and its settings go onto the encrypted root; the
        family-specific hooks copy them into the initramfs when it is built.
        That is the only place they can be useful, because recovery happens
        before the root has been unlocked -- and it is why the medium keeps no
        second copy of the salt anywhere an attacker could read it.
        """
        layout = identity.layout
        self._install_recovery_module_files(backend, config, layout.mount_root, layout,
                                            plan, dry_run, confirm_device)
        target_dir = f"{layout.mount_root}/{RECOVERY_DIR}"
        self._write_file(
            f"{target_dir}/recovery_salt.bin", identity.recovery_salt, plan, dry_run,
            confirm_device, binary=True, mode=0o400,
        )
        # Everything the module needs to know about this particular medium.
        # The device is named by UUID: the medium boots on machines where the
        # disk ordering is different, and /dev/sda2 would be someone else's.
        self._write_file(
            f"{target_dir}/env",
            recovery_env(config, identity.media_id, layout.luks_uuid, layout.mapper_name),
            plan, dry_run, confirm_device, mode=0o400,
        )
        # Готовые строки запросов на все recovery-слоты. Загрузочная среда их
        # не вычисляет: считать base32 и sha256 в shell означало бы вторую
        # реализацию того же кодирования, и разошлись бы они молча -- на живой
        # загрузке у человека, оставшегося без системы.
        self._write_file(
            f"{target_dir}/requests",
            RecoveryRequest.table(identity.media_id, machine_binding.recovery_keyslots(config)),
            plan, dry_run, confirm_device, mode=0o400,
        )

    def _install_generic_boot(self, config, backend, root, plan, dry_run, confirm) -> bool:
        """Общий initramfs выпуска (Э2): хуки с данными носителя из `/poa/env`.

        Только для корня из образа (LiveCD) -- обычный корень записывается
        по-старому, с initramfs на каждом носителе. Возвращает, собран ли.
        """
        if not getattr(config.disk, "system_image", True):
            return False
        from . import medium_initrd

        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm, **kw)  # noqa: E731
        self._progress.step("Сборка общего initramfs")
        for relative, content in backend.boot_config_files(config).items():
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm)
        for relative, (content, mode) in backend.scripted_files(
            config, medium_initrd.GENERIC, image=self._image, overlay_root=True
        ).items():
            target = f"{root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm, mode=mode)
        self._install_recovery_module_files(backend, config, root, medium_initrd.GENERIC,
                                            plan, dry_run, confirm)
        relabel = backend.restore_labels_command(root, ("/etc", "/usr", "/boot"))
        if relabel is not None:
            run(relabel, timeout=BOOTSTRAP_TIMEOUT)
        run(backend.initramfs_command(root), timeout=BOOTSTRAP_TIMEOUT)
        return True

    def _write_medium_initrd(self, backend, identity, config, plan, dry_run, confirm_device) -> None:
        """Второй initrd носителя рядом с ядром (Э2): `/poa/env`, соль и
        запросы восстановления, у Debian -- строка корня для cryptroot."""
        from . import medium_initrd

        layout = identity.layout
        env = recovery_env(config, identity.media_id, layout.luks_uuid, layout.mapper_name,
                           layout.images_uuid)
        requests = RecoveryRequest.table(identity.media_id, machine_binding.recovery_keyslots(config))
        crypttab = None
        if backend.initramfs_crypttab_path:
            root_line = next(line for line in backend._crypttab(layout, config).splitlines()
                             if line.startswith(layout.mapper_name + "\t"))
            crypttab = (backend.initramfs_crypttab_path, root_line + "\n")
        archive = medium_initrd.medium_archive(env, identity.recovery_salt, requests, crypttab)
        self._write_file(f"{layout.mount_root}/boot/{medium_initrd.ARCHIVE_NAME}", archive,
                         plan, dry_run, confirm_device, binary=True, mode=0o600)

    def _install_recovery_module_files(self, backend, config, root, boot_layout,
                                       plan, dry_run, confirm_device) -> None:
        """Модуль восстановления, его экран и хуки семейства -- общее для всех
        носителей. `boot_layout` -- настоящая раскладка (старый путь) или
        `medium_initrd.GENERIC` (сборка общего initramfs, Э2)."""
        target_dir = f"{root}/{RECOVERY_DIR}"
        self._run(["mkdir", "-p", target_dir], plan, dry_run, confirm_device)
        self._run(["cp", RECOVERY_MODULE_SRC, f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device)
        self._run(["chmod", "0755", f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device)
        # Слой вывода рядом с модулем: его подключают и он сам, и хуки
        # привязки к машине.
        self._run(["cp", RECOVERY_UI_SRC, f"{target_dir}/ui.sh"], plan, dry_run, confirm_device)
        self._run(["chmod", "0644", f"{target_dir}/ui.sh"], plan, dry_run, confirm_device)
        # Тема plymouth: каталог и элементы поля ввода.
        #
        # Картинки берутся из штатной темы дистрибутива, и это не лень. Плагин
        # two-step рисует запрос пароля из готовых изображений -- рамки поля,
        # точки, замок, -- и без них показать его не может: plymouth молча
        # уходит в текстовый режим, где кириллицы нет. Тема без единой
        # картинки именно так и вела себя на прогоне 29.08.2026, и по экрану
        # это выглядело как «графика не включилась».
        #
        # Берутся только элементы поля. Фон, цвета и выравнивание -- свои (см.
        # backends/base.py), поэтому логотипа и полосы загрузки дистрибутива
        # на экране не будет.
        theme_dir = f"{root}/usr/share/plymouth/themes/{PLYMOUTH_THEME_NAME}"
        self._run(["mkdir", "-p", theme_dir], plan, dry_run, confirm_device)
        themes_root = f"{root}/usr/share/plymouth/themes"
        self._run(
            [
                "sh", "-c",
                # Первая тема, где эти элементы есть целиком. Перебор, а не
                # жёсткое имя: у семейств темы называются по-разному, и
                # пропавшая картинка стоила бы немого экрана.
                f'for d in "{themes_root}"/*/; do '
                f'  if [ -f "$d/entry.png" ] && [ -f "$d/bullet.png" ]; then '
                f'    cp -f "$d"/entry.png "$d"/bullet.png "{theme_dir}/" 2>/dev/null; '
                f'    for extra in box.png lock.png; do '
                f'      [ -f "$d/$extra" ] && cp -f "$d/$extra" "{theme_dir}/"; '
                f'    done; '
                f'    break; '
                f'  fi; '
                f'done; true',
            ],
            plan, dry_run, confirm_device,
        )
        for relative, (content, mode) in backend.recovery_boot_files(boot_layout, config).items():
            path = f"{root}/{relative}"
            self._run(["mkdir", "-p", path.rsplit("/", 1)[0]], plan, dry_run, confirm_device)
            self._write_file(path, content, plan, dry_run, confirm_device, mode=mode)

    def _fill_exchange(self, run, layout, sources) -> "list[str]":
        """Кладёт файлы администратора в раздел обмена, пока он смонтирован.

        Каталоги копируются целиком (`cp -a`): настройки программы -- это чаще
        дерево, чем один файл, и заставлять выбирать их по одному значит
        предлагать не пользоваться этим вовсе.

        Ошибка копирования не срывает запись носителя, а возвращается
        предупреждением. Носитель без файла настроек работоспособен, и файл
        кладётся потом через раздел «Обмен»; носитель, стёртый на
        девятнадцатом шаге из двадцати, не спасти ничем.
        """
        problems: list[str] = []
        if not sources:
            return problems
        target = f"{layout.mount_root}{EXCHANGE_MOUNTPOINT}"
        for source in sources:
            try:
                run(["cp", "-a", "--", str(source), f"{target}/"])
            except Exception as exc:  # noqa: BLE001
                problems.append(
                    f"не удалось положить в раздел обмена {source!r}: {exc}; "
                    "носитель записан, файл можно добавить через раздел «Обмен»"
                )
        return problems

    def _collect_boot_manifest(self, layout, dry_run: bool) -> "dict[str, str]":
        """Опись того, что исполняется до расшифровки корня.

        Считается в самом конце: раньше её делать нельзя, потому что
        initramfs собирается, а загрузчик ставится посреди развёртывания, и
        опись, снятая до них, описывала бы не тот носитель, который получит
        пользователь.

        В сухом прогоне ничего не читается -- разделы не смонтированы, и
        обходить нечего.
        """
        if dry_run:
            return {}
        return integrity.collect({
            "boot": f"{layout.mount_root}/boot",
            "esp": f"{layout.mount_root}/boot/efi",
        })

    def _install_self_check(
        self, identity, backend, plan, dry_run, confirm_device, overlay_upper: str = ""
    ) -> None:
        """Копия описи на зашифрованном корне и служба, которая её сверяет.

        Пишется после снятия описи и до размонтирования: корень ещё открыт, а
        незашифрованная часть уже окончательна.

        Пустая опись -- это сухой прогон, и класть на носитель пустой список
        значило бы получить службу, которая при каждой загрузке рапортует,
        что всё в порядке, ничего не проверив.
        """
        if not identity.boot_manifest:
            return
        run = lambda argv, **kw: self._run(argv, plan, dry_run, confirm_device, **kw)  # noqa: E731
        layout = identity.layout
        for relative, content in integrity.self_check_files(identity.boot_manifest).items():
            target = f"{layout.mount_root}/{relative}"
            run(["mkdir", "-p", target.rsplit("/", 1)[0]])
            self._write_file(target, content, plan, dry_run, confirm_device)
        run(["chmod", "0755", f"{layout.mount_root}/{integrity.SELF_CHECK_SCRIPT}"])
        # Служба включается ссылкой, а не `systemctl enable`: systemd целевой
        # системы здесь не запущен, и enable в chroot делает ровно эту ссылку.
        wants = f"{layout.mount_root}/etc/systemd/system/multi-user.target.wants"
        run(["mkdir", "-p", wants])
        run([
            "ln", "-sf", "/etc/systemd/system/poa-boot-integrity.service",
            f"{wants}/poa-boot-integrity.service",
        ])

        # Метки SELinux -- своими руками, потому что общий проход по /etc и
        # /boot прошёл раньше: опись снимается в самом конце, когда
        # незашифрованная часть окончательна, а перемечивание идёт до сборки
        # initramfs.
        #
        # Найдено прогоном 01.09.2026: у `poa-boot-integrity` метка была `?`,
        # тогда как у соседних poa-machine-key и poa-kiosk-apply -- `bin_t`.
        # Служба при этом не запускается и молчит: в журнале носителя не
        # оказалось ни строчки, хотя и файл, и ссылка на месте. Ровно этим
        # однажды сломался SDDM, не сумевший прочитать собственную настройку.
        # `/etc` целиком, а не только `/etc/systemd`: прогон 12.09.2026 нашёл
        # без метки `/etc/fstab` -- его развёртывание дописывает после разметки
        # эталона, как и crypttab с vconsole.conf. На overlay это ничего не
        # копирует наверх: `/etc` и так лежит в верхнем слое целиком.
        paths = ("/usr/local/sbin", "/etc", "/var/lib")
        if overlay_upper:
            # Корень собран overlay, а по нему setfiles не работает: он молча
            # пропускает такую файловую систему и выходит с нулём. Поэтому слой
            # изменений подставляется внутрь chroot как обычный каталог, и
            # метки ставятся прямо по нему.
            inside = "/mnt/poa-upper"
            run(["mkdir", "-p", f"{layout.mount_root}{inside}"])
            run(["mount", "--bind", overlay_upper, f"{layout.mount_root}{inside}"])
            try:
                relabel = backend.restore_labels_command(
                    layout.mount_root, paths, upper=inside
                )
                if relabel is not None:
                    run(relabel, timeout=BOOTSTRAP_TIMEOUT)
            finally:
                run(["umount", f"{layout.mount_root}{inside}"])
                run(["rmdir", f"{layout.mount_root}{inside}"])
            return

        relabel = backend.restore_labels_command(layout.mount_root, paths)
        if relabel is not None:
            run(relabel, timeout=BOOTSTRAP_TIMEOUT)

    def _install_recovery_module(
        self, identity: MediaIdentity, plan: CommandPlan, *, dry_run, confirm_device
    ) -> None:
        """"Установка.Загрузка": deploys the reference recovery script and its
        metadata onto the unencrypted ESP, where the admin tool can find it
        later without the user's passphrase.

        The script is still not wired into the bootloader/initramfs as an
        automatic recovery entry -- see README, раздел «Архитектура» "Known
        limitations"."""
        target_dir = f"{identity.layout.mount_root}/boot/efi/{ESP_METADATA_DIR}"
        self._run(["mkdir", "-p", target_dir], plan, dry_run, confirm_device)

        self._run(
            ["cp", RECOVERY_MODULE_SRC, f"{target_dir}/recovery_module.sh"], plan, dry_run, confirm_device
        )
        # Идентификатор кладётся целиком и таким, каким родился. Именно этот
        # файл читает приложение, опознавая носитель позже, и именно здесь
        # обрезанный ID стоил бы неузнанного носителя.
        self._write_file(
            f"{target_dir}/media_id.txt", identity.media_id + "\n", plan, dry_run, confirm_device
        )
        self._write_file(
            f"{target_dir}/recovery_salt.bin", identity.recovery_salt, plan, dry_run,
            confirm_device, binary=True,
        )

    # ------------------------------------------------------------------
    # Preflight / teardown
    # ------------------------------------------------------------------
    def _refuse_if_busy(self, device: BlockDevice, dry_run: bool) -> None:
        """Отказывается писать на носитель, который держит кто-то ещё.

        Проверка стоит после размонтирования и до первой разрушающей команды:
        размонтировать своё -- наша работа, а вот чужой процесс, пишущий в то
        же устройство, -- причина остановиться.

        Цена ошибки известна: прогон 04.09.2026 писал носитель одновременно с
        оставшейся запущенной виртуальной машиной. Все восемнадцать шагов
        прошли, приложение отчиталось «Носитель готов», а внутри системного
        тома не оказалось файловой системы -- загрузка встала, ожидая том по
        UUID, которого не существует. Обнаружить это дёшево, а последствие --
        носитель, который выглядит записанным и не грузится.
        """
        if dry_run:
            return
        holders = busy_holders(device.path)
        if not holders:
            return
        raise InstallError(
            f"носитель {device.path} занят: " + "; ".join(holders) + ". "
            "Запись в четыре руки испортит его молча -- закройте то, что его держит, "
            "и повторите. Открытые тома можно освободить на экране «Обслуживание»."
        )

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
        hide what actually went wrong.

        Порядок и повторы здесь не формальность. Прежняя версия делала
        `umount -l` и тут же `cryptsetup luksClose`. Ленивое размонтирование
        отцепляет точку сразу, но том освобождается только когда допишется
        кэш, а на носителе, пишущем несколько мегабайт в секунду, это минуты.
        Закрытие натыкалось на «устройство занято», ошибка съедалась — и от
        прерванного прогона оставался открытый /dev/mapper, из-за которого
        следующий запуск отказывался работать.
        """
        # Сначала выгнать кэш: тогда обычному umount нечего ждать.
        self._try(["sync"], plan, dry_run, confirm_device)
        for mountpoint in reversed(state.mounts):
            try:
                self._run(["umount", mountpoint], plan, dry_run, confirm_device)
            except Exception:  # noqa: BLE001 - см. docstring
                # Ленивое — только как запасной путь, а не как основной.
                self._try(["umount", "-l", mountpoint], plan, dry_run, confirm_device)
        state.mounts.clear()

        for mapper in reversed(state.service_mappers):
            self._close_mapper(mapper, plan, dry_run, confirm_device)
        state.service_mappers.clear()
        if state.mapper_opened:
            self._close_mapper(layout.mapper_name, plan, dry_run, confirm_device)
            state.mapper_opened = False
        self._try(["rmdir", layout.mount_root], plan, dry_run, confirm_device)
        if state.image is not None:
            state.image.unmount()
            state.image = None

    def _close_mapper(self, mapper: str, plan: CommandPlan, dry_run: bool, confirm_device: str | None) -> bool:
        """Закрывает том, дожидаясь, пока он освободится.

        Возвращает False, если так и не закрылся: об этом надо сказать вслух,
        иначе носитель останется занятым молча.
        """
        for attempt in range(TEARDOWN_CLOSE_ATTEMPTS):
            try:
                self._run(["cryptsetup", "luksClose", mapper], plan, dry_run, confirm_device)
                return True
            except Exception:  # noqa: BLE001 - см. docstring _teardown
                if dry_run or attempt == TEARDOWN_CLOSE_ATTEMPTS - 1:
                    break
                time.sleep(TEARDOWN_CLOSE_PAUSE)
        self._unreleased.append(f"/dev/mapper/{mapper}")
        return False

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
            layout.images_uuid = str(uuid.uuid4())
            layout.luks_uuid = str(uuid.uuid4())
            layout.logs_luks_uuid = str(uuid.uuid4())
            return

        layout.esp_uuid = _blkid_uuid(layout.esp_partition)
        layout.boot_uuid = _blkid_uuid(layout.boot_partition)
        layout.root_uuid = _blkid_uuid(layout.mapper_path)
        layout.logs_uuid = _blkid_uuid(layout.logs_mapper_path)
        layout.images_uuid = _blkid_uuid(layout.images_partition)
        layout.luks_uuid = _read_output(LuksManager.uuid_command(layout.system_partition))
        layout.logs_luks_uuid = _read_output(LuksManager.uuid_command(layout.logs_partition))


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
    #:
    #: 01.09.2026 к обоим путям добавился шаг описи загрузчика, и числа стали
    #: на единицу меньше правды: на прогоне это выглядело как «[17/17] Опись»,
    #: а следом «[18/18] Завершение». Тот же класс, что счётчик, показывавший
    #: 14 вместо 17. Считать надо по коду, а не по памяти.
    EXPECTED_STAGES = 19
    #: Building a distribution skips everything to do with a medium.
    BUILD_STAGES = 10
    #: Deploying one skips everything to do with packages.
    #: Восемь шагов подготовки носителя плюс восемь самого развёртывания и
    #: один на проверку дистрибутива. Считать надо по коду, а не по памяти:
    #: занижённое число упирает полосу в край задолго до конца, и человек
    #: решает, что программа зависла.
    DEPLOY_STAGES = 18

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


#: Возможности ext4, которые НЕ включаются при разметке носителя.
#:
#: Файловую систему создаёт машина администратора, а работать с ней будет
#: устанавливаемая система — и они разные. Debian trixie несёт e2fsprogs 1.47,
#: который по умолчанию включает `orphan_file`; e2fsck из РЕД ОС 8 (1.46) о
#: такой возможности не знает и отказывается проверять раздел:
#:
#:     /dev/mapper/poa-… has unsupported feature(s): FEATURE_C12
#:     e2fsck: Get a newer version of e2fsck!
#:     fsck failed with exit status 12
#:
#: Дальше systemd не монтирует корень и уходит в аварийный режим. Файловая
#: система при этом **исправна** — e2fsck новой версии проверяет её без
#: замечаний, — то есть носитель нерабочий не из-за повреждения, а из-за
#: несовпадения версий инструментов.
#:
#: Требование общее для обоих семейств: носитель форматируется здесь, а
#: читается там. `metadata_csum_seed` отключён по той же причине — он новее,
#: чем инструменты в старых выпусках.
CONSERVATIVE_EXT4_FEATURES = "^orphan_file,^metadata_csum_seed"

#: Сколько раз и с какой паузой пытаться закрыть том при уборке. Минута в
#: сумме: столько занимает дозапись кэша на медленном носителе, после чего
#: устройство освобождается само.
TEARDOWN_CLOSE_ATTEMPTS = 12
TEARDOWN_CLOSE_PAUSE = 5

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
