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
        calls.append({"url": url, "media_id": media_id, "state": state, "counter": counter})
        return f"принято: {state}"

    monkeypatch.setattr(revocation_client, "publish_state", fake)
    return calls


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
