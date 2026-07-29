"""Admin container lifecycle: create, open, save.

Implements UC-1 "Открытие контейнера и создание сеанса" and UC-2 "Создание
контейнера администратора", and the security requirements that the container
password is never stored (Требования к безопасности-2) and that the container
protects its contents from unauthorized reading/modification (Требования к
безопасности-3) via AES-256-GCM (confidentiality + integrity in one primitive).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .. import crypto
from ..errors import (
    ContainerAlreadyExistsError,
    ContainerAuthenticationError,
    ContainerCorruptedError,
    WeakPasswordError,
)
from .models import AdminContainerData, MediaRecord, RecoveryRecord

CONTAINER_FORMAT = "portable-os-admin-container"
CONTAINER_VERSION = 1


class AdminContainer:
    """An opened admin container: decrypted data plus the key needed to re-save it.

    The key is held in memory only for the lifetime of the session; `close()`
    best-effort zeroes it out (Требования к безопасности-10)."""

    def __init__(self, path: Path, key: bytes, kdf_params: crypto.KdfParams, data: AdminContainerData):
        self.path = path
        self._key = bytearray(key)
        self._kdf_params = kdf_params
        self.data = data
        self.closed = False

    # -- media records ----------------------------------------------------
    def add_media_record(self, record: MediaRecord) -> None:
        self.data.media_records.append(record)

    def find_media(self, media_id: str) -> MediaRecord | None:
        for record in self.data.media_records:
            if record.media_id == media_id:
                return record
        return None

    # -- recovery records ---------------------------------------------------
    def add_recovery_record(self, record: RecoveryRecord) -> None:
        self.data.recovery_records.append(record)

    def find_recovery(self, media_id: str, slot: int) -> RecoveryRecord | None:
        for record in self.data.recovery_records:
            if record.media_id == media_id and record.slot == slot:
                return record
        return None

    def close(self) -> None:
        for i in range(len(self._key)):
            self._key[i] = 0
        self.closed = True

    @property
    def key(self) -> bytes:
        if self.closed:
            raise RuntimeError("container is closed")
        return bytes(self._key)

    @property
    def kdf_params(self) -> crypto.KdfParams:
        return self._kdf_params


class ContainerManager:
    """Creates and opens admin-container files on disk."""

    def create(self, path: Path, password: str) -> AdminContainer:
        path = Path(path)
        if path.exists():
            raise ContainerAlreadyExistsError(f"файл контейнера уже существует: {path}")

        problems = crypto.validate_password_strength(password)
        if problems:
            raise WeakPasswordError(problems)

        kdf_params = crypto.default_kdf_params()
        key = crypto.derive_key(password, kdf_params)
        data = AdminContainerData()
        container = AdminContainer(path, key, kdf_params, data)
        self.save(container)
        return container

    def open(self, path: Path, password: str) -> AdminContainer:
        path = Path(path)
        if not path.exists():
            raise ContainerCorruptedError(f"файл контейнера не найден: {path}")

        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ContainerCorruptedError(f"не удалось прочитать контейнер: {exc}") from exc

        try:
            if envelope.get("format") != CONTAINER_FORMAT:
                raise ContainerCorruptedError("неизвестный формат файла контейнера")
            kdf_params = crypto.KdfParams.from_dict(envelope["kdf"])
            nonce = _b64decode(envelope["nonce"])
            ciphertext = _b64decode(envelope["ciphertext"])
        except (KeyError, ValueError) as exc:
            raise ContainerCorruptedError(f"повреждена структура контейнера: {exc}") from exc

        key = crypto.derive_key(password, kdf_params)
        try:
            plaintext = crypto.decrypt(key, nonce, ciphertext)
        except ValueError as exc:
            raise ContainerAuthenticationError("неверный пароль контейнера") from exc

        try:
            payload = json.loads(plaintext.decode("utf-8"))
            data = AdminContainerData.from_dict(payload)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ContainerCorruptedError(f"повреждено содержимое контейнера: {exc}") from exc

        return AdminContainer(path, key, kdf_params, data)

    def save(self, container: AdminContainer) -> None:
        plaintext = json.dumps(container.data.to_dict()).encode("utf-8")
        nonce, ciphertext = crypto.encrypt(container.key, plaintext)
        envelope = {
            "format": CONTAINER_FORMAT,
            "version": CONTAINER_VERSION,
            "kdf": container.kdf_params.to_dict(),
            "cipher": "AES-256-GCM",
            "nonce": _b64encode(nonce),
            "ciphertext": _b64encode(ciphertext),
        }
        _atomic_write(container.path, json.dumps(envelope, indent=2))


def _b64encode(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _b64decode(data: str) -> bytes:
    import base64

    return base64.b64decode(data)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".container", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
