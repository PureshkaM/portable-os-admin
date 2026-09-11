"""Журнал работы приложения: кто и когда открывал контейнер.

Проверяется то, ради чего он заведён, и то, чего в нём быть не должно.
"""
from __future__ import annotations

from portable_os_admin.gui import app_journal


def test_a_record_names_the_time_the_user_the_action_and_the_container(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    app_journal.record(app_journal.CONTAINER_OPENED, "/root/admin.container")

    lines = app_journal.read()
    assert len(lines) == 1
    fields = lines[0].split("\t")
    assert fields[2] == app_journal.CONTAINER_OPENED
    assert fields[3] == "/root/admin.container"
    assert fields[0].startswith("20"), "нет времени события"
    assert fields[1], "не сказано, под какой учётной записью работали"


def test_the_journal_lives_beside_the_interface_settings_not_in_the_container(tmp_path, monkeypatch):
    """Не внутри контейнера -- он про носители, а не про привычки работы.

    Контейнер копируют, передают и открывают целиком; растить его записями о
    том, кто и когда его открывал, значило бы возить эти сведения вместе с ним.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    app_journal.record(app_journal.CONTAINER_CREATED, tmp_path / "admin.container")

    assert app_journal.path() == tmp_path / "portable-os-admin" / "journal.log"
    assert app_journal.path().is_file()


def test_a_record_never_carries_the_password_or_the_code(tmp_path, monkeypatch):
    """Журнал лежит открытым текстом, и всё в нём надо считать доступным.

    Поэтому в него едут ссылки на события, а не их содержимое: какой носитель и
    чей, но не код, который этот носитель открывает.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    app_journal.record(
        app_journal.RECOVERY_CODE_ISSUED,
        details="носитель 46aed677, владелец: Иванов Иван Иванович",
    )

    written = app_journal.path().read_text(encoding="utf-8")
    assert "46aed677" in written
    assert "Иванов Иван Иванович" in written
    for secret in ("CSJ0-J20E-VEC3", "CorrectHorse123"):
        assert secret not in written


def test_one_event_stays_one_line(tmp_path, monkeypatch):
    """Перевод строки в поле иначе разрывает запись надвое.

    Журнал после этого нельзя ни просмотреть глазами, ни разобрать построчно, а
    поле сюда приходит из введённого человеком.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    app_journal.record(app_journal.CONTAINER_OPENED, "/root/a.container", details="строка\nвторая")

    assert len(app_journal.read()) == 1


def test_an_overgrown_journal_loses_its_older_half_not_all_of_it(tmp_path, monkeypatch):
    """Журнал живёт годами и никем не чистится.

    Терять его целиком из-за того, что он вырос, -- худший способ им управлять:
    разбирают всегда недавнее, а пропадает при этом всё.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(app_journal, "MAX_BYTES", 300)

    for number in range(40):
        app_journal.record(app_journal.CONTAINER_OPENED, f"/root/{number}.container")

    lines = app_journal.read()
    assert lines, "журнал опустел целиком"
    assert app_journal.path().stat().st_size <= app_journal.MAX_BYTES * 2
    assert "/root/39.container" in lines[-1], "выброшено новое вместо старого"


def test_a_journal_that_cannot_be_written_does_not_stop_the_work(tmp_path, monkeypatch):
    """Работа администратора не должна вставать из-за строки в журнале.

    Обратная сторона названа прямо в модуле: журнал, который можно потерять, не
    является доказательством. Здесь проверяется, что он хотя бы не мешает.
    """
    # Каталог создать невозможно: на его месте лежит обычный файл. Это честный
    # отказ файловой системы, а не подменённая функция, -- проверяется именно
    # то, что случится на машине, где каталог настроек занят или недоступен.
    blocked = tmp_path / "blocked"
    blocked.write_text("не каталог", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(blocked))

    app_journal.record(app_journal.CONTAINER_OPENED, "/root/admin.container")
    assert app_journal.read() == [], "журнал не записался — и это не должно было ничего сломать"
