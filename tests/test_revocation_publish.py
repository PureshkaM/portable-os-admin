"""Что уходит на ресурс отзыва при выпуске и отзыве носителя."""
from __future__ import annotations

import pytest

from portable_os_admin.admin import revocation_client, revocation_publish
from portable_os_admin.container.models import AdminContainerData, MediaRecord
from portable_os_admin.errors import MediaNotRegisteredError


class _Container:
    """Контейнер в объёме, который нужен публикации."""

    def __init__(self, url: str = "https://revoke.example.org") -> None:
        self.data = AdminContainerData(revocation_server_url=url)
        self.admin_authority_secret = b"\x05" * 32
        self.events = []

    def record_event(self, action, media_id="", details=""):
        self.events.append((action, media_id, details))

    def find_media(self, media_id: str):
        for record in self.data.media_records:
            if record.media_id == media_id:
                return record
        return None


def _record(media_id: str = "cafe0001", **kwargs) -> MediaRecord:
    return MediaRecord(
        media_id=media_id, luks_uuid="u", label="l", config_name="c", target_os="redos",
        **kwargs,
    )


@pytest.fixture
def sent(monkeypatch):
    """Перехватывает отправку: сеть здесь не нужна."""
    calls = []

    def fake(url, seed, media_id, state, counter, **kwargs):
        calls.append({"url": url, "media_id": media_id, "state": state, "counter": counter,
                      "key": kwargs.get("device_public_key_b64")})
        return f"принято: {state}"

    monkeypatch.setattr(revocation_client, "publish_state", fake)
    return calls


def test_the_device_key_of_the_record_is_sent(sent):
    """Без ключа носителя сервер новую запись не заводит."""
    container = _Container()
    container.data.media_records.append(_record(device_public_key_b64="S0VZ"))

    revocation_publish.publish_record(container, "cafe0001")

    assert sent[0]["key"] == "S0VZ"


def test_nothing_is_asked_until_the_resource_is_connected(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("спрашивать нельзя")

    monkeypatch.setattr(revocation_client, "query_media", refuse)
    assert revocation_publish.query_record(_Container(url=""), "cafe0001") is None


def test_a_query_changes_nothing_in_the_container(monkeypatch):
    monkeypatch.setattr(revocation_client, "query_media",
                        lambda url, seed, media_id, **kw: revocation_client.MediaStatus("ok"))
    container = _Container()
    record = _record()
    container.data.media_records.append(record)

    assert revocation_publish.query_record(container, "cafe0001").state == "ok"
    assert record.revocation_counter == 0
    assert container.events == []


def test_nothing_is_sent_until_the_resource_is_connected(sent):
    """Организация, не подключавшая отзыв, не должна видеть ошибок соединения
    после каждого действия."""
    container = _Container(url="")
    container.data.media_records.append(_record())

    assert revocation_publish.publish_record(container, "cafe0001") == ""
    assert sent == []


def test_a_fresh_medium_is_announced_as_working(sent):
    container = _Container()
    container.data.media_records.append(_record())

    revocation_publish.publish_record(container, "cafe0001")

    assert sent == [{
        "url": "https://revoke.example.org",
        "media_id": "cafe0001",
        "state": "ok",
        "counter": 1,
        "key": "",
    }]


def test_a_revoked_medium_is_announced_as_revoked(sent):
    container = _Container()
    container.data.media_records.append(_record(status="revoked", revoked_at="2026-09-13"))

    revocation_publish.publish_record(container, "cafe0001")

    assert sent[0]["state"] == "revoked"


def test_every_command_about_a_medium_gets_a_higher_number(sent):
    """Номер строго возрастает: без этого однажды подписанную команду «носитель
    в порядке» можно было бы прислать повторно и оживить отозванный."""
    container = _Container()
    record = _record()
    container.data.media_records.append(record)

    revocation_publish.publish_record(container, "cafe0001")
    record.status = "revoked"
    revocation_publish.publish_record(container, "cafe0001")

    assert [call["counter"] for call in sent] == [1, 2]
    assert record.revocation_counter == 2


def test_the_number_is_spent_even_when_the_answer_never_came(monkeypatch):
    """Ответ теряется уже после того, как сервер команду принял. Оставь мы
    номер прежним, повтор получил бы «номер устарел», и носитель застрял бы в
    несогласованном состоянии навсегда."""

    def refuse(*args, **kwargs):
        raise revocation_client.RevocationServerError("ресурс отзыва недоступен")

    monkeypatch.setattr(revocation_client, "publish_state", refuse)
    container = _Container()
    record = _record()
    container.data.media_records.append(record)

    with pytest.raises(revocation_client.RevocationServerError):
        revocation_publish.publish_record(container, "cafe0001")

    assert record.revocation_counter == 1


def test_an_unknown_medium_is_refused(sent):
    with pytest.raises(MediaNotRegisteredError):
        revocation_publish.publish_record(_Container(), "cafe0001")
    assert sent == []


def test_the_journal_remembers_whether_the_resource_learned_of_it(sent):
    """«Отозван» и «ресурс об этом знает» -- разные факты, и спросят через
    полгода именно второй."""
    from portable_os_admin.container.models import ACTION_PUBLISHED

    container = _Container()
    container.data.media_records.append(_record(status="revoked", revoked_at="2026-09-13"))

    revocation_publish.publish_record(container, "cafe0001")

    assert container.events == [(ACTION_PUBLISHED, "cafe0001", "revoked")]


def test_a_failed_delivery_is_written_down_too(monkeypatch):
    from portable_os_admin.container.models import ACTION_PUBLISH_FAILED

    def refuse(*args, **kwargs):
        raise revocation_client.RevocationServerError("ресурс отзыва недоступен")

    monkeypatch.setattr(revocation_client, "publish_state", refuse)
    container = _Container()
    container.data.media_records.append(_record())

    with pytest.raises(revocation_client.RevocationServerError):
        revocation_publish.publish_record(container, "cafe0001")

    assert container.events[0][0] == ACTION_PUBLISH_FAILED
    assert "недоступен" in container.events[0][2]
