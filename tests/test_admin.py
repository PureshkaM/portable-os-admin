import pytest

from portable_os_admin.admin import ExchangeManager, LogViewer, find_media_by_boot_metadata, read_media_id
from portable_os_admin.container import ContainerManager, MediaRecord
from portable_os_admin.errors import (
    ExchangeError,
    InsufficientSpaceError,
    LogsCorruptedError,
    LogsUnavailableError,
    MediaNotRegisteredError,
)


def test_read_logs_and_filter(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "boot.log").write_text("boot ok\nerror X\n")
    (logs_dir / "app.log").write_text("app started\n")

    viewer = LogViewer()
    lines = viewer.read_logs(str(logs_dir))
    assert "boot ok" in lines
    assert "app started" in lines

    filtered = viewer.read_logs(str(logs_dir), filter_substring="error")
    assert filtered == ["error X"]


def test_read_logs_missing_partition_raises(tmp_path):
    viewer = LogViewer()
    with pytest.raises(LogsUnavailableError):
        viewer.read_logs(str(tmp_path / "missing"))


def test_read_logs_no_log_files_raises_corrupted(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    viewer = LogViewer()
    with pytest.raises(LogsCorruptedError):
        viewer.read_logs(str(empty_dir))


def test_exchange_write_list_read_delete_round_trip(tmp_path):
    exchange_dir = tmp_path / "exchange"
    exchange_dir.mkdir()
    source = tmp_path / "payload.txt"
    source.write_text("hello world")

    manager = ExchangeManager()
    manager.write_file(str(exchange_dir), source)

    files = manager.list_files(str(exchange_dir))
    assert [f.name for f in files] == ["payload.txt"]

    destination = tmp_path / "out.txt"
    manager.read_file(str(exchange_dir), "payload.txt", destination)
    assert destination.read_text() == "hello world"

    manager.delete_file(str(exchange_dir), "payload.txt")
    assert manager.list_files(str(exchange_dir)) == []


def test_exchange_read_missing_file_raises(tmp_path):
    exchange_dir = tmp_path / "exchange"
    exchange_dir.mkdir()
    manager = ExchangeManager()
    with pytest.raises(ExchangeError):
        manager.read_file(str(exchange_dir), "nope.txt", tmp_path / "out.txt")


def test_exchange_write_rejects_when_over_free_space(tmp_path, monkeypatch):
    exchange_dir = tmp_path / "exchange"
    exchange_dir.mkdir()
    source = tmp_path / "payload.bin"
    source.write_bytes(b"0" * 1024)

    class FakeUsage:
        free = 100

    monkeypatch.setattr("shutil.disk_usage", lambda _path: FakeUsage())

    manager = ExchangeManager()
    with pytest.raises(InsufficientSpaceError):
        manager.write_file(str(exchange_dir), source)


def test_find_media_by_boot_metadata(tmp_path):
    boot_mount = tmp_path / "boot"
    (boot_mount / "poa").mkdir(parents=True)
    (boot_mount / "poa" / "media_id.txt").write_text("deadbeef\n")

    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")

    assert read_media_id(str(boot_mount)) == "deadbeef"

    with pytest.raises(MediaNotRegisteredError):
        find_media_by_boot_metadata(container, str(boot_mount))

    container.add_media_record(
        MediaRecord(media_id="deadbeef", luks_uuid="u", label="l", config_name="c", target_os="debian")
    )
    record = find_media_by_boot_metadata(container, str(boot_mount))
    assert record.media_id == "deadbeef"


def test_find_media_missing_metadata_file(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    with pytest.raises(MediaNotRegisteredError):
        find_media_by_boot_metadata(container, str(tmp_path / "not-a-poa-medium"))
