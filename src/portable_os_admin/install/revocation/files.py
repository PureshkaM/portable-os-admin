"""Что из всего этого попадает на носитель и с каким ключом.

Единственное место, где генераторы сходятся вместе: здесь решают, писать ли
файлы вообще, и здесь же рождается ключ носителя. Дальше набор уходит в
бэкенды нетронутым.
"""
from __future__ import annotations

from ...config.schema import InstallConfig
from .gate import gate_script
from .paths import (
    DISPATCHER_PATH,
    GATE_PATH,
    SCRIPT_PATH,
    SERVICE_PATH,
    TIMER_PATH,
)
from .policy import gate_enabled, is_enabled
from .script import revocation_script
from .units import dispatcher_script, service_unit, timer_unit


def config_files(
    config: InstallConfig,
    media_id: str = "",
    luks_uuid: str = "",
    logs_luks_uuid: str = "",
    exchange_luks_uuid: str = "",
    esp_uuid: str = "",
    boot_uuid: str = "",
) -> dict[str, tuple[str, int | None]]:
    """Файлы носителя. Пусто, пока отзыв не включён или носитель неизвестен.

    `media_id` и UUID приходят от установщика: они разные у каждого носителя,
    поэтому эти файлы пишутся при записи, а не попадают в образ дистрибутива --
    иначе все носители спрашивали бы сервер об одном и том же идентификаторе.
    Шлюз одинаков у всех, но пишется вместе с остальным: без службы, которая
    открывает его, он лишь запер бы приложения навсегда.
    """
    if not is_enabled(config) or not media_id or not luks_uuid:
        return {}
    script = revocation_script(
        config,
        media_id,
        luks_uuid,
        logs_luks_uuid=logs_luks_uuid,
        exchange_luks_uuid=exchange_luks_uuid,
        esp_uuid=esp_uuid,
        boot_uuid=boot_uuid,
    )
    files: dict[str, tuple[str, int | None]] = {
        SCRIPT_PATH: (script, 0o755),
        SERVICE_PATH: (service_unit(), 0o644),
        TIMER_PATH: (timer_unit(), 0o644),
        DISPATCHER_PATH: (dispatcher_script(), 0o755),
    }
    if gate_enabled(config):
        files[GATE_PATH] = (gate_script(), 0o755)
    return files


def new_device_key() -> tuple[str, str]:
    """Ключ носителя: (закрытая часть в PEM, открытая часть -- base64 сырых байт).

    Открытая часть -- в том виде, в каком её ждёт команда серверу
    (`poa-media <id> <номер> <состояние> <ключ>`): 44 знака, без переводов строк.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_pem, base64.b64encode(public_raw).decode("ascii")


# Службу здесь не включают. Это делает установщик ссылкой в
# `timers.target.wants`, рядом с записью самих файлов -- см.
# `OSInstaller._write_revocation_key`.
#
# Так вышло не сразу. Сперва включение жило в командах chroot и шло
# безусловно: сборка дистрибутива падала на «unit poa-revocation.service does
# not exist», потому что файлов там нет по замыслу. Признак исправили на
# «известен носитель» -- и служба перестала включаться на пути развёртывания
# готового дистрибутива, где команды chroot не выполняются вовсе: носитель
# РЕД ОС 14.09.2026 уехал со скриптом, ключом и без автозапуска.
