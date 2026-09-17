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
    MediaNotRegisteredError,
    WeakPasswordError,
)
from .models import (
    ACTION_ERASED,
    ACTION_RESTORED,
    ACTION_REVOKED,
    STATUS_ACTIVE,
    STATUS_REVOKED,
    AdminContainerData,
    ConfigRecord,
    JournalEntry,
    MediaRecord,
    RecoveryRecord,
    _now_iso,
)

CONTAINER_FORMAT = "portable-os-admin-container"
#: Версия формата. Со 2 в ней есть журнал и состояние записи носителя.
#:
#: `open` её намеренно не проверяет: контейнер живёт дольше программы, и
#: отказать в открытии из-за незнакомого номера -- худший из способов сообщить
#: о разнице версий. Совместимость держится на другом: неизвестные ключи
#: отбрасываются, отсутствующие берут умолчание, и контейнер версии 1
#: открывается этим кодом без единой правки файла.
CONTAINER_VERSION = 2


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

    @property
    def admin_authority_secret(self) -> bytes:
        """Семя ключа, которым администратор подписывает команды серверу отзыва.

        Третий ключ, и снова отдельный -- по той же причине, по какой отделён
        ключ отзыва, только с другой стороны. Ключ отзыва уезжает на сервер
        целиком: без него тот не подпишет ответ носителю. Этот не уезжает
        никогда, на сервере лежит только его открытая часть.

        Что это даёт: администратору не нужен ни доступ к оболочке сервера, ни
        токен, который можно украсть и предъявить в другом месте, -- он просто
        подписывает новый список состояний. А взломанный сервер не может выдать
        себя за администратора: подделать подпись ему нечем.
        """
        if not self.data.admin_authority_secret_b64:
            self.data.admin_authority_secret_b64 = base64.b64encode(
                crypto.new_admin_secret()
            ).decode()
        return base64.b64decode(self.data.admin_authority_secret_b64)

    # -- media records ----------------------------------------------------
    def add_media_record(self, record: MediaRecord) -> None:
        self.data.media_records.append(record)

    def find_media(self, media_id: str) -> MediaRecord | None:
        for record in self.data.media_records:
            if record.media_id == media_id:
                return record
        # Код восстановления везёт только собственную часть ID -- приставка
        # администратора в нём не нужна, она у контейнера одна. Поэтому запись
        # ищется и по окончанию, но только если такая запись одна: два
        # совпавших окончания -- это уже не опознание, и лучше «не найдено»,
        # чем выданный код от чужого носителя.
        tail = media_id.rpartition("-")[2] or media_id
        found = [
            record
            for record in self.data.media_records
            if record.media_id.rpartition("-")[2] == tail
        ]
        return found[0] if len(found) == 1 else None

    def revoke_media(self, media_id: str, reason: str, *, erased: bool = False) -> MediaRecord:
        """Пометить носитель отозванным -- и записать это в журнал.

        Пометка ничего не запирает. Носитель, который не вернули, остаётся
        рабочим: у того, кто его держит, есть пароль или нет его, и отметка в
        контейнере администратора на это не влияет никак. Она отвечает на
        другой вопрос -- числится ли носитель действующим, -- и это всё, что
        администратор может сделать, пока флешки нет у него в руках.

        `erased` отличает две записи в журнале: пометку от настоящего
        стирания. Состояние после обеих одно, а произошедшее -- разное, и
        через год по журналу должно быть видно, какой из носителей
        действительно мёртв.
        """
        record = self.find_media(media_id)
        if record is None:
            raise MediaNotRegisteredError(
                f"сведения о носителе не найдены в контейнере (media_id={media_id})"
            )
        record.status = STATUS_REVOKED
        record.revoked_at = _now_iso()
        record.revoked_reason = reason.strip()
        self.record_event(
            ACTION_ERASED if erased else ACTION_REVOKED, media_id, record.revoked_reason
        )
        return record

    def restore_media(self, media_id: str) -> MediaRecord:
        """Снять отметку отзыва.

        Отзыв -- решение человека, а человек ошибается: отозвать можно не ту
        строку, а сотрудник, которого «уволили», может вернуться из отпуска.
        Причина отзыва при этом стирается, а журнал остаётся: состояние
        показывает, как есть сейчас, история -- как было.
        """
        record = self.find_media(media_id)
        if record is None:
            raise MediaNotRegisteredError(
                f"сведения о носителе не найдены в контейнере (media_id={media_id})"
            )
        record.status = STATUS_ACTIVE
        record.revoked_at = ""
        record.revoked_reason = ""
        self.record_event(ACTION_RESTORED, media_id)
        return record

    # -- журнал контейнера ---------------------------------------------------
    def record_event(self, action: str, media_id: str = "", details: str = "") -> JournalEntry:
        entry = JournalEntry(at=_now_iso(), action=action, media_id=media_id, details=details)
        self.data.journal.append(entry)
        return entry

    def journal_for(self, media_id: str) -> "list[JournalEntry]":
        """События одного носителя, новые сверху.

        Сверху потому, что смотрят в него ради последнего: «что с ним успели
        сделать», а не «с чего всё начиналось».
        """
        found = [entry for entry in self.data.journal if entry.media_id == media_id]
        return list(reversed(found))

    @property
    def exchange_seed(self) -> bytes:
        """Семя ключей обмена. Заводится при первом обращении.

        Не при создании контейнера: контейнеры, сделанные раньше, должны
        получить обмен без пересоздания, а лишнее случайное число в файле,
        которым никто не пользуется, -- это ещё один секрет, который надо
        хранить.
        """
        if not self.data.exchange_seed_b64:
            self.data.exchange_seed_b64 = base64.b64encode(os.urandom(32)).decode()
        return base64.b64decode(self.data.exchange_seed_b64)

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

    def scrub(self) -> None:
        """Затереть всё, что поддаётся, и закрыть контейнер.

        Ключ лежит в `bytearray` и затирается на месте -- это настоящее
        затирание. С секретами в данных так нельзя: они хранятся строками
        base64, а строка в Python неизменяема, и присваивание лишь теряет
        ссылку, оставляя прежние байты в куче до сборщика мусора. Поэтому
        здесь не «стирание», а сокращение окна: ссылки обрываются, списки
        записей очищаются.

        Настоящую гарантию даёт только выход из процесса -- страницы
        возвращаются ядру и обнуляются перед выдачей другому. Поэтому
        `PortableOSAdminGUI.close_session` завершает программу, а не
        возвращается на начальный экран.

        Затирается больше, чем главный секрет: семя ключа отзыва, семя обмена
        и ключ служебных томов у каждой записи носителя. Последний открывает
        тома журналов и обмена **без** пароля пользователя -- то есть ценен
        ровно настолько же, насколько сам носитель.

        Сохранить контейнер после этого невозможно: `key` у закрытого
        контейнера бросает исключение, и `save` упадёт, не записав пустоту
        поверх работы администратора.
        """
        data = self.data
        data.admin_secret_b64 = ""
        data.admin_authority_secret_b64 = ""
        data.exchange_seed_b64 = ""
        for record in data.media_records:
            record.service_key_b64 = ""
        data.media_records.clear()
        data.recovery_records.clear()
        data.configs.clear()
        data.journal.clear()
        self.close()

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

        kdf_params = crypto.calibrated_kdf_params()
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

        kdf_params = crypto.calibrated_kdf_params()
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
