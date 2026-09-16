"""Включён ли отзыв и закрыты ли приложения шлюзом.

Два признака, по которым решают все остальные модули: пишутся ли вообще файлы
отзыва на носитель и попадает ли в них шлюз сеанса. Держатся отдельно от
генераторов, потому что спрашивают их и снаружи -- установщик и модуль киоска.
"""
from __future__ import annotations

from ...config.schema import InstallConfig


def is_enabled(config: InstallConfig) -> bool:
    settings = getattr(config, "revocation", None)
    return bool(settings and settings.enabled and settings.url.strip())


def gate_enabled(config: InstallConfig) -> bool:
    """Закрывать ли приложения шлюзом.

    Только в режиме киоска: в консольном режиме запускать через `.desktop`
    нечего, а закрывать консоль значило бы оставить носитель без входа.
    """
    from .. import kiosk

    return is_enabled(config) and kiosk.is_enabled(config)
