"""Admin container lifecycle: create, open, save.

Implements UC-1 "Открытие контейнера и создание сеанса" and UC-2 "Создание
контейнера администратора", and the security requirements that the container
password is never stored (Требования к безопасности-2) and that the container
protects its contents from unauthorized reading/modification (Требования к
безопасности-3) via AES-256-GCM (confidentiality + integrity in one primitive).
"""
from __future__ import annotations

import base64
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
from .models import AdminContainerData, ConfigRecord, MediaRecord, RecoveryRecord, _now_iso

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

    @property
    def admin_secret(self) -> bytes:
        """Секрет, из которого выводятся коды восстановления.

        Заводится на месте, если контейнер создан до 29.08.2026: там его нет,
        и без этого носители, собранные таким контейнером дальше, остались бы
        вовсе без кодов. Уже выданные носители это не затрагивает -- их коды
        лежат в записях и выдаются оттуда.
        """
        if not self.data.admin_secret_b64:
            self.data.admin_secret_b64 = base64.b64encode(crypto.new_admin_secret()).decode()
        return base64.b64decode(self.data.admin_secret_b64)

    # -- media records ----------------------------------------------------
    def add_media_record(self, record: MediaRecord) -> None:
        self.data.media_records.append(record)

    def find_media(self, media_id: str) -> MediaRecord | None:
        for record in self.data.media_records:
            if record.media_id == media_id:
                return record
        return None

    # -- configurations -----------------------------------------------------
    def list_configs(self) -> "list[ConfigRecord]":
        """Конфигурации по алфавиту -- порядок списка на экране."""
        return sorted(self.data.configs, key=lambda record: record.name.lower())

    def find_config(self, name: str) -> "ConfigRecord | None":
        for record in self.data.configs:
            if record.name == name:
                return record
        return None

    def put_config(self, name: str, data: dict) -> "ConfigRecord":
        """Сохранить конфигурацию под именем, заменив прежнюю с тем же именем.

        Имя -- ключ, потому что оно же показывается администратору: две
        разные конфигурации с одинаковым именем он всё равно не различит, а
        отдельный идентификатор пришлось бы где-то показывать.
        """
        existing = self.find_config(name)
        if existing is not None:
            existing.data = data
            existing.updated_at = _now_iso()
            return existing
        record = ConfigRecord(name=name, data=data)
        self.data.configs.append(record)
        return record

    def remove_config(self, name: str) -> bool:
        """True, если было что удалять."""
        before = len(self.data.configs)
        self.data.configs = [r for r in self.data.configs if r.name != name]
        return len(self.data.configs) != before

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

    def rekey(self, key: bytes, kdf_params: crypto.KdfParams) -> None:
        """Заменить ключ, которым контейнер шифруется, оставив содержимое.

        Старый ключ затирается на месте, а не выбрасывается сборщику мусора:
        `close()` затирает его по той же причине (Требования к
        безопасности-10), и смена пароля -- ровно тот случай, когда старый
        ключ в памяти больше не нужен ни на секунду.
        """
        if self.closed:
            raise RuntimeError("container is closed")
        for i in range(len(self._key)):
            self._key[i] = 0
        self._key = bytearray(key)
        self._kdf_params = kdf_params


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
        # Секрет администратора рождается вместе с контейнером и живёт ровно
        # столько же. Из него выводятся коды восстановления всех носителей,
        # которые этот администратор когда-либо соберёт, поэтому сменить его
        # нельзя, не пересобрав их все.
        data = AdminContainerData(
            admin_secret_b64=base64.b64encode(crypto.new_admin_secret()).decode()
        )
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

    def change_password(self, container: AdminContainer, new_password: str) -> None:
        """Перешифровать открытый контейнер новым паролем.

        Носителей это не касается вовсе, и в этом главное. Пароль контейнера
        шифрует файл, а коды восстановления выводятся из секрета
        администратора, который лежит внутри и остаётся прежним: носитель,
        собранный вчера, после смены пароля откроется теми же кодами. Иначе
        смена пароля означала бы пересборку всех носителей, какие этот
        администратор когда-либо выдал.

        Контейнер должен быть открыт -- то есть старый пароль уже предъявлен.
        Спрашивать его второй раз незачем: это тот же сеанс, и других способов
        получить открытый контейнер нет.

        Соль KDF берётся новая, а не переиспользуется. Она не секрет, но одна
        и та же соль на два пароля означала бы, что по двум версиям файла
        видно, менялся пароль или нет, а перебор по обеим ведётся за одну
        цену.
        """
        problems = crypto.validate_password_strength(new_password)
        if problems:
            raise WeakPasswordError(problems)

        kdf_params = crypto.default_kdf_params()
        container.rekey(crypto.derive_key(new_password, kdf_params), kdf_params)
        self.save(container)

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
