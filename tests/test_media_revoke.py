"""Отзыв носителя: стирание вернувшегося и учёт того, что не вернулся.

Стирание здесь всегда сухое (`dry_run=True`): проверяется, какие команды и по
каким разделам программа собирается выполнить, а не то, что делает cryptsetup.
Настоящее стирание проверяется на стенде, носителем в руках.
"""
from __future__ import annotations

import pytest

from portable_os_admin.admin import media_revoke
from portable_os_admin.container import (
    ACTION_ERASED,
    ContainerManager,
    MediaRecord,
)
from portable_os_admin.errors import InstallError, MediaNotRegisteredError
from portable_os_admin.install.devices import BlockDevice, Partition

SYSTEM_UUID = "523e0993-4dac-4eee-9f72-a272ce456d34"
LOGS_UUID = "bbbbbbbb-0000-0000-0000-000000000000"
EXCHANGE_UUID = "cccccccc-0000-0000-0000-000000000000"


class FakeLister:
    """Подставной опрос устройств: тесты идут и там, где нет ни одной флешки."""

    def __init__(self, device: "BlockDevice | None"):
        self._device = device

    def describe(self, path: str) -> "BlockDevice | None":
        if self._device is not None and self._device.path == path:
            return self._device
        return None


def make_device(path: str, uuids: "list[str]") -> BlockDevice:
    return BlockDevice(
        name=path.rsplit("/", 1)[-1],
        path=path,
        size_bytes=16 * 1024**3,
        model="Kingston DT",
        removable=True,
        mountpoints=(),
        partitions=tuple(
            Partition(
                path=f"{path}{index + 1}",
                size_bytes=1024**3,
                fstype="crypto_LUKS",
                label="",
                mountpoint="",
                uuid=uuid,
            )
            for index, uuid in enumerate(uuids)
        ),
    )


@pytest.fixture
def issued(tmp_path):
    """Контейнер с одним выданным носителем -- из трёх томов, как на самом деле."""
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    container.add_media_record(
        MediaRecord(
            media_id="46aed677",
            luks_uuid=SYSTEM_UUID,
            label="Kingston DT",
            name="Иванов, бухгалтерия",
            config_name="kiosk",
            target_os="debian",
            logs_luks_uuid=LOGS_UUID,
            exchange_luks_uuid=EXCHANGE_UUID,
        )
    )
    yield container
    container.close()


def test_erasing_a_returned_medium_kills_every_volume_it_has(issued):
    """Все три тома, а не один системный.

    Журналы и раздел обмена открываются служебным ключом из контейнера
    администратора -- то есть пережили бы стирание одного лишь системного тома
    и остались бы читаемыми. Отзыв, после которого половина данных носителя
    по-прежнему открывается, отзывом не является.

    Первый раздел устройства при этом не наш (ESP) и остаться должен нетронутым.
    """
    device = make_device("/dev/sdb", ["", SYSTEM_UUID, LOGS_UUID, EXCHANGE_UUID])

    result = media_revoke.erase_medium(
        issued,
        "46aed677",
        "/dev/sdb",
        "/dev/sdb",
        lister=FakeLister(device),
        dry_run=True,
    )

    assert [volume.partition for volume in result.volumes] == [
        "/dev/sdb2",
        "/dev/sdb3",
        "/dev/sdb4",
    ]
    assert [volume.name for volume in result.volumes] == ["системный", "журналов", "обмена"]
    erased = [argv for argv in result.commands if "luksErase" in argv]
    assert len(erased) == 3, "стёрты не все тома носителя"
    assert all("/dev/sdb1" not in argv for argv in result.commands), "затронут чужой раздел"


def test_a_mistyped_device_stops_everything(issued):
    """Повтор пути -- последняя защита, и она обязана быть настоящей.

    Не совпало -- ни одной команды, и запись остаётся действующей: носитель
    цел, отзывать нечего.
    """
    device = make_device("/dev/sdb", [SYSTEM_UUID])

    with pytest.raises(InstallError) as failure:
        media_revoke.erase_medium(
            issued,
            "46aed677",
            "/dev/sdb",
            "/dev/sda",
            lister=FakeLister(device),
            dry_run=True,
        )

    assert "подтверждение" in str(failure.value)
    record = issued.find_media("46aed677")
    assert not record.is_revoked, "запись отозвана, хотя стирание не выполнялось"
    assert issued.journal_for("46aed677") == []


def test_another_stick_in_the_same_port_is_refused(issued):
    """Путь набран верно, а флешка в нём другая.

    Этого человек не видит: за ночь порядок портов меняется, и `/dev/sdb`
    сегодня -- не вчерашний носитель. Сверка UUID с записью в контейнере ловит
    ровно этот случай, которого повтор пути поймать не может.
    """
    stranger = make_device("/dev/sdb", ["ffffffff-0000-0000-0000-000000000000"])

    with pytest.raises(InstallError) as failure:
        media_revoke.erase_medium(
            issued,
            "46aed677",
            "/dev/sdb",
            "/dev/sdb",
            lister=FakeLister(stranger),
            dry_run=True,
        )

    assert "другое устройство" in str(failure.value)
    assert not issued.find_media("46aed677").is_revoked


def test_a_device_that_is_not_there_is_refused(issued):
    with pytest.raises(InstallError) as failure:
        media_revoke.erase_medium(
            issued,
            "46aed677",
            "/dev/sdz",
            "/dev/sdz",
            lister=FakeLister(None),
            dry_run=True,
        )

    assert "не найдено" in str(failure.value)


def test_erasing_marks_the_record_revoked_by_itself(issued):
    """Стёртый носитель, оставшийся действующим, -- худшее из состояний.

    По списку администратор считал бы выданным то, чего больше нет, поэтому
    отметку ставит сам `erase_medium`, а не экран, который его вызвал.
    """
    device = make_device("/dev/sdb", [SYSTEM_UUID])

    media_revoke.erase_medium(
        issued, "46aed677", "/dev/sdb", "/dev/sdb", lister=FakeLister(device), dry_run=True
    )

    record = issued.find_media("46aed677")
    assert record.is_revoked
    assert record.revoked_at, "дата отзыва не проставлена"
    assert record.revoked_reason == media_revoke.ERASED_REASON

    # В журнале именно стирание, а не пометка: состояние после них одно, а
    # произошедшее -- разное, и через год это единственный способ отличить
    # мёртвый носитель от снятого с учёта.
    journal = issued.journal_for("46aed677")
    assert [entry.action for entry in journal] == [ACTION_ERASED]


def test_the_administrator_reason_survives_the_erasure(issued):
    device = make_device("/dev/sdb", [SYSTEM_UUID])

    media_revoke.erase_medium(
        issued,
        "46aed677",
        "/dev/sdb",
        "/dev/sdb",
        reason="сотрудник уволен, носитель сдан",
        lister=FakeLister(device),
        dry_run=True,
    )

    assert issued.find_media("46aed677").revoked_reason == "сотрудник уволен, носитель сдан"


def test_an_unknown_medium_cannot_be_erased(issued):
    with pytest.raises(MediaNotRegisteredError):
        media_revoke.erase_medium(
            issued, "нет такого", "/dev/sdb", "/dev/sdb", lister=FakeLister(None), dry_run=True
        )
