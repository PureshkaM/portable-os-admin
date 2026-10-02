"""Корни организации и сертификаты администраторов (LIVE-2.0, Э18).

Схема согласована 02.10.2026 (`docs/LIVE-2.0.md`, «Корень организации»):

* **два корня ГОСТ Р 34.10-2012**: основной -- на токене (Рутокен ЭЦП 3.0,
  ключ рождается и живёт в чипе), резервный -- здесь, в файле, зашифрованном
  сгенерированной фразой. Каждый корень выдаёт и отзывает сертификаты
  администраторов; заменить корень можно только подписью другого;
* **сертификат администратора** -- на рабочий ключ контейнера (Ed25519, ключ
  подписи выпусков), на год. Администратор приносит запрос, владелец корня
  сверяет отпечаток и подписывает;
* **якоря** (открытые части обоих корней) хранит контейнер -- оттуда их
  получает каждый записываемый носитель.

Ключ резервного корня -- из системного генератора: повторить его можно только
перебором. Фраза -- тоже сгенерированная, 33 знака из 56 (≈192 бита). Файл
шифруется AES-256-GCM ключом из scrypt N=2^20 (1 ГиБ памяти на каждую пробу):
симметричная часть стойка и к квантовому перебору (Гровер оставляет ~128 бит).
scrypt, а не Argon2id: Argon2id в `cryptography` с версии 44, а в Debian 13 --
43, файл же должен открываться годами и на любой машине.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from .. import crypto
from ..errors import PortableOSAdminError
from ..install import distribution
from ..install.access.runtime import poa_gost, poa_trust

FILE_FORMAT = "poa-org-root/2"
REQUEST_KIND = "poa-admin-request"
#: Сертификат администратора -- на год (решение 02.10.2026).
CERT_LIFETIME = 365 * 24 * 3600
#: 33 знака из 56 -- ≈192 бита.
RESERVE_PHRASE_LENGTH = 33
RESERVE_KDF_N = 2 ** 20


class OrgRootError(PortableOSAdminError):
    pass


def fingerprint(entry: dict) -> str:
    """Отпечаток корня для сверки глазами и на бумаге: 4 группы по 4 знака."""
    kid = poa_trust.key_id(entry)
    return " ".join(kid[i:i + 4] for i in range(0, 16, 4))


def format_phrase(phrase: str) -> str:
    """Фраза группами по 5 -- чтобы её можно было переписать без ошибок."""
    return "-".join(phrase[i:i + 5] for i in range(0, len(phrase), 5))


def _kdf(salt: bytes) -> crypto.KdfParams:
    return crypto.KdfParams(algo="scrypt", salt=salt, n=RESERVE_KDF_N, r=8, p=1, length=32)


# -- резервный корень: файл -------------------------------------------------------
def create_reserve_root(path: Path) -> tuple[str, dict]:
    """Новый резервный корень. Возвращает (фразу, якорь). Фразу показать один раз."""
    if path.exists():
        raise OrgRootError(f"файл {path} уже существует -- корень не перезаписывается")
    d = poa_gost.new_private_key(poa_gost.TC26_256_A)
    signer = poa_trust.GostSigner(d)
    phrase = crypto.generate_passphrase(RESERVE_PHRASE_LENGTH)
    params = _kdf(crypto.new_salt(32))
    aad = FILE_FORMAT.encode() + poa_trust.canonical(signer.entry)
    nonce, ciphertext = crypto.encrypt(crypto.derive_key(phrase, params), d.to_bytes(32, "big"), aad)
    document = {"format": FILE_FORMAT, "entry": signer.entry, "kdf": params.to_dict(),
                "nonce": base64.b64encode(nonce).decode(), "key": base64.b64encode(ciphertext).decode()}
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return phrase, signer.entry


def read_reserve_entry(path: Path) -> dict:
    """Якорь из файла без фразы -- открытая часть хранится открыто."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format") != FILE_FORMAT:
        raise OrgRootError("это не файл корня организации")
    return document["entry"]


def open_reserve_root(path: Path, phrase: str) -> poa_trust.GostSigner:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format") != FILE_FORMAT:
        raise OrgRootError("это не файл корня организации")
    phrase = "".join(phrase.split()).replace("-", "")
    params = crypto.KdfParams.from_dict(document["kdf"])
    aad = FILE_FORMAT.encode() + poa_trust.canonical(document["entry"])
    try:
        raw = crypto.decrypt(crypto.derive_key(phrase, params), base64.b64decode(document["nonce"]),
                             base64.b64decode(document["key"]), aad)
    except ValueError as exc:
        raise OrgRootError("фраза корня не подошла") from exc
    signer = poa_trust.GostSigner(int.from_bytes(raw, "big"))
    if signer.entry != document["entry"]:
        raise OrgRootError("файл корня повреждён: ключ не соответствует якорю")
    return signer


