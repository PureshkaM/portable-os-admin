"""Локальное стирание ключей физически подключённого носителя.

Удалённый отзыв публикуется через revocation_publish. После его получения
носитель закрывает рабочий сеанс. Стирание возвращённого носителя требует
отдельного выбора устройства и подтверждения его пути.

Почему стирание живёт здесь, а не в установщике: установщик пишет носитель, а
это обратное действие над уже выданным, и делается оно из списка носителей, а
не из мастера.

Две защиты от ошибки в устройстве, и они разной природы:

1. **Путь повторяется вручную** -- та же последняя защита, что стоит перед
   записью носителя. Ловит промах человека: «хотел /dev/sdb, набрал /dev/sda».
2. **UUID томов сверяется с записью** в контейнере. Ловит то, чего человек не
   видит вовсе: путь набран верно, но за ночь порты переехали, и `/dev/sdb` --
   уже другая флешка. Стирание идёт только по тем разделам, чьи UUID записаны
   в контейнере при выдаче; ни один посторонний раздел этот модуль не трогает,
   даже если он лежит на том же устройстве.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..container import AdminContainer
from ..errors import InstallError, MediaNotRegisteredError
from ..install.command_runner import CommandPlan, CommandRunner
from ..install.devices import DeviceLister
from ..install.luks import LuksManager

#: Причина по умолчанию, если администратор своей не назвал. Стирание само по
#: себе -- достаточное объяснение, в отличие от пометки, где причина и есть
#: единственное содержание записи.
ERASED_REASON = "носитель возвращён администратору, заголовки LUKS стёрты"


@dataclass(frozen=True)
class ErasedVolume:
    """Один стёртый том: чем он был и на каком разделе лежал."""

    name: str
    partition: str


@dataclass(frozen=True)
class EraseResult:
    device_path: str
    volumes: "tuple[ErasedVolume, ...]"
    #: Команды, которые модуль выполнил (или собирался -- в сухом прогоне).
    commands: "tuple[tuple[str, ...], ...]" = ()

    def describe(self) -> str:
        listed = ", ".join(f"{v.name} ({v.partition})" for v in self.volumes)
        return f"{self.device_path}: стёрты заголовки томов — {listed}"


def _volumes_of(record, device) -> "tuple[ErasedVolume, ...]":
    """Разделы устройства, чьи UUID записаны за этим носителем.

    Сверка по UUID, а не по номеру раздела: разметка у носителя известна, но
    полагаться на неё значило бы стирать третий раздел чего угодно, что
    подключено по этому пути.
    """
    wanted = {
        uuid.lower(): name
        for name, uuid in (
            ("системный", record.luks_uuid),
            ("журналов", record.logs_luks_uuid),
            ("обмена", record.exchange_luks_uuid),
        )
        if uuid
    }
    found = []
    for partition in device.partitions:
        name = wanted.get((partition.uuid or "").lower())
        if name:
            found.append(ErasedVolume(name=name, partition=partition.path))
    return tuple(found)


def erase_medium(
    container: AdminContainer,
    media_id: str,
    device_path: str,
    confirm_device: str,
    *,
    reason: str = "",
    lister: "DeviceLister | None" = None,
    runner: "CommandRunner | None" = None,
    dry_run: bool = False,
) -> EraseResult:
    """Стереть вернувшийся носитель и пометить его запись отозванной.

    Пометка стоит здесь, а не на вызывающей стороне, и это не мелочь: стёртый
    носитель, оставшийся в списке действующим, -- худшее из состояний, потому
    что администратор будет считать выданным то, чего больше нет. Сделать одно
    без другого через интерфейс невозможно по построению.

    Порядок обратный по той же причине: сперва стирание, потом отметка. Если
    команда не прошла -- запись остаётся действующей, и это правда: носитель
    цел.
    """
    record = container.find_media(media_id)
    if record is None:
        raise MediaNotRegisteredError(
            f"сведения о носителе не найдены в контейнере (media_id={media_id})"
        )

    device_path = device_path.strip()
    if not device_path:
        raise InstallError("не указано устройство, на котором лежит носитель")
    if confirm_device.strip() != device_path:
        raise InstallError(
            "подтверждение устройства не совпадает с указанным путём — стирание отменено"
        )

    device = (lister or DeviceLister()).describe(device_path)
    if device is None:
        raise InstallError(
            f"устройство {device_path} не найдено — подключён ли носитель к этой машине?"
        )

    volumes = _volumes_of(record, device)
    if not volumes:
        # Отказ, а не стирание «на всякий случай»: подключено не то, что
        # думает администратор, и единственное безопасное действие здесь --
        # не делать ничего.
        raise InstallError(
            f"на {device_path} нет ни одного тома носителя «{record.display_name}» — "
            "подключено другое устройство, стирание отменено"
        )

    luks = LuksManager(runner or CommandRunner())
    commands: "list[tuple[str, ...]]" = []
    for volume in volumes:
        # Подтверждение здесь своё у каждого раздела: CommandRunner сверяет его
        # с устройством плана, а человек подтверждал носитель целиком -- и
        # подтвердил выше, по всему пути сразу.
        plan = CommandPlan(device=volume.partition)
        luks.erase_header(
            volume.partition,
            dry_run=dry_run,
            confirm_device=volume.partition,
            plan=plan,
        )
        commands.extend(tuple(result.argv) for result in plan.results)

    container.revoke_media(media_id, reason.strip() or ERASED_REASON, erased=True)
    return EraseResult(device_path=device_path, volumes=volumes, commands=tuple(commands))


__all__ = ["ERASED_REASON", "ErasedVolume", "EraseResult", "erase_medium"]
