"""Partition-table planning and creation ("Установка.Разметка").

Layout written to the device, in order: boot/ESP, LUKS/system (rest of the
usable space minus the two service partitions below), log partition,
exchange partition -- matching "Конфигурация.Состав" and the "Разметка"
step of the activity diagram (Рисунок 2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config.schema import DiskLayoutConfig
from .command_runner import CommandPlan, CommandRunner

MIB = 1024 * 1024


@dataclass(frozen=True)
class NegativeOffset:
    """parted accepts negative offsets ("-N MiB") to mean "N MiB before the
    end of the disk"; this wraps that so a PartitionSpec position can be
    either an absolute MiB offset from the start, or one of these."""

    mib_from_end: int

    def __str__(self) -> str:
        return f"-{self.mib_from_end}MiB"


@dataclass(frozen=True)
class PartitionSpec:
    number: int
    label: str
    fs_hint: str  # parted's filesystem-type hint, e.g. "fat32", "ext4", or "" for the LUKS partition
    start_mib: "int | NegativeOffset"
    end_mib: "int | NegativeOffset | None"  # None means "100%" (rest of the disk)


@dataclass(frozen=True)
class PartitionPlan:
    device: str
    partitions: tuple[PartitionSpec, ...]

    def partition_path(self, spec: PartitionSpec) -> str:
        # NVMe/mmcblk devices need a "p" separator before the partition number.
        if self.device[-1].isdigit():
            return f"{self.device}p{spec.number}"
        return f"{self.device}{spec.number}"


@dataclass
class MediaLayout:
    """Concrete device/partition/mapper paths for one install, plus the UUIDs
    discovered after the filesystems exist. Passed to the backend so it can
    render fstab/crypttab against real UUIDs instead of guessed device names
    (which reorder the moment the stick is plugged into another port)."""

    device: str
    esp_partition: str
    boot_partition: str
    system_partition: str  # the raw partition holding the LUKS container
    logs_partition: str
    #: Раздел образов слотов (разметка v3): squashfs и дерево dm-verity.
    images_partition: str
    mapper_name: str
    mapper_path: str
    mount_root: str
    #: Идентификатор носителя целиком. Раньше его восстанавливали обратно из
    #: `mapper_name`, отрезая часть после последнего дефиса; с 1.1.0 ID сам
    #: содержит дефис (приставка администратора), и такое восстановление
    #: отдавало половину -- носитель спрашивал сервер о несуществующем
    #: идентификаторе (живой прогон 16.09.2026). Теперь он едет полем.
    media_id: str = ""
    esp_uuid: str = ""
    boot_uuid: str = ""
    root_uuid: str = ""
    luks_uuid: str = ""
    #: filesystem UUIDs *inside* the opened mappings
    logs_uuid: str = ""
    #: ФС раздела образов -- по нему initramfs находит образ слота.
    images_uuid: str = ""
    #: LUKS container UUIDs of the two service partitions. Separate from the
    #: filesystem UUIDs above because crypttab addresses the container and
    #: fstab addresses what is inside it.
    logs_luks_uuid: str = ""
    #: Раздела обмена в разметке v3 нет (упразднён 01.10.2026). Поле остаётся
    #: пустым: его читают отзыв и записи контейнера, где пусто -- «нет тома».
    exchange_luks_uuid: str = ""
    #: Слот, в который пишется образ, и корневой хеш его дерева dm-verity --
    #: их называет командная строка ядра в grub.cfg (до Э4).
    slot: str = "a"
    root_hash: str = ""

    @property
    def container_root(self) -> str:
        """Куда монтируется сам том, когда система хранится сжатым образом.

        При overlay корнем становится не том, а конструкция поверх него, и
        точки нужны две: тому -- эта, собранной системе -- `mount_root`.
        Соседний каталог, а не подкаталог корня: подкаталог оказался бы
        внутри overlay, то есть внутри того, что сам же и держит.
        """
        return f"{self.mount_root}-container"

    @property
    def images_root(self) -> str:
        """Куда монтируется раздел образов на время записи."""
        return f"{self.mount_root}-images"

    @property
    def logs_mapper_name(self) -> str:
        return f"{self.mapper_name}-logs"

    @property
    def logs_mapper_path(self) -> str:
        return f"/dev/mapper/{self.logs_mapper_name}"



@dataclass
class MediaIdentity:
    """Всё, что делает носитель именно этим носителем, -- одним объектом.

    Собирается один раз, в начале записи, и едет от места рождения до тех, кто
    пишет файлы носителя. Смысл именно в этом, а не в укорачивании списков
    аргументов: пока те же значения ехали по отдельности, каждый путь записи
    брал их откуда придётся, и появлялись «короткие пути» вроде восстановления
    ID из имени dm-отображения. Прогон 16.09.2026 показал цену: с приставкой
    администратора в ID (`51839da0-8b860df2`) носитель спрашивал сервер отзыва
    о половине своего идентификатора и получал 403 на каждой проверке.

    Раскладка (`layout`) -- часть удостоверения, а не отдельная вещь: UUID
    томов появляются при записи этого носителя и больше нигде не повторяются.

    Ключи лежат здесь же, потому что рождаются в том же месте и в тот же
    момент, что и всё остальное: носитель без своего ключа неотличим от
    другого такого же, а это и есть отсутствие удостоверения.
    """

    media_id: str
    layout: MediaLayout
    #: Разметка, по которой носитель размечен. Едет полем, чтобы её не
    #: вычисляли второй раз там, где она уже посчитана: два независимых
    #: расчёта из одних входных данных совпадают ровно до первого расхождения
    #: в аргументах, и заметить такое расхождение нечем.
    partition_plan: PartitionPlan
    #: Ключ администратора для служебных томов. Второй копии нет нигде: без
    #: него журналы и обмен этого носителя больше не открыть.
    service_key: bytes = b""
    #: Ключ-файл на зашифрованном корне: им носитель сам открывает служебные
    #: тома при загрузке, не спрашивая второй парольной фразы.
    boot_key: bytes = b""
    #: Первый из выданных кодов восстановления и его соль -- те, что уезжают в
    #: загрузочную среду.
    recovery_code: str = ""
    recovery_salt: bytes = b""
    #: Все выданные коды: (слот, код, соль). Каждый расходуется безвозвратно,
    #: поэтому их столько, сколько keyslot осталось после слотов машин.
    issued_recovery: tuple[tuple[int, str, bytes], ...] = ()
    #: Открытая часть ключа носителя: её получает сервер отзыва и по ней
    #: проверяет запросы именно этого носителя.
    device_public_key_b64: str = ""
    #: Опись незашифрованной части: путь -> sha256. Снимается в самом конце
    #: записи, когда загрузчик уже стоит, а initramfs собран.
    boot_manifest: dict[str, str] = field(default_factory=dict)


#: Версия разметки носителя. 3 -- LIVE-2.0 (Э3b/Э19, 02.10.2026): образы
#: слотов A/B файлами на отдельном разделе, LUKS -- только слой носителя и
#: состояние, раздела обмена нет. Решение -- `docs/LIVE-2.0.md`, «Э3b/Э19».
LAYOUT_VERSION = 3


def plan_partitions(device: str, layout: DiskLayoutConfig) -> PartitionPlan:
    """ESP, ядра слотов, LUKS-хранилище и журналы -- фиксированного размера;
    образы слотов -- всё остальное. Метки `POA_BOOT` и `POA_SYSTEM` прежние:
    смысл тот же -- раздел загрузки (теперь с ядрами слотов) и зашифрованный
    том носителя (теперь без системы: она в образе на `POA_IMAGES`)."""
    esp_end = layout.esp_partition_mb
    boot_end = esp_end + layout.boot_partition_mb
    state_end = boot_end + layout.state_partition_mb
    logs_end = state_end + layout.log_partition_mb
    partitions = (
        PartitionSpec(1, "POA_ESP", "fat32", 1, esp_end),
        PartitionSpec(2, "POA_BOOT", "ext4", esp_end, boot_end),
        PartitionSpec(3, "POA_SYSTEM", "", boot_end, state_end),
        PartitionSpec(4, "POA_LOGS", "ext4", state_end, logs_end),
        PartitionSpec(5, "POA_IMAGES", "ext4", logs_end, None),
    )
    return PartitionPlan(device=device, partitions=partitions)


def build_media_layout(
    plan: PartitionPlan, mapper_name: str, mount_root: str, media_id: str = ""
) -> MediaLayout:
    esp, boot, system, logs, images = (plan.partition_path(spec) for spec in plan.partitions)
    return MediaLayout(
        device=plan.device,
        esp_partition=esp,
        boot_partition=boot,
        system_partition=system,
        logs_partition=logs,
        images_partition=images,
        mapper_name=mapper_name,
        mapper_path=f"/dev/mapper/{mapper_name}",
        mount_root=mount_root,
    )


def build_media_identity(media_id: str, device: str, disk: DiskLayoutConfig) -> MediaIdentity:
    """Собирает удостоверение носителя из его идентификатора.

    Имя dm-отображения и точка монтирования выводятся ЗДЕСЬ, из ID, и больше
    нигде. Направление важно: из идентификатора имя получается однозначно, а
    обратно -- нет. Обратное восстановление (`mapper_name.rsplit("-", 1)[-1]`)
    и было дефектом: пока ID состоял из восьми знаков, оно давало верный
    ответ, а с приставкой администратора стало отдавать половину.

    Один помощник на оба пути записи -- установку с образа и развёртывание
    дистрибутива: пока каждый собирал раскладку сам, новое поле попадало в
    один путь и не попадало в другой.
    """
    plan = plan_partitions(device, disk)
    return MediaIdentity(
        media_id=media_id,
        layout=build_media_layout(
            plan, f"poa-{media_id}", f"/mnt/poa-install-{media_id}", media_id
        ),
        partition_plan=plan,
    )


def _format_position(position: "int | NegativeOffset") -> str:
    if isinstance(position, NegativeOffset):
        return str(position)
    return f"{position}MiB"


class DiskManager:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def commands_for(self, plan: PartitionPlan, existing_partitions: tuple[str, ...] = ()) -> list[list[str]]:
        # Erase before writing anything new. Leftover filesystem, LUKS and RAID
        # signatures survive a fresh partition table and then confuse blkid,
        # the initramfs and the bootloader into finding two candidate
        # filesystems at the same offsets -- a medium that was previously
        # installed by this very tool is the likeliest case.
        #
        # Order matters: signatures inside each old partition go first (once
        # the partition table is gone their offsets are unknown), then the
        # disk's own signatures, then sgdisk --zap-all which clears both the
        # primary GPT and its backup copy at the end of the device. Wiping only
        # the head of the disk leaves that backup behind, and the kernel
        # happily restores the old layout from it.
        cmds: list[list[str]] = []
        for partition in existing_partitions:
            cmds.append(["wipefs", "--all", "--force", partition])
        # Сообщить флешке, что все её блоки свободны. Запись в предварительно
        # стёртый блок идёт заметно быстрее, чем в занятый: иначе контроллер
        # стирает страницу прямо во время записи. Не все носители это умеют,
        # поэтому команда выполняется как необязательная.
        # `|| true`, потому что необязательная: дешёвые носители TRIM не
        # поддерживают, и отказ здесь не повод срывать установку.
        cmds.append(["sh", "-c", f"blkdiscard --force {plan.device} || true"])
        cmds += [
            ["wipefs", "--all", "--force", plan.device],
            ["sgdisk", "--zap-all", plan.device],
            ["parted", "--script", plan.device, "--", "mklabel", "gpt"],
        ]
        for spec in plan.partitions:
            start = _format_position(spec.start_mib)
            end = "100%" if spec.end_mib is None else _format_position(spec.end_mib)
            fs_arg = [spec.fs_hint] if spec.fs_hint else []
            cmds.append(["parted", "--script", plan.device, "--", "mkpart", spec.label, *fs_arg, start, end])
        cmds.append(["parted", "--script", plan.device, "set", "1", "esp", "on"])
        # The kernel does not necessarily see the new partitions the instant
        # parted returns; without these, the mkfs calls that follow can hit a
        # /dev/sdaN node that does not exist yet.
        cmds.append(["partprobe", plan.device])
        cmds.append(["udevadm", "settle"])
        return cmds

    def apply(
        self,
        plan: PartitionPlan,
        *,
        dry_run: bool = True,
        confirm_device: str | None = None,
        command_plan: CommandPlan | None = None,
        existing_partitions: tuple[str, ...] = (),
    ) -> CommandPlan:
        command_plan = command_plan if command_plan is not None else CommandPlan(device=plan.device)
        for argv in self.commands_for(plan, existing_partitions):
            self.runner.run(argv, command_plan, dry_run=dry_run, confirm_device=confirm_device)
        return command_plan
