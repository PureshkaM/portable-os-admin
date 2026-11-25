"""Подписанный опрос состояния персонального носителя и шлюз приложений.

Новый образ проверяет подпись сервера, идентификатор и одноразовый запрос.
После подтверждённого отзыва служба закрывает рабочие соединения и записывает
блокировку в постоянное состояние. Ключи и разделы сохраняются.
"""
from __future__ import annotations

#: Имя протекало наружу и из прежнего модуля -- он импортировал его для
#: подсказок типов. Никто снаружи им не пользуется, но набор имён фасада должен
#: совпадать с прежним: рефакторинг размещения не место для тихого сужения.
from ...config.schema import InstallConfig  # noqa: F401
from .files import config_files, new_device_key
from .gate import gate_script
from .paths import (
    AUTHORIZED_FILE,
    DEVICE_KEY_PATH,
    DISPATCHER_PATH,
    GATE_PATH,
    LOG_PATH,
    NOTIFY_APP,
    POLL_INTERVAL,
    PUBLIC_KEY_PATH,
    SCRIPT_PATH,
    SERVICE_NAME,
    SERVICE_PATH,
    STATE_DIR,
    TIMER_NAME,
    TIMER_PATH,
)
from .policy import gate_enabled, is_enabled
from .script import revocation_script
from .units import dispatcher_script, service_unit, timer_unit

__all__ = [
    "AUTHORIZED_FILE",
    "DEVICE_KEY_PATH",
    "DISPATCHER_PATH",
    "GATE_PATH",
    "InstallConfig",
    "LOG_PATH",
    "NOTIFY_APP",
    "POLL_INTERVAL",
    "PUBLIC_KEY_PATH",
    "SCRIPT_PATH",
    "SERVICE_NAME",
    "SERVICE_PATH",
    "STATE_DIR",
    "TIMER_NAME",
    "TIMER_PATH",
    "config_files",
    "dispatcher_script",
    "gate_enabled",
    "gate_script",
    "is_enabled",
    "new_device_key",
    "revocation_script",
    "service_unit",
    "timer_unit",
]
