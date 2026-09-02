"""Узнаёт свои носители среди подключённых -- не спрашивая пароля.

Зачем. Администратор держит десяток выданных носителей, и в списке они
различаются идентификаторами вроде `46aed677` и `218d2d33`. Когда человек
приносит флешку с вопросом «почему не грузится», первое, что нужно узнать, --
какая из строк списка соответствует тому, что сейчас в порту. Сличать
идентификатор глазами можно, но только зная, где он написан, а написан он
внутри зашифрованной системы.

Смотрим на все блочные устройства, а не только на «съёмные»: вопрос здесь не
«можно ли на это писать», а «не наш ли это носитель», и кардридер, который врёт
про съёмность, иначе остался бы неузнанным.

Узнаётся носитель по UUID томов LUKS. Эти UUID лежат в заголовках разделов,
читаются без всякого ключа -- заголовок для того и открыт, чтобы система знала,
что перед ней, -- и записаны в контейнере администратора при выдаче. Значит
совпадение UUID означает ровно одно: этот носитель выдан из этого контейнера.

Что это **не** значит: что носитель цел, что его не подменили и что систему на
нём не трогали. UUID -- имя, а не подпись; на вопрос о целостности отвечает
опись загрузчика (см. `install/integrity.py`), и она требует ключа.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..container import MediaRecord
from ..install.devices import DeviceLister


@dataclass(frozen=True)
class DetectedMedium:
    """Запись контейнера и устройство, в котором она узнана."""

    record: MediaRecord
    device_path: str
    model: str
    #: Каким томом узнали: системным, журнальным или обменным. Полезно в
    #: сообщении -- по одному лишь «узнан» непонятно, что именно совпало.
    matched_volume: str

    def describe(self) -> str:
        name = self.record.display_name
        model = f" ({self.model})" if self.model else ""
        return f"{name} — {self.device_path}{model}"


def _uuids_of(record: MediaRecord) -> "dict[str, str]":
    """UUID томов записи с именем каждого, пустые отброшены."""
    named = {
        "системный": record.luks_uuid,
        "журналов": record.logs_luks_uuid,
        "обмена": record.exchange_luks_uuid,
    }
    return {name: value.lower() for name, value in named.items() if value}


def detect(
    records: "list[MediaRecord] | tuple[MediaRecord, ...]",
    lister: DeviceLister | None = None,
) -> "list[DetectedMedium]":
    """Какие из записей контейнера сейчас подключены к этой машине.

    Порядок ответа -- порядок записей, а не порядок устройств: список на
    экране от подключения флешки не должен перестраиваться.
    """
    lister = lister or DeviceLister()
    try:
        devices = lister.list_all()
    except Exception:  # noqa: BLE001
        # Ни один сценарий администратора не должен ломаться оттого, что
        # опрос устройств не удался: это подсказка, а не обязанность.
        return []

    # UUID всех разделов всех съёмных устройств -> устройство.
    seen: dict[str, tuple[str, str]] = {}
    for device in devices:
        for partition in device.partitions:
            if partition.uuid:
                seen[partition.uuid.lower()] = (device.path, device.model)

    found: list[DetectedMedium] = []
    for record in records:
        for volume_name, uuid in _uuids_of(record).items():
            if uuid in seen:
                device_path, model = seen[uuid]
                found.append(
                    DetectedMedium(
                        record=record,
                        device_path=device_path,
                        model=model,
                        matched_volume=volume_name,
                    )
                )
                break  # одного совпадения достаточно: это тот же носитель
    return found


def detect_map(
    records: "list[MediaRecord] | tuple[MediaRecord, ...]",
    lister: DeviceLister | None = None,
) -> "dict[str, DetectedMedium]":
    """То же самое, но по идентификатору носителя -- для таблиц."""
    return {item.record.media_id: item for item in detect(records, lister)}


__all__ = ["DetectedMedium", "detect", "detect_map"]