# -- якоря в контейнере --------------------------------------------------------------
def anchors(container) -> list[dict]:
    raw = getattr(container.data, "org_roots_json", "")
    return json.loads(raw) if raw else []


def set_anchors(container, entries: list[dict]) -> None:
    if not 1 <= len(entries) <= 2:
        raise OrgRootError("корней организации -- один или два")
    container.data.org_roots_json = json.dumps(entries)


# -- сертификаты администраторов -------------------------------------------------
def working_key(container):
    return distribution.signing_key(container.admin_secret)


def admin_request(container, name: str) -> dict:
    """Запрос сертификата: открытый рабочий ключ и имя. Секретов не содержит."""
    return {"kind": REQUEST_KIND, "key": poa_trust.b64(poa_trust.raw_public(working_key(container))),
            "name": name}


def certify_request(root, request: dict, serial: int | None = None) -> dict:
    """Владелец корня подписывает запрос. Отпечаток ключа из запроса сверяется
    с администратором голосом или лично -- это ответственность владельца."""
    if request.get("kind") != REQUEST_KIND:
        raise OrgRootError("это не запрос сертификата администратора")
    return poa_trust.certify(root, base64.b64decode(request["key"]), int(time.time()) + CERT_LIFETIME,
                             serial or int(time.time()), request.get("name", ""))


def install_certificate(container, certificate: dict) -> None:
    """Сертификат ложится в контейнер, только если выдан корнем организации и
    именно на рабочий ключ этого контейнера."""
    key = poa_trust.accepted_key(anchors(container), certificate)
    if key != poa_trust.b64(poa_trust.raw_public(working_key(container))):
        raise OrgRootError("сертификат выдан на другой рабочий ключ")
    container.data.working_key_cert_json = json.dumps(certificate)


def certify_container(root, container, name: str = "") -> dict:
    """Запрос и подпись за один шаг -- когда корень и контейнер на одной машине."""
    certificate = certify_request(root, admin_request(container, name))
    install_certificate(container, certificate)
    return certificate


def descriptor(container, endpoints: list[str], tls_spki: list[str], server_sign_keys: list[str],
               fallback: list[str] = (), revoked_server_keys: list[str] = ()) -> dict:
    """Новый подписанный описатель сервера; номер строго растёт."""
    if not container.data.working_key_cert_json:
        raise OrgRootError("у рабочего ключа контейнера нет сертификата корня организации")
    serial = max(container.data.descriptor_serial + 1, int(time.time()))
    payload = {"format": 1, "serial": serial, "endpoints": list(endpoints),
               "tls_spki": list(tls_spki), "server_sign_keys": list(server_sign_keys),
               "revoked_server_keys": list(revoked_server_keys), "fallback": list(fallback),
               "issued_at": int(time.time())}
    document = poa_trust.sign_document(working_key(container),
                                       json.loads(container.data.working_key_cert_json),
                                       "descriptor", payload)
    # Отзыв и замены корня -- рядом с описателем, а не внутри: их подлинность
    # подтверждает подпись корня, а не рабочего ключа.
    if container.data.revoked_keys_json:
        document["revoked"] = json.loads(container.data.revoked_keys_json)
    if container.data.root_updates_json:
        document["root_updates"] = json.loads(container.data.root_updates_json)
    container.data.descriptor_serial = serial
    return document


# -- отзыв и замена корня ----------------------------------------------------------
def revoke_working_keys(root, container, keys_b64: list[str]) -> dict:
    """Новый список отзыва: прежние отозванные плюс `keys_b64`. Номер растёт."""
    previous = json.loads(container.data.revoked_keys_json) if container.data.revoked_keys_json else None
    keys = set(previous["list"]["keys"]) if previous else set()
    keys.update(keys_b64)
    serial = (previous["list"]["serial"] + 1) if previous else 1
    document = poa_trust.revocation_list(root, [base64.b64decode(k) for k in keys], serial)
    container.data.revoked_keys_json = json.dumps(document)
    return document


def replace_main_root(reserve_root, container, new_main_entry: dict) -> None:
    """Резервный корень заменяет основной (утерян, украден, сломан токен)."""
    current = anchors(container)
    old = next((a for a in current if a.get("role") == poa_trust.ROLE_MAIN), None)
    if old is None:
        raise OrgRootError("основного корня среди якорей нет")
    updates = json.loads(container.data.root_updates_json) if container.data.root_updates_json else []
    document = poa_trust.root_replacement(reserve_root, old, new_main_entry, len(updates) + 1)
    set_anchors(container, poa_trust.apply_root_replacement(current, document))
    container.data.root_updates_json = json.dumps(updates + [document])
