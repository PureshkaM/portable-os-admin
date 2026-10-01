"""Ключи сервера доступа.

Сегодня -- из каталога учётных данных systemd (`LoadCredential`, как ключ
отзыва v2). Этап Э21 заменит источник на запечатанную связку: на диске только
шифротекст, ключ данных -- в памяти, распечатывает администратор. Остальной
сервер этого не заметит: он берёт ключи только отсюда.

Ключ данных (`data-key`, 32 байта) шифрует то, что хранится в базе и не должно
читаться из её копии: семена TOTP и пароли `.dst`. AES-256-GCM, связанные данные
-- назначение и идентификатор носителя, чтобы шифротекст одной записи нельзя
было подставить в другую.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: Имена файлов в каталоге учётных данных.
SIGN_KEY = "revocation-key"     # Ed25519, тот же, что подписывает ответы v2
CA_KEY = "ca-key"               # ECDSA P-256 в PEM
CA_CERT = "ca-cert"             # сертификат CA в PEM
TLS_CRYPT_V2 = "tls-crypt-v2"   # серверный ключ OpenVPN tls-crypt-v2
DATA_KEY = "data-key"           # 32 сырых байта


@dataclass
class ServerSecrets:
    sign_key: object
    ca_key: object | None = None
    ca_cert_pem: bytes = b""
    tls_crypt_v2_server: bytes = b""
    data_key: bytes = b""

    # -- шифрование полей базы ------------------------------------------------
    def seal(self, purpose: str, media: str, plaintext: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + AESGCM(self.data_key).encrypt(nonce, plaintext, f"{purpose}:{media}".encode())

    def unseal(self, purpose: str, media: str, blob: bytes) -> bytes:
        return AESGCM(self.data_key).decrypt(blob[:12], blob[12:], f"{purpose}:{media}".encode())

    @property
    def v3_ready(self) -> bool:
        """Всё ли есть для выдачи доступа. Без этого v3 отвечает 503, v2 работает."""
        return bool(self.ca_key and self.ca_cert_pem and len(self.data_key) == 32)


def load(directory: pathlib.Path, sign_key_path: pathlib.Path | None = None) -> ServerSecrets:
    """Читает ключи из каталога; отсутствующие ключи v3 -- не ошибка (сервер v2)."""
    def read(name: str) -> bytes | None:
        path = directory / name
        return path.read_bytes() if path.is_file() else None

    sign_raw = sign_key_path.read_bytes() if sign_key_path else read(SIGN_KEY)
    if sign_raw is None:
        raise FileNotFoundError("нет ключа подписи сервера")
    secrets = ServerSecrets(sign_key=serialization.load_pem_private_key(sign_raw, password=None))
    ca_key = read(CA_KEY)
    if ca_key:
        secrets.ca_key = serialization.load_pem_private_key(ca_key, password=None)
    secrets.ca_cert_pem = read(CA_CERT) or b""
    secrets.tls_crypt_v2_server = read(TLS_CRYPT_V2) or b""
    secrets.data_key = read(DATA_KEY) or b""
    return secrets
