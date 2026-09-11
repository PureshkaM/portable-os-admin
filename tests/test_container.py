import base64
import json

import pytest

from portable_os_admin import crypto
from portable_os_admin.container import ContainerManager, MediaRecord, RecoveryRecord
from portable_os_admin.container.manager import CONTAINER_FORMAT
from portable_os_admin.errors import (
    ContainerAlreadyExistsError,
    ContainerAuthenticationError,
    ContainerCorruptedError,
    MediaNotRegisteredError,
    WeakPasswordError,
)


def test_create_and_reopen_round_trip(tmp_path):
    cm = ContainerManager()
    path = tmp_path / "admin.container"
    container = cm.create(path, "CorrectHorse123")
    container.add_media_record(
        MediaRecord(media_id="ab12cd34", luks_uuid="u-1", label="stick", config_name="cfg", target_os="debian")
    )
    cm.save(container)

    reopened = cm.open(path, "CorrectHorse123")
    assert len(reopened.data.media_records) == 1
    assert reopened.data.media_records[0].media_id == "ab12cd34"


def test_create_rejects_weak_password(tmp_path):
    cm = ContainerManager()
    with pytest.raises(WeakPasswordError):
        cm.create(tmp_path / "admin.container", "short1")


def test_create_rejects_existing_path(tmp_path):
    cm = ContainerManager()
    path = tmp_path / "admin.container"
    cm.create(path, "CorrectHorse123")
    with pytest.raises(ContainerAlreadyExistsError):
        cm.create(path, "AnotherPassword123")


def test_open_rejects_wrong_password(tmp_path):
    cm = ContainerManager()
    path = tmp_path / "admin.container"
    cm.create(path, "CorrectHorse123")
    with pytest.raises(ContainerAuthenticationError):
        cm.open(path, "WrongPassword123")


def test_open_missing_file_raises_corrupted(tmp_path):
    cm = ContainerManager()
    with pytest.raises(ContainerCorruptedError):
        cm.open(tmp_path / "does-not-exist.container", "whatever123")


def test_open_garbage_file_raises_corrupted(tmp_path):
    path = tmp_path / "admin.container"
    path.write_text("not json at all {{{")
    cm = ContainerManager()
    with pytest.raises(ContainerCorruptedError):
        cm.open(path, "whatever123")


