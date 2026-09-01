import pytest

from portable_os_admin.container import ContainerManager, MediaRecord, RecoveryRecord
from portable_os_admin.errors import (
    ContainerAlreadyExistsError,
    ContainerAuthenticationError,
    ContainerCorruptedError,
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
