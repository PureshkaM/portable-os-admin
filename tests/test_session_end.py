"""Конец сеанса: что затирается и что после этого невозможно.

Проверяется без окна -- затирание живёт в контейнере, а не в интерфейсе.
Выход из процесса и вопрос при идущей записи проверяются в наборе интерфейса,
там, где есть настоящее приложение.

Почему это вообще важно. Ключ контейнера защищает файл, а секрет
администратора -- все выданные носители: из него выводятся коды
восстановления. Сеанс, затирающий только ключ, кончается наполовину.
"""
from __future__ import annotations

import pytest

from portable_os_admin.container import ContainerManager
from portable_os_admin.session import AdminSession


@pytest.fixture
def container(tmp_path):
    manager = ContainerManager()
    made = manager.create(tmp_path / "admin.container", "CorrectHorse123")
    yield made


def test_scrubbing_wipes_the_key_in_place(container):
    """Ключ лежит в bytearray и затирается по-настоящему."""
    assert container.key, "у нового контейнера нет ключа"
    container.scrub()
    assert container.closed
    with pytest.raises(RuntimeError):
        container.key


def test_scrubbing_drops_every_secret_in_the_data(container):
    """Секретов в данных четыре вида, и все они уходят.

    Секрет администратора -- коды восстановления всех носителей. Семя ключа
    отзыва -- подпись команд серверу. Семя обмена -- переписка с другими
    администраторами. Ключ служебных томов у записи носителя открывает журналы
    и раздел обмена **без** пароля пользователя.
    """
    data = container.data
    assert container.admin_secret, "секрет администратора не заведён"
    assert container.admin_authority_secret
    data.exchange_seed_b64 = "c2VlZA=="

    container.scrub()

    assert data.admin_secret_b64 == ""
    assert data.admin_authority_secret_b64 == ""
    assert data.exchange_seed_b64 == ""


def test_scrubbing_clears_the_records(container, tmp_path):
    """Записи о носителях тоже уходят: в них ключи служебных томов."""
    from portable_os_admin.container import MediaRecord

    container.add_media_record(
        MediaRecord(
            media_id="aaaabbbb-11112222",
            luks_uuid="523e0993-4dac-4eee-9f72-a272ce456d34",
            label="Kingston DT",
            config_name="kiosk",
            target_os="debian",
            service_key_b64="c2VjcmV0LWtleQ==",
        )
    )
    record = container.data.media_records[0]

    container.scrub()

    assert record.service_key_b64 == "", "ключ служебных томов остался в записи"
    assert container.data.media_records == []
    assert container.data.recovery_records == []
    assert container.data.configs == []


def test_a_scrubbed_container_cannot_be_saved_over(container):
    """Пустота не должна лечь поверх работы администратора.

    Затирание оставляет данные пустыми, и сохранение после него означало бы
    потерю всего, что администратор вёл. Защита не в дисциплине вызова, а в
    том, что у закрытого контейнера нет ключа: записать его нечем.
    """
    container.scrub()
    with pytest.raises(RuntimeError):
        ContainerManager().save(container)


def test_closing_a_session_scrubs_and_not_merely_closes(container):
    """Сеанс заканчивается затиранием, а не закрытием ключа."""
    session = AdminSession(container)
    session.close()

    assert session.is_open is False
    assert container.closed
    assert container.data.admin_secret_b64 == "", "секрет пережил конец сеанса"


def test_closing_a_session_twice_is_harmless(container):
    """Второй вызов не должен падать: путей выхода несколько, и они могут
    сойтись -- крестик во время обработки сигнала, например."""
    session = AdminSession(container)
    session.close()
    session.close()
    assert container.closed