def test_recovery_record_lookup(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    container.add_recovery_record(RecoveryRecord(media_id="m1", slot=7, code="ABCD-EFGH", salt_b64="c2FsdA=="))
    found = container.find_recovery("m1", 7)
    assert found is not None
    assert container.find_recovery("m1", 8) is None
    assert container.find_recovery("other", 7) is None


def test_close_scrubs_key(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    key_before = container.key
    assert any(b != 0 for b in key_before)
    container.close()
    with pytest.raises(RuntimeError):
        _ = container.key


# ----------------------------------------------------------------------
# Смена пароля контейнера
# ----------------------------------------------------------------------
def test_changing_the_password_keeps_everything_the_container_holds(tmp_path):
    """Главное свойство: носителей смена пароля не касается.

    Пароль шифрует файл, а коды восстановления выводятся из секрета
    администратора, который лежит внутри. Останься секрет прежним — носитель,
    собранный вчера, откроется теми же кодами; смени его — пришлось бы
    пересобирать все носители, какие этот администратор когда-либо выдал.
    """
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    container = manager.create(path, "CorrectHorse123")
    secret_before = container.admin_secret
    container.add_media_record(
        MediaRecord(media_id="abcd1234", luks_uuid="u-9", label="стенд",
                    config_name="k", target_os="redos")
    )
    manager.save(container)

    manager.change_password(container, "AnotherHorse456")

    reopened = manager.open(path, "AnotherHorse456")
    assert reopened.admin_secret == secret_before, "секрет администратора изменился"
    assert reopened.find_media("abcd1234") is not None, "записи носителей потерялись"


def test_the_old_password_stops_working(tmp_path):
    """Иначе смена пароля не смена, а добавление второго."""
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    container = manager.create(path, "CorrectHorse123")
    manager.change_password(container, "AnotherHorse456")

    with pytest.raises(ContainerAuthenticationError):
        manager.open(path, "CorrectHorse123")


def test_the_salt_is_new_after_a_change(tmp_path):
    """Соль не переиспользуется.

    Она не секрет, но одна и та же соль на два пароля означала бы, что по двум
    версиям файла видно, менялся пароль или нет, а перебор по обеим ведётся за
    одну цену.
    """
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    container = manager.create(path, "CorrectHorse123")
    salt_before = container.kdf_params.salt

    manager.change_password(container, "AnotherHorse456")

    assert container.kdf_params.salt != salt_before


def test_a_weak_new_password_is_refused(tmp_path):
    """Проверка силы та же, что при создании: слабость не становится
    допустимой оттого, что контейнер уже существует."""
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    container = manager.create(path, "CorrectHorse123")

    with pytest.raises(WeakPasswordError):
        manager.change_password(container, "123")

    # Старый пароль продолжает работать: отказ ничего не менял.
    assert manager.open(path, "CorrectHorse123") is not None


# ----------------------------------------------------------------------
# Отзыв носителя (задача 32)
# ----------------------------------------------------------------------
def _issued(container, media_id: str = "46aed677") -> MediaRecord:
    record = MediaRecord(
        media_id=media_id,
        luks_uuid="u-1",
        label="Kingston DT",
        name="Иванов, бухгалтерия",
        config_name="kiosk",
        target_os="debian",
    )
    container.add_media_record(record)
    return record


def test_a_new_record_is_in_service(tmp_path):
    """Умолчание -- «действует»: отзыв это событие, а не исходное состояние."""
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    record = _issued(container)

    assert not record.is_revoked
    assert record.status_text == "действует"


def test_revoking_keeps_the_record_and_remembers_why(tmp_path):
    """Отзыв -- факт учёта, а не удаление истории.

    Сотрудник уволился, а носитель остаётся фактом: на нём стоит `media_id`,
    по нему приходят запросы восстановления, и он числится выданным. Удалить
    запись значило бы стереть историю ради того, чтобы список выглядел короче.
    """
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    container = manager.create(path, "CorrectHorse123")
    _issued(container)

    container.revoke_media("46aed677", "сотрудник уволен 11.09.2026")
    manager.save(container)

    reopened = manager.open(path, "CorrectHorse123")
    record = reopened.find_media("46aed677")
    assert record is not None, "запись исчезла из контейнера"
    assert record.is_revoked
    assert record.revoked_reason == "сотрудник уволен 11.09.2026"
    assert record.revoked_at, "дата отзыва не проставлена"
    assert record.status_text.startswith("отозван")


def test_revocation_can_be_undone_and_both_facts_stay_in_the_journal(tmp_path):
    """Отзыв -- решение человека, а человек ошибается строкой.

    Состояние после отмены прежнее, но история остаётся: журнал отвечает на
    вопрос, что с носителем делали, а поле состояния -- только на вопрос, как
    дела сейчас.
    """
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    _issued(container)

    container.revoke_media("46aed677", "ошибся строкой")
    container.restore_media("46aed677")

    record = container.find_media("46aed677")
    assert not record.is_revoked
    assert record.revoked_reason == "", "причина пережила снятие отметки"

    # Журнал отдаётся новыми сверху: смотрят в него ради последнего.
    journal = container.journal_for("46aed677")
    assert [entry.action for entry in journal] == ["media_restored", "media_revoked"]
    assert journal[1].details == "ошибся строкой"


def test_the_journal_keeps_media_apart(tmp_path):
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    _issued(container, "46aed677")
    _issued(container, "218d2d33")

    container.revoke_media("46aed677", "уволен")

    assert len(container.journal_for("46aed677")) == 1
    assert container.journal_for("218d2d33") == []


def test_revoking_an_unknown_medium_is_refused(tmp_path):
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")

    with pytest.raises(MediaNotRegisteredError):
        container.revoke_media("нет такого", "причина")
    with pytest.raises(MediaNotRegisteredError):
        container.restore_media("нет такого")


# ----------------------------------------------------------------------
# Совместимость формата: контейнер переживает обновление программы
# ----------------------------------------------------------------------
def _write_old_container(path, password: str, payload: dict) -> None:
    """Пишет контейнер так, как его писала прежняя версия программы.

    Своими руками, а не текущим `save`: смысл проверки в том, что на диске
    лежит файл **старого** формата. Пропустив его через нынешний код, мы
    проверили бы лишь то, что он согласуется сам с собой.
    """
    kdf = crypto.default_kdf_params()
    key = crypto.derive_key(password, kdf)
    nonce, ciphertext = crypto.encrypt(key, json.dumps(payload).encode("utf-8"))
    path.write_text(
        json.dumps(
            {
                "format": CONTAINER_FORMAT,
                "version": 1,
                "kdf": kdf.to_dict(),
                "cipher": "AES-256-GCM",
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def test_a_container_written_before_revocation_still_opens(tmp_path):
    """Контейнеры живут дольше кода, и это в проекте уже обжигали.

    02.09.2026 конфигурации прежней версии перестали открываться после
    переименования одного поля -- список падал с `TypeError`, а экран
    оставался пустым. Здесь та же опасность и то же лекарство: незнакомые
    ключи отбрасываются, отсутствующие берут умолчание.
    """
    path = tmp_path / "admin.container"
    _write_old_container(
        path,
        "CorrectHorse123",
        {
            # Ровно те поля, что были в записи до 11.09.2026: ни состояния,
            # ни журнала.
            "media_records": [
                {
                    "media_id": "46aed677",
                    "luks_uuid": "u-1",
                    "label": "Kingston DT",
                    "config_name": "kiosk",
                    "target_os": "debian",
                    "created_at": "2026-09-01T10:00:00+00:00",
                }
            ],
            "recovery_records": [],
            "admin_secret_b64": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LTI=",
        },
    )

    container = ContainerManager().open(path, "CorrectHorse123")

    record = container.find_media("46aed677")
    assert record is not None, "запись прежней версии не прочиталась"
    # Носитель, выданный до появления отзыва, действует: отсутствие отметки
    # означает именно это, а не «состояние неизвестно».
    assert not record.is_revoked
    assert container.data.journal == []
    # И он отзывается как любой другой -- миграция не оставляет запись
    # второсортной.
    container.revoke_media("46aed677", "сотрудник уволен")
    assert container.find_media("46aed677").is_revoked


def test_a_container_from_a_newer_version_still_opens(tmp_path):
    """Разницу версий нельзя сообщать отказом открыть контейнер.

    Программа старше файла -- обычный случай в организации, где обновляются не
    все и не сразу. Понятное читается, незнакомое отбрасывается.
    """
    path = tmp_path / "admin.container"
    _write_old_container(
        path,
        "CorrectHorse123",
        {
            "media_records": [
                {
                    "media_id": "46aed677",
                    "luks_uuid": "u-1",
                    "label": "Kingston DT",
                    "config_name": "kiosk",
                    "target_os": "debian",
                    "status": "revoked",
                    "revoked_at": "2026-09-11T10:00:00+00:00",
                    "revoked_reason": "уволен",
                    "поле_из_будущего": "что-то ещё",
                }
            ],
            "recovery_records": [],
            "journal": [
                {
                    "at": "2026-09-11T10:00:00+00:00",
                    "action": "media_revoked",
                    "media_id": "46aed677",
                    "details": "уволен",
                    "кто_именно": "неизвестное поле",
                }
            ],
            "admin_secret_b64": "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LTI=",
            "раздел_из_будущего": {"что-то": "ещё"},
        },
    )

    container = ContainerManager().open(path, "CorrectHorse123")

    assert container.find_media("46aed677").is_revoked
    assert [entry.action for entry in container.journal_for("46aed677")] == ["media_revoked"]


def test_the_old_key_does_not_stay_in_memory(tmp_path):
    """Старый ключ затирается на месте, как при закрытии контейнера.

    Требования к безопасности-10: ключ живёт ровно столько, сколько нужен, и
    смена пароля -- ровно тот случай, когда старый не нужен больше ни секунды.
    """
    manager = ContainerManager()
    container = manager.create(tmp_path / "admin.container", "CorrectHorse123")
    key_before = container.key

    manager.change_password(container, "AnotherHorse456")

    assert container.key != key_before
