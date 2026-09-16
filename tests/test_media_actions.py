"""Действия над выданным носителем: проверяется служба, а не экран.

Главное здесь -- то, что секрет администратора больше не покидает слой служб,
и то, что переименование доходит до диска. Имя, показанное как сохранённое и
пропавшее при закрытии программы, -- это носитель, который через месяц никто
не опознает.
"""
from __future__ import annotations

import pytest

from portable_os_admin.admin import media_actions, media_detect
from portable_os_admin.errors import PortableOSAdminError
from portable_os_admin.install import integrity


class _Record:
    def __init__(self, media_id, name="", manifest=None, signature=""):
        self.media_id = media_id
        self.name = name
        self.label = f"метка {media_id}"
        self.boot_manifest = manifest or {}
        self.boot_manifest_signature = signature

    @property
    def display_name(self):
        return self.name or self.label


class _Data:
    def __init__(self, records):
        self.media_records = list(records)


class _Container:
    def __init__(self, records=(), secret=b"admin-secret"):
        self.data = _Data(records)
        self.admin_secret = secret

    def find_media(self, media_id):
        return next((r for r in self.data.media_records if r.media_id == media_id), None)


class _Session:
    def __init__(self, container):
        self.container = container


class _Containers:
    """Запоминает, что сохраняли и сколько раз."""

    def __init__(self):
        self.saved = []

    def save(self, container):
        self.saved.append(container)


def _service(records=(), secret=b"admin-secret"):
    container = _Container(records, secret)
    containers = _Containers()
    return media_actions.MediaActions(_Session(container), containers), container, containers


def test_the_list_of_media_comes_from_the_container():
    service, container, _ = _service([_Record("aaaabbbb-11112222")])
    assert [r.media_id for r in service.records()] == ["aaaabbbb-11112222"]
    assert service.records()[0] is container.data.media_records[0]


def test_the_list_handed_out_is_a_snapshot():
    """Экран, который по недосмотру удалит строку из выданного списка, не
    должен удалить её из контейнера."""
    service, container, _ = _service([_Record("aaaabbbb-11112222")])
    handed = service.records()
    assert isinstance(handed, tuple)
    assert len(container.data.media_records) == 1


def test_an_unknown_medium_is_simply_not_found():
    service, _, _ = _service([_Record("aaaabbbb-11112222")])
    assert service.find("нет такого") is None
    assert service.find("") is None


def test_a_rename_reaches_the_disk():
    """Переименование, не дошедшее до диска, выглядит на экране успешным и
    пропадает при закрытии программы."""
    record = _Record("aaaabbbb-11112222")
    service, container, containers = _service([record])

    returned = service.rename("aaaabbbb-11112222", "  Иванов, бухгалтерия  ")

    assert returned is record
    assert record.name == "Иванов, бухгалтерия", "имя не обрезано по краям"
    assert containers.saved == [container], "контейнер не сохранён"


def test_renaming_a_medium_that_is_not_there_is_refused():
    service, _, containers = _service([])
    with pytest.raises(PortableOSAdminError):
        service.rename("aaaabbbb-11112222", "имя")
    assert containers.saved == [], "сохранение при неудаче"


def test_integrity_needs_a_manifest_to_compare_with():
    service, _, _ = _service([_Record("aaaabbbb-11112222")])
    with pytest.raises(PortableOSAdminError) as caught:
        service.check_integrity(service.records()[0])
    assert "описи" in str(caught.value)


def test_integrity_refuses_a_manifest_signed_by_another_container():
    """Опись лежит в зашифрованном контейнере, но проверить подпись всё равно
    надо: файл мог быть склеен из двух, и сравнение шло бы с чужим эталоном."""
    manifest = {"esp:EFI/BOOT/BOOTX64.EFI": "0" * 64}
    record = _Record(
        "aaaabbbb-11112222",
        manifest=manifest,
        signature=integrity.sign(manifest, b"another-container"),
    )
    service, _, _ = _service([record], secret=b"admin-secret")
    with pytest.raises(PortableOSAdminError) as caught:
        service.check_integrity(record)
    assert "одпись" in str(caught.value)


def test_integrity_says_when_the_medium_is_not_plugged_in(monkeypatch):
    """Отсутствие носителя -- сообщение человеку, а не ошибка программы."""
    secret = b"admin-secret"
    manifest = {"esp:EFI/BOOT/BOOTX64.EFI": "0" * 64}
    record = _Record(
        "aaaabbbb-11112222", manifest=manifest, signature=integrity.sign(manifest, secret)
    )
    service, _, _ = _service([record], secret=secret)
    monkeypatch.setattr(media_detect, "detect_map", lambda records: {})

    with pytest.raises(PortableOSAdminError) as caught:
        service.check_integrity(record)
    assert "не найден среди подключённых" in str(caught.value)


def test_the_medium_is_looked_for_by_volume_uuid_not_by_path(monkeypatch):
    """Та же флешка на этой машине /dev/sdb, на соседней /dev/sdc. Ошибка
    здесь означала бы сверку описи одного носителя с загрузчиком другого."""
    asked = []
    records = [_Record("aaaabbbb-11112222"), _Record("ccccdddd-33334444")]
    service, _, _ = _service(records)
    monkeypatch.setattr(
        media_detect, "detect_map", lambda given: asked.append(tuple(given)) or {}
    )

    service.connected()

    assert asked == [tuple(records)], "опознание получило не те записи"
