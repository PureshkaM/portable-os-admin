"""Cryptographic primitives used by the admin container and the recovery flow.

Requirements traced to the course-project spec:
  - "Контейнер.Пароль.Хранение" / "Требования к безопасности-2": the container
    password is never stored, only used to derive a key via scrypt.
  - "Восстановление.Код.НеПароль": the recovery code handed to the user is never
    used directly as a LUKS passphrase -- it is a seed that gets re-derived
    through the same KDF into the actual passphrase used on the recovery keyslot.
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt cost parameters. n=2**15 keeps container open times well under a
# second on typical hardware while remaining expensive for offline guessing.
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LENGTH = 32
SALT_LENGTH = 16
GCM_NONCE_LENGTH = 12

# Reduced cost profile used only for the recovery-passphrase KDF, where the
# "password" is a high-entropy random code rather than something a human
# picked, so a lower cost is an acceptable trade-off for faster recovery.
RECOVERY_SCRYPT_N = 2 ** 14


@dataclass(frozen=True)
class KdfParams:
    algo: str
    n: int
    r: int
    p: int
    salt: bytes
    length: int

    def to_dict(self) -> dict:
        return {
            "algo": self.algo,
            "n": self.n,
            "r": self.r,
            "p": self.p,
            "salt": base64.b64encode(self.salt).decode("ascii"),
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KdfParams":
        return cls(
            algo=data["algo"],
            n=int(data["n"]),
            r=int(data["r"]),
            p=int(data["p"]),
            salt=base64.b64decode(data["salt"]),
            length=int(data["length"]),
        )


def new_salt(length: int = SALT_LENGTH) -> bytes:
    return secrets.token_bytes(length)


def derive_key(password: str, params: KdfParams) -> bytes:
    if params.algo != "scrypt":
        raise ValueError(f"unsupported KDF algorithm: {params.algo}")
    kdf = Scrypt(salt=params.salt, length=params.length, n=params.n, r=params.r, p=params.p)
    return kdf.derive(password.encode("utf-8"))


def default_kdf_params(salt: bytes | None = None) -> KdfParams:
    return KdfParams(
        algo="scrypt",
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        salt=salt if salt is not None else new_salt(),
        length=KEY_LENGTH,
    )


def recovery_kdf_params(salt: bytes | None = None, length: int = 64) -> KdfParams:
    return KdfParams(
        algo="scrypt",
        n=RECOVERY_SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        salt=salt if salt is not None else new_salt(),
        length=length,
    )


def encrypt(key: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    nonce = os.urandom(GCM_NONCE_LENGTH)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce, ciphertext


def decrypt(key: bytes, nonce: bytes, ciphertext: bytes, aad: bytes = b"") -> bytes:
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise ValueError("authentication tag mismatch (wrong password or corrupted data)") from exc


def validate_password_strength(password: str, min_length: int = 12) -> list[str]:
    """Implements "Контейнер.Создание.Пароль.Требования" — returns a list of
    human-readable problems; an empty list means the password is acceptable."""
    problems = []
    if len(password) < min_length:
        problems.append(f"пароль должен содержать не менее {min_length} символов")
    if not any(c.isalpha() for c in password):
        problems.append("пароль должен содержать хотя бы одну букву")
    if not any(c.isdigit() for c in password):
        problems.append("пароль должен содержать хотя бы одну цифру")
    return problems


_RECOVERY_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # Crockford-ish, no 0/O/1/I/L


def generate_recovery_code(group_count: int = 4, group_size: int = 4) -> str:
    """Generates a human-typable, high-entropy recovery code, e.g. "K7ZQ-9XPT-3F2M-QA57".

    This code is what the administrator reads out to the user; it is never
    itself a LUKS passphrase (see module docstring)."""
    groups = []
    for _ in range(group_count):
        groups.append("".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(group_size)))
    return "-".join(groups)


def short_checksum(*parts: str, length: int = 8) -> str:
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def new_volume_key(length: int = 64) -> bytes:
    """A random key for a LUKS keyslot that a human never types.

    Used for the log and exchange volumes, which are opened by the portable
    system from a key file and by the administrator from the key kept in the
    administrator container -- never from a passphrase. 64 bytes of entropy
    because nothing has to remember it.
    """
    return secrets.token_bytes(length)


def new_media_id() -> str:
    return secrets.token_hex(4)


def derive_recovery_passphrase(code: str, salt: bytes) -> bytes:
    """Derives the actual LUKS recovery-keyslot passphrase from a recovery
    code, so the code handed to the user is never the passphrase itself
    ("Восстановление.Код.НеПароль")."""
    params = recovery_kdf_params(salt=salt)
    return derive_key(code, params)
