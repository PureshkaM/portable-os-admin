"""Раздел обмена принимает каталоги: настройки программ -- это дерево."""
from __future__ import annotations

import pathlib

import pytest

from portable_os_admin.admin import ExchangeManager
from portable_os_admin.errors import ExchangeError, InsufficientSpaceError


@pytest.fixture
def exchange(tmp_path):
    mount = tmp_path / "exchange"
    mount.mkdir()
    return ExchangeManager(), str(mount), tmp_path


def test_a_directory_of_settings_goes_across_whole(exchange):
    """Через обмен едут настройки доустановленных программ.

    Это чаще дерево, чем один файл, и заставлять администратора выбирать его
    по файлу значит предлагать не пользоваться этим вовсе.
    """
    manager, mount, tmp_path = exchange
    source = tmp_path / "myapp"
    (source / "profiles").mkdir(parents=True)
    (source / "settings.ini").write_text("[main]\nlang=ru\n", encoding="utf-8")
    (source / "profiles" / "default.conf").write_text("theme=dark\n", encoding="utf-8")

    manager.write_file(mount, source)

    copied = pathlib.Path(mount) / "myapp"
    assert (copied / "settings.ini").read_text(encoding="utf-8").startswith("[main]")
    assert (copied / "profiles" / "default.conf").exists()


def test_a_directory_can_be_written_again_after_editing(exchange):
    """Настройки правят и кладут заново -- это обычный ход работы.

    Отказ на повторной записи заставил бы сначала удалять старое.
    """
    manager, mount, tmp_path = exchange
    source = tmp_path / "myapp"
    source.mkdir()
    (source / "settings.ini").write_text("lang=ru\n", encoding="utf-8")
    manager.write_file(mount, source)

    (source / "settings.ini").write_text("lang=en\n", encoding="utf-8")
    manager.write_file(mount, source)

    assert (pathlib.Path(mount) / "myapp" / "settings.ini").read_text(
        encoding="utf-8"
    ) == "lang=en\n"


def test_a_directory_shows_up_in_the_listing_with_its_size(exchange):
    """Каталог, не показанный вовсе, выглядел бы как пропавшая работа."""
    manager, mount, tmp_path = exchange
    source = tmp_path / "myapp"
    source.mkdir()
    (source / "big.bin").write_bytes(b"x" * 2048)
    manager.write_file(mount, source)

    listing = {info.name: info.size_bytes for info in manager.list_files(mount)}

    assert "myapp/" in listing, "каталог виден, и видно, что это каталог"
    assert listing["myapp/"] >= 2048


def test_a_directory_can_be_taken_back_and_removed(exchange):
    manager, mount, source_root = exchange
    source = source_root / "myapp"
    source.mkdir()
    (source / "settings.ini").write_text("lang=ru\n", encoding="utf-8")
    manager.write_file(mount, source)

    destination = source_root / "taken"
    manager.read_file(mount, "myapp/", destination)
    assert (destination / "settings.ini").exists()

    manager.delete_file(mount, "myapp/")
    assert not (pathlib.Path(mount) / "myapp").exists()


def test_free_space_is_answered_before_the_copy_not_after(exchange):
    """Раздел обмена невелик, а кладут в него то, что уже выбрали."""
    manager, mount, _tmp = exchange
    free, total = manager.free_space(mount)
    assert free > 0 and total >= free


def test_a_directory_too_big_for_the_volume_is_refused_by_size(exchange, monkeypatch):
    """Размер каталога считается по содержимому, а не по записи о нём."""
    manager, mount, tmp_path = exchange
    source = tmp_path / "huge"
    source.mkdir()
    (source / "payload.bin").write_bytes(b"x" * 4096)

    import shutil as shutil_module

    monkeypatch.setattr(
        shutil_module,
        "disk_usage",
        lambda _path: type("Usage", (), {"free": 1024, "total": 4096, "used": 3072})(),
    )
    with pytest.raises(InsufficientSpaceError):
        manager.write_file(mount, source)


def test_writing_something_that_is_not_there_says_so(exchange):
    manager, mount, tmp_path = exchange
    with pytest.raises(ExchangeError):
        manager.write_file(mount, tmp_path / "нет-такого")
