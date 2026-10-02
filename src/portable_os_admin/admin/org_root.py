"""Корневой ключ организации и рабочие ключи администраторов (LIVE-2.0, Э18).

Корневой ключ создаётся здесь один раз и **не хранится в контейнере**: он
уходит в отдельный файл, зашифрованный паролем (как сам контейнер: scrypt и
AES-256-GCM), -- файл и пароль хранятся офлайн, в сейфе. Корень нужен редко:
заверить рабочий ключ нового администратора, отозвать заверение, распечатать
сервер, когда потеряны все контейнеры.

Рабочий ключ администратора -- ключ подписи выпусков из секрета контейнера
(`distribution.signing_key`); корень заверяет именно его. Описатель сервера
подписывает рабочий ключ с приложенным заверением.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from .. import crypto
from ..errors import PortableOSAdminError
from ..install import distribution
from ..install.access.runtime import poa_trust

#: Срок заверения рабочего ключа. Два года: заверение обновляется корнем, а
#: корень достают из сейфа редко.
CERT_LIFETIME = 2 * 365 * 24 * 3600
FILE_FORMAT = "poa-org-root/1"


class OrgRootError(PortableOSAdminError):
    pass


def create_root_file(path: Path, password: str) -> str:
    """Новый корневой ключ в зашифрованном файле. Возвращает открытую часть (b64)."""
    problems = crypto.validate_password_strength(password, 16)
    if problems:
        raise OrgRootError("пароль корневого ключа: " + "; ".join(problems))
    if path.exists():
        raise OrgRootError(f"файл {path} уже существует -- корень не перезаписывается")
    key = ed25519.Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    params = crypto.calibrated_kdf_params()
    nonce, ciphertext = crypto.encrypt(crypto.derive_key(password, params), raw, FILE_FORMAT.encode())
    public = poa_trust.b64(poa_trust.raw_public(key))
    document = {"format": FILE_FORMAT, "public": public, "kdf": params.to_dict(),
                "nonce": base64.b64encode(nonce).decode(), "key": base64.b64encode(ciphertext).decode()}
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return public


def open_root_file(path: Path, password: str) -> ed25519.Ed25519PrivateKey:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("format") != FILE_FORMAT:
        raise OrgRootError("это не файл корневого ключа")
    params = crypto.KdfParams.from_dict(document["kdf"])
    try:
        raw = crypto.decrypt(crypto.derive_key(password, params),
                             base64.b64decode(document["nonce"]), base64.b64decode(document["key"]),
                             FILE_FORMAT.encode())
    except ValueError as exc:
        raise OrgRootError("пароль корневого ключа не подошёл") from exc
    return ed25519.Ed25519PrivateKey.from_private_bytes(raw)


def working_key(container):
    return distribution.signing_key(container.admin_secret)


def certify_container(root_key, container, serial: int | None = None) -> dict:
    """Корень заверяет рабочий ключ контейнера и становится корнем доверия его носителей."""
    cert = poa_trust.certify(root_key, poa_trust.raw_public(working_key(container)),
                             time.time() + CERT_LIFETIME, serial or int(time.time()))
    container.data.org_root_public_b64 = poa_trust.b64(poa_trust.raw_public(root_key))
    container.data.working_key_cert_json = json.dumps(cert)
    return cert


def descriptor(container, endpoints: list[str], tls_spki: list[str], server_sign_keys: list[str],
               fallback: list[str] = (), revoked_server_keys: list[str] = ()) -> dict:
    """Новый подписанный описатель сервера; номер строго растёт."""
    if not container.data.working_key_cert_json:
        raise OrgRootError("рабочий ключ контейнера не заверен корнем организации")
    serial = max(container.data.descriptor_serial + 1, int(time.time()))
    payload = {"format": 1, "serial": serial, "endpoints": list(endpoints),
               "tls_spki": list(tls_spki), "server_sign_keys": list(server_sign_keys),
               "revoked_server_keys": list(revoked_server_keys), "fallback": list(fallback),
               "issued_at": int(time.time())}
    document = poa_trust.sign_document(working_key(container),
                                       json.loads(container.data.working_key_cert_json),
                                       "descriptor", payload)
    container.data.descriptor_serial = serial
    return document
