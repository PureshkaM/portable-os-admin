"""Сообщить ресурсу отзыва о состоянии одного носителя.

Тонкая прослойка между экранами и `revocation_client`: экранам не нужно знать
ни про номера команд, ни про то, подключён ли ресурс вообще.

Главное правило здесь -- **сообщение серверу не отменяет местного решения**.
Администратор отозвал носитель; запись в контейнере уже изменена; ресурс
недоступен -- это повод сказать «сообщить не удалось, повторите», а не повод
делать вид, что отзыва не было.
"""
from __future__ import annotations

from ..container.models import ACTION_PUBLISH_FAILED, ACTION_PUBLISHED
from ..errors import MediaNotRegisteredError
from . import revocation_client

STATE_OK = "ok"
STATE_REVOKED = "revoked"


def is_connected(container) -> bool:
    """Знаком ли контейнер с ресурсом отзыва."""
    return bool(container.data.revocation_server_url.strip())


def state_of(record) -> str:
    return STATE_REVOKED if record.is_revoked else STATE_OK


def publish_record(container, media_id: str) -> str:
    """Отправляет текущее состояние носителя. Пусто, если ресурс не подключён.

    Номер команды увеличивается **до** отправки и остаётся увеличенным, даже
    если отправка не удалась. Так задумано: номер обязан строго возрастать, а
    ответ сервера может потеряться по дороге уже после того, как он команду
    принял. Оставь мы номер прежним -- повтор получил бы отказ «номер устарел»
    и носитель навсегда застрял бы в несогласованном состоянии. Пропуск в
    нумерации не стоит ничего.
    """
    if not is_connected(container):
        return ""
    record = container.find_media(media_id)
    if record is None:
        raise MediaNotRegisteredError(
            f"сведения о носителе не найдены в контейнере (media_id={media_id})"
        )

    record.revocation_counter += 1
    state = state_of(record)
    try:
        said = revocation_client.publish_state(
            container.data.revocation_server_url,
            container.admin_authority_secret,
            media_id,
            state,
            record.revocation_counter,
        )
    except revocation_client.RevocationServerError as error:
        # Неудача записывается в журнал наравне с успехом: «отозван» и «ресурс
        # об этом знает» -- разные факты, и через полгода спросят именно
        # второй. Без записи он не восстановим ничем.
        container.record_event(ACTION_PUBLISH_FAILED, media_id, f"{state}: {error}")
        raise
    container.record_event(ACTION_PUBLISHED, media_id, state)
    return said
