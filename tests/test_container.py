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
