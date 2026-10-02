"""Клиент сервера доступа на самом носителе (протокол v3).

Живёт в образе отдельным файлом (`/usr/lib/poa-access/poa_client.py`): пакета
программы администратора на носителе нет, поэтому модуль самодостаточен --
стандартная библиотека и `python3-cryptography`. Им пользуется только
`poa-accessd` (root): ключ носителя и звено читает только он.

Договор -- `docs/ACCESS-PROTOCOL.md`. Здесь три обязанности:

* **транспорт** -- HTTPS, сервер опознаётся по отпечатку SPKI из описателя, а
  не цепочкой УЦ; несовпадение обрывает соединение до отправки чего-либо;
* **протокол** -- nonce, подпись запроса, активация, вход с подтверждением
  звена, журнал;
* **звено** -- пишется атомарно (tmp, fsync, rename, fsync каталога) до
  подтверждения серверу: иначе сбой питания между ответом и записью превратил
  бы носитель в «клона» самого себя.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import urllib.parse
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

TIMEOUT = 30


class AccessError(Exception):
    """Отказ сервера или транспорта: `code` -- HTTP-код или 0, `reason` -- поле error."""

    def __init__(self, code: int, reason: str, body: dict | None = None) -> None:
        super().__init__(f"{code} {reason}")
        self.code, self.reason, self.body = code, reason, body or {}


class PinMismatch(AccessError):
    """Отпечаток TLS-ключа сервера не тот: подозрение на «человека посередине»."""


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def spki_sha256(der_cert: bytes) -> str:
    from cryptography import x509

    cert = x509.load_der_x509_certificate(der_cert)
    spki = cert.public_key().public_bytes(serialization.Encoding.DER,
                                          serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(spki).hexdigest()


# ---------------------------------------------------------------------------
# транспорт
# ---------------------------------------------------------------------------
@dataclass
class Endpoint:
    url: str                       # https://access.example.org
    pins: tuple[str, ...] = ()     # sha256 SPKI; пусто допустимо только для http://127.0.0.1 в тестах

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        parts = urllib.parse.urlsplit(self.url)
        host, port = parts.hostname, parts.port
        if parts.scheme == "https":
            context = ssl.create_default_context()
            # УЦ не участвуют: подлинность сервера -- отпечаток из описателя.
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            conn = http.client.HTTPSConnection(host, port or 443, timeout=TIMEOUT, context=context)
            conn.connect()
            der = conn.sock.getpeercert(binary_form=True)
            if not der or spki_sha256(der) not in self.pins:
                conn.close()
                raise PinMismatch(0, "tls_pin")
        elif parts.scheme == "http" and host in ("127.0.0.1", "localhost") and not self.pins:
            conn = http.client.HTTPConnection(host, port or 80, timeout=TIMEOUT)   # только тесты
        else:
            raise AccessError(0, "insecure_endpoint")
        try:
            conn.request(method, parts.path.rstrip("/") + path, body=body,
                         headers={"Content-Length": str(len(body)), **(headers or {})})
            response = conn.getresponse()
            data = response.read(1 << 24)
            return response.status, data
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise AccessError(0, f"network: {exc}") from exc
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# подписант и звено
# ---------------------------------------------------------------------------
class FileSigner:
    """Ключ носителя из файла (флешка). Токен -- PKCS#11, позже (`medium.DeviceSigner`)."""

    alg = "ed25519"

    def __init__(self, path: str) -> None:
        self._key = serialization.load_pem_private_key(open(path, "rb").read(), password=None)
        if not isinstance(self._key, ed25519.Ed25519PrivateKey):
            raise ValueError("ключ носителя -- не Ed25519")

    def sign(self, data: bytes) -> bytes:
        return self._key.sign(data)


def atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """tmp → fsync → rename → fsync каталога: после возврата данные на носителе."""
    directory = os.path.dirname(path) or "."
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


@dataclass
class State:
    """Постоянное состояние доступа: `/var/lib/poa/access.json` (каталог state)."""

    path: str

    def load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        atomic_write(self.path, canonical(data))


# ---------------------------------------------------------------------------
# протокол
# ---------------------------------------------------------------------------
@dataclass
class Client:
    endpoint: Endpoint
    media: str
    signer: FileSigner
    server_keys: tuple[bytes, ...]     # открытые ключи подписи сервера (сырые 32 байта)

    def _json(self, method: str, path: str, obj: dict | None = None, headers=None):
        body = canonical(obj) if obj is not None else b""
        status, data = self.endpoint.request(method, path, body,
                                             {"Content-Type": "application/json", **(headers or {})})
        try:
            parsed = json.loads(data) if data[:1] == b"{" else {}
        except ValueError:
            parsed = {}
        if status != 200:
            raise AccessError(status, parsed.get("error", "http"), parsed)
        return parsed

    def _signed_post(self, path: str, obj: dict) -> dict:
        nonce = self._json("POST", "/v3/nonce", {"media": self.media})["nonce"]
        body = canonical(obj)
        message = "\n".join(("poa-v3", f"POST {path}", self.media, nonce,
                             hashlib.sha256(body).hexdigest())).encode("utf-8")
        headers = {"Content-Type": "application/json", "X-POA-Media": self.media,
                   "X-POA-Nonce": nonce,
                   "X-POA-Signature": base64.b64encode(self.signer.sign(message)).decode()}
        status, data = self.endpoint.request("POST", path, body, headers)
        try:
            parsed = json.loads(data) if data[:1] == b"{" else {}
        except ValueError:
            parsed = {}
        if status != 200:
            raise AccessError(status, parsed.get("error", "http"), parsed)
        return parsed

    def _verified_grant(self, answer: dict) -> dict:
        grant, signature = answer.get("grant"), answer.get("signature", "")
        if not isinstance(grant, dict):
            raise AccessError(0, "bad_grant")
        raw = base64.b64decode(signature)
        for key in self.server_keys:
            try:
                ed25519.Ed25519PublicKey.from_public_bytes(key).verify(raw, canonical(grant))
                return grant
            except Exception:
                continue
        raise AccessError(0, "grant_signature")

    # -- шаги ------------------------------------------------------------------
    def activate(self, code: str, info: dict) -> dict:
        """Возвращает {"totp": {...}, "link": ...}; звено сохраняет вызывающий."""
        return self._verified_grant(self._signed_post("/v3/activate", {"code": code, "info": info}))

    def login(self, state: State, totp: str, status: dict, vpn: dict) -> dict:
        """Вход целиком: session → запись N+1 → confirm. Возвращает grant."""
        data = state.load()
        answer = self._signed_post("/v3/session", {"totp": totp, "link": data["link"],
                                                   "status": status})
        data["link"] = answer["proposal"]
        data["release"] = answer.get("release")
        state.save(data)                                   # до подтверждения -- обязательно
        return self._verified_grant(self._signed_post("/v3/session/confirm",
                                                      {"link": data["link"], "vpn": vpn}))

    def send_events(self, catalog: int, events: list[dict]) -> int:
        return self._signed_post("/v3/events", {"catalog": catalog, "events": events})["acked"]
