"""Прослойка между экранами и сервером доступа v3 (Э7).

Как `revocation_publish` для отзыва: экранам не нужно знать ни про ключ
полномочий, ни про номера команд. Сервер доступа -- тот же сервер, с которым
контейнер познакомился для отзыва.
"""
from __future__ import annotations

from . import access_client


def is_connected(container) -> bool:
    return bool(container.data.revocation_server_url.strip())


def client(container) -> access_client.AccessClient:
    return access_client.AccessClient(container.data.revocation_server_url,
                                      container.admin_authority_secret)


def issue_activation(container, media_id: str) -> str:
    """Новый код активации. Показывается администратору один раз."""
    code = client(container).issue_activation(media_id)
    container.record_event("access_activation", media_id, "выдан код активации")
    return code


def set_targets(container, media_id: str, targets: list[dict]) -> int:
    count = client(container).set_targets(media_id, targets)
    container.record_event("access_targets", media_id, f"рабочих мест: {count}")
    return count


def get_targets(container, media_id: str) -> list[dict]:
    return client(container).get_targets(media_id)


def unblock(container, media_id: str) -> None:
    client(container).unblock(media_id)
    container.record_event("access_unblock", media_id, "носитель разблокирован на сервере")


def media_events(container, media_id: str) -> list[str]:
    """Строки журнала сервера об этом носителе, новые сверху."""
    log = client(container).log()
    lines = [f"{e.get('t') or e['received']}: {e['text']}" for e in log.get("device", [])
             if e.get("media_id") == media_id]
    lines += [f"{e['t']}: {media_id}: {e['kind']}" + (f" {e['detail']}" if e.get("detail") else "")
              for e in log.get("server", []) if e.get("media_id") == media_id]
    return sorted(lines, reverse=True)
