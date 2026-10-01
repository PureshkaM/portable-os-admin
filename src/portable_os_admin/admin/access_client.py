"""Команды серверу доступа v3 от программы администратора.

Договор -- `docs/ACCESS-PROTOCOL.md`, раздел 9. Ключ тот же, что у команд
отзыва (`revocation_client.signing_key`): ключ полномочий из семени контейнера,
закрытая часть контейнера не покидает.

Номер команды должен строго расти у каждого администратора. Берётся время в
миллисекундах: хранить счётчик в контейнере значило бы ломать команды при
работе с копии контейнера на второй машине, а часам машины администратора
доверять можно (так же считает и запрос `query_media`).

Код активации создаётся здесь, а не на сервере: на сервер уходит только его
SHA-256, сам код видит лишь администратор и передаёт пользователю отдельным
каналом. Взлом сервера не выдаёт действующих кодов.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..errors import PortableOSAdminError
from .revocation_client import key_id, signing_key

TIMEOUT_SECONDS = 30
UPLOAD_TIMEOUT_SECONDS = 3600
#: Алфавит кода активации: без похожих знаков (0/O, 1/I/L, U/V). 30 знаков.
ACTIVATION_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
ACTIVATION_LENGTH = 10
#: Срок кода активации -- решение 01.10.2026.
ACTIVATION_TTL_SECONDS = 72 * 3600


class AccessServerError(PortableOSAdminError):
    """Сервер доступа не принял команду -- с причиной, понятной человеку."""


_EXPLAIN = {
    400: "сервер не разобрал команду",
    403: "сервер не допустил этот контейнер (ключ не знаком или подпись не сходится)",
    404: "такого объекта на сервере нет",
    409: "команда устарела или противоречит состоянию сервера",
    503: "сервер доступа не настроен (нет ключей v3)",
}


def _canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def fetch_tls_spki(url: str, timeout: int = TIMEOUT_SECONDS) -> str:
    """SHA-256 SPKI TLS-сертификата сервера -- для закрепления на носителе.

    Снимается на машине администратора с **обычной** проверкой цепочки УЦ
    (сертификат сервера -- Let's Encrypt): это и есть момент доверия. Носитель
    дальше УЦ не спрашивает -- только этот отпечаток (`docs/ACCESS-PROTOCOL.md`, 2).
    """
    import socket
    import ssl
    import urllib.parse

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise AccessServerError("отпечаток TLS снимается только с https-адреса сервера")
    context = ssl.create_default_context()
    try:
        with socket.create_connection((parts.hostname, parts.port or 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=parts.hostname) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as error:
        raise AccessServerError(f"не удалось проверить TLS сервера доступа: {error}") from error
    spki = x509.load_der_x509_certificate(der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


def new_activation_code() -> tuple[str, str]:
    """(код для человека «XXXXX-XXXXX», SHA-256 для сервера)."""
    raw = "".join(secrets.choice(ACTIVATION_ALPHABET) for _ in range(ACTIVATION_LENGTH))
    shown = f"{raw[:5]}-{raw[5:]}"
    return shown, hashlib.sha256(raw.encode("ascii")).hexdigest()


@dataclass
class AccessClient:
    url: str
    seed: bytes
    opener: object = urllib.request.urlopen
    _last: int = 0

    def _counter(self) -> int:
        # Две команды в одну миллисекунду не должны получить один номер.
        self._last = max(self._last + 1, time.time_ns() // 1_000_000)
        return self._last

    def command(self, name: str, **params) -> dict:
        body = _canonical({"cmd": name, "counter": self._counter(), **params})
        request = urllib.request.Request(
            f"{self.url.rstrip('/')}/admin/v3/{name}", data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-POA-Key-Id": key_id(self.seed),
                "X-POA-Signature": base64.b64encode(signing_key(self.seed).sign(body)).decode(),
            },
        )
        return self._send(request, TIMEOUT_SECONDS)

    def _send(self, request, timeout: int) -> dict:
        try:
            with self.opener(request, timeout=timeout) as response:
                raw = response.read(1 << 22)
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read(4096) or b"{}").get("error", "")
            except (ValueError, OSError):
                pass
            reason = _EXPLAIN.get(error.code, f"код {error.code}")
            raise AccessServerError(f"{reason}{f' ({detail})' if detail else ''}") from error
        except OSError as error:
            raise AccessServerError(f"сервер доступа недоступен: {error}") from error
        return json.loads(raw) if raw else {}

    # -- носители -------------------------------------------------------------
    def issue_activation(self, media: str, ttl: int = ACTIVATION_TTL_SECONDS) -> str:
        """Новый код активации носителя. Возвращает код -- показать администратору."""
        shown, digest = new_activation_code()
        self.command("activation", media=media, code_hash=digest, expires=int(time.time()) + ttl)
        return shown

    def set_targets(self, media: str, targets: list[dict]) -> int:
        return self.command("targets", media=media, targets=targets).get("count", 0)

    def unblock(self, media: str) -> None:
        self.command("unblock", media=media)

    def set_vipnet_password(self, media: str, password: str) -> None:
        self.command("dst", media=media, password=password)

    # -- группы и выпуски ---------------------------------------------------------
    def group(self, group_id: str, parent: str = "root") -> None:
        self.command("group", id=group_id, parent=parent)

    def move_media(self, media: str, group_id: str) -> None:
        self.command("media_group", media=media, group=group_id)

    def assign(self, group_id: str, distribution: str | None) -> None:
        self.command("assign", group=group_id, distribution=distribution)

    def allow_vm(self, group_id: str, allow: bool | None) -> None:
        self.command("policy", group=group_id, allow_vm=allow)

    def distribution(self, dist: str, family: str, arch: str, layout: int,
                     min_serial: int = 0) -> None:
        self.command("distribution", id=dist, family=family, arch=arch, layout=layout,
                     min_serial=min_serial)

    def upload(self, dist: str, serial: int, path: str, name: str | None = None) -> str:
        """Потоковая выгрузка файла выпуска. Возвращает SHA-256."""
        name = name or os.path.basename(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        size = os.path.getsize(path)
        message = f"poa-upload {dist} {serial} {name} {sha} {size}".encode("ascii")
        with open(path, "rb") as body:
            request = urllib.request.Request(
                f"{self.url.rstrip('/')}/admin/v3/upload/{dist}/{serial}/{name}",
                data=body, method="PUT",
                headers={
                    "Content-Length": str(size), "X-POA-SHA256": sha,
                    "X-POA-Key-Id": key_id(self.seed),
                    "X-POA-Signature": base64.b64encode(signing_key(self.seed).sign(message)).decode(),
                },
            )
            self._send(request, UPLOAD_TIMEOUT_SECONDS)
        return sha

    def publish_release(self, dist: str, serial: int, paths: list[str]) -> None:
        """Выгружает файлы выпуска и объявляет его. Порядок важен: сервер
        принимает объявление, только если все файлы уже лежат и сходятся."""
        files = {os.path.basename(p): self.upload(dist, serial, p) for p in paths}
        self.command("release", distribution=dist, serial=serial, files=files)

    def set_vpn_remotes(self, remotes: list[dict], gateway_name: str = "") -> None:
        """Адреса шлюза и имя в его сертификате (носитель сверяет его)."""
        self.command("vpn", remotes=remotes, gateway_name=gateway_name)

    def log(self, since: int = 0, events_since: int = 0) -> dict:
        return self.command("log", since=since, events_since=events_since)
