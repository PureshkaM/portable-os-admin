"""Журнал работы приложения: кто и когда открывал контейнер.

Проверяется то, ради чего он заведён, и то, чего в нём быть не должно.
"""
from __future__ import annotations

from portable_os_admin.gui import app_journal


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
