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
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

TIMEOUT = 30


def boottime() -> float:
    """Linux считает время сна в CLOCK_BOOTTIME. Другие ОС используются в тестах."""
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class AccessError(Exception):
    """Отказ сервера или транспорта: `code` -- HTTP-код или 0, `reason` -- поле error."""

    def __init__(self, code: int, reason: str, body: dict | None = None) -> None:
        super().__init__(f"{code} {reason}")
        self.code, self.reason, self.body = code, reason, body or {}


class PinMismatch(AccessError):
    """Отпечаток TLS-ключа сервера не тот: подозрение на «человека посередине»."""


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _host(value) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253 or "%" in value:
        return False
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        label = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        return all(re.fullmatch(label, part) for part in value.removesuffix(".").split("."))


def _checked_profile(grant: dict, entry: dict) -> None:
    """Сверяет фактически полученные цель и транспорт с версией из каталога."""
    target, vpn = grant.get("target"), grant.get("vpn")
    if not isinstance(target, dict) or target.get("id") != entry["id"] or not _host(target.get("host")) \
            or type(target.get("port")) is not int or not 0 < target["port"] < 65536 \
            or target.get("type") not in ("rdp", "vnc") or not isinstance(target.get("pin"), str):
        raise AccessError(0, "bad_target")
    if target["type"] == "rdp" and not re.fullmatch(r"sha256:[a-f0-9]{64}", target["pin"]):
        raise AccessError(0, "bad_target")
    if not isinstance(vpn, dict) or vpn.get("provider") != "openvpn" or not isinstance(vpn.get("ca"), str):
        raise AccessError(0, "bad_vpn")
    remotes = vpn.get("remotes")
    if not isinstance(remotes, list) or not 1 <= len(remotes) <= 16 or any(
        not isinstance(remote, dict) or not _host(remote.get("host")) or type(remote.get("port")) is not int
        or not 0 < remote["port"] < 65536 or remote.get("proto") not in ("udp", "tcp") for remote in remotes
    ) or (vpn.get("gateway_name") and not _host(vpn["gateway_name"])):
        raise AccessError(0, "bad_vpn")
    transport = {"remotes": remotes, "gateway_name": vpn.get("gateway_name", ""),
                 "version": vpn.get("version", ""), "ca": hashlib.sha256(vpn["ca"].encode()).hexdigest(),
                 "tls_crypt": vpn.get("tls_crypt_revision")}
    snapshot = {"version": entry["version"], "target": target, "transport": transport}
    if hashlib.sha256(canonical(snapshot)).hexdigest() != entry["revision"]:
        raise AccessError(0, "assignment_changed")


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

    def _open(self):
        """Соединение с сервером, узнанным по закреплённому отпечатку TLS."""
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
        return conn, parts.path.rstrip("/")

    def request(self, method: str, path: str, body: bytes = b"", headers: dict | None = None):
        conn, base = self._open()
        try:
            conn.request(method, base + path, body=body,
                         headers={"Content-Length": str(len(body)), **(headers or {})})
            response = conn.getresponse()
            data = response.read(1 << 24)
            return response.status, data
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise AccessError(0, f"network: {exc}") from exc
        finally:
            conn.close()

    def download(self, path: str, dest: str, progress=None) -> int:
        """Файл выпуска в `dest` потоком, с докачкой (`Range`) -- образы весят
        гигабайты, а связь у ноутбука в поезде рвётся. Возвращает размер."""
        offset = os.path.getsize(dest) if os.path.exists(dest) else 0
        conn, base = self._open()
        try:
            conn.request("GET", base + path, headers={"Range": f"bytes={offset}-"} if offset else {})
            response = conn.getresponse()
            if response.status == 416 and offset:      # уже скачан целиком
                return offset
            if response.status == 200:
                offset, mode = 0, "wb"                 # сервер начал сначала
            elif response.status == 206:
                mode = "ab"
            else:
                raise AccessError(response.status, "download")
            with open(dest, mode) as handle:
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    handle.write(chunk)
                    offset += len(chunk)
                    if progress:
                        progress(offset)
            return offset
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
        try:
            raw = base64.b64decode(signature, validate=True)
        except (ValueError, TypeError):
            raise AccessError(0, "grant_signature") from None
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

    def login(self, state: State, totp: str, status: dict) -> dict:
        """Подтверждение звена после MFA открывает каталог мест."""
        data = state.load()
        answer = self._signed_post("/v3/session", {"totp": totp, "link": data["link"],
                                                   "status": status})
        data["link"] = answer["proposal"]
        data["release"] = answer.get("release")
        state.save(data)                                   # до подтверждения -- обязательно
        return self._work_request("confirm", {"link": data["link"]})

    def _work_request(self, action: str, obj: dict) -> dict:
        challenge = secrets.token_hex(16)
        sent = boottime()
        answer = self._signed_post("/v3/session/" + action, {**obj, "nonce": challenge})
        grant = self._verified_grant(answer)
        purpose = "catalog" if action == "confirm" else action
        expected = {"purpose": purpose, "nonce": challenge, "media": self.media}
        expected.update({key: obj[key] for key in ("session", "generation", "target_id", "revision")
                         if key in obj})
        if any(type(grant.get(key)) is not type(value) or grant[key] != value
               for key, value in expected.items()):
            raise AccessError(0, "grant_binding")
        if not isinstance(grant.get("session"), str) or not re.fullmatch(r"[0-9a-f]{32}", grant["session"]):
            raise AccessError(0, "grant_binding")
        entries = grant.get("targets")
        if not isinstance(entries, list) or len(entries) > 64 or any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            or not isinstance(item.get("name"), str) or type(item.get("version")) is not int
            or not isinstance(item.get("revision"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["revision"])
            for item in entries
        ) or len({item["id"] for item in entries}) != len(entries):
            raise AccessError(0, "bad_catalog")
        if action in ("select", "renew"):
            issued, expires = grant.get("server_time"), grant.get("not_after")
            if type(issued) is not int or type(expires) is not int or not 0 < expires - issued <= 300:
                raise AccessError(0, "grant_lifetime")
            deadline = sent + (expires - issued)
            if boottime() >= deadline:
                raise AccessError(401, "session_expired")
            grant["_deadline"] = deadline
        return grant

    def select(self, session: str, entry: dict, generation: int, vpn_request: dict) -> dict:
        grant = self._work_request("select", {"session": session, "generation": generation,
                                               "target_id": entry["id"], "revision": entry["revision"],
                                               "vpn": vpn_request})
        _checked_profile(grant, entry)
        return grant

    def renew(self, session: str, entry: dict, generation: int) -> dict:
        return self._work_request("renew", {"session": session, "generation": generation,
                                            "target_id": entry["id"], "revision": entry["revision"]})

    def release(self, session: str, generation: int) -> dict:
        return self._work_request("release", {"session": session, "generation": generation})

    def catalog(self, session: str) -> dict:
        return self._work_request("catalog", {"session": session})

    def send_events(self, catalog: int, events: list[dict]) -> int:
        return self._signed_post("/v3/events", {"catalog": catalog, "events": events})["acked"]
