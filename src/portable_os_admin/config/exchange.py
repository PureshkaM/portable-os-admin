"""Передача конфигураций между администраторами.

Конфигурации живут внутри контейнера (см. `config/manager.py`), и это
правильно ровно до того момента, когда одну из них надо отдать коллеге.
Раньше для этого хватало скопировать файл; теперь нужен способ вынести
конфигурацию наружу так, чтобы по дороге её не прочли и чтобы получатель знал,
от кого она.

**Как это выглядит для людей.** Получатель показывает свой код администратора
-- он публичный, его можно переслать почтой, продиктовать, положить в чат.
Отправитель вводит этот код, выбирает конфигурации и получает файл посылки.
Получатель открывает файл своим контейнером: приложение проверяет, что посылка
адресована именно ему, что подпись отправителя верна, и показывает, чей это
код, прежде чем что-либо принять.

**Что под этим.** Пара X25519 для шифрования и пара Ed25519 для подписи, обе
выведены из одного семени, которое хранится в контейнере. Отправитель
заводит одноразовую пару X25519 на каждую посылку, считает общий секрет с
публичным ключом получателя, из него выводит ключ AES-256-GCM. Расшифровать
может только владелец контейнера-получателя: перехваченный код не даёт
ничего, потому что он и есть публичная часть.

Подпись отвечает на второй вопрос -- «от кого». Без неё любой, кто знает
публичный код, мог бы прислать конфигурацию от чужого имени, а по ней собирают
носитель.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..errors import PortableOSAdminError

#: Формат посылки. Пишется внутрь файла, чтобы чужой файл был отвергнут по
#: имени формата, а не по разбору того, что в нём оказалось.
EXCHANGE_FORMAT = "portable-os-admin-exchange"
EXCHANGE_VERSION = 1

#: Расширение файла посылки -- своё, чтобы диалог открытия показывал только
#: посылки, а не всякий json.
EXCHANGE_SUFFIX = ".poaexch"

#: Та же азбука, что у кодов восстановления: цифры и латиница без `I`, `L`,
#: `O`, `U`. Код администратора длиннее кода восстановления и не рассчитан на
#: диктовку по телефону -- он копируется кнопкой или пересылается файлом, -- но
#: азбука общая: человек, который всё-таки перепишет его руками, не должен
#: спотыкаться на разных правилах в одной программе.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOOKALIKES = {"O": "0", "I": "1", "L": "1"}
_GROUP_SIZE = 4

#: Что кодирует код администратора: две публичные части и контрольная сумма.
_KEY_LENGTH = 32
_CHECKSUM_LENGTH = 2
_CODE_BYTES = _KEY_LENGTH * 2 + _CHECKSUM_LENGTH


class ExchangeError(PortableOSAdminError):
    """Посылку нельзя принять, и причина -- в самой посылке или в коде."""


def _b32encode(data: bytes) -> str:
    """Байты в код, группами по четыре знака.

    Своя реализация, а не `base64.b32encode`: стандартная берёт другую азбуку
    (с `I`, `L`, `O`) и добавляет `=` в хвост. Смешивать две азбуки в одной
    программе -- значит однажды принять код одной за код другой.
    """
    value = int.from_bytes(data, "big")
    length = (len(data) * 8 + 4) // 5
    chars = [_ALPHABET[(value >> shift) & 0x1F] for shift in range((length - 1) * 5, -1, -5)]
    return "-".join("".join(chars[i:i + _GROUP_SIZE]) for i in range(0, len(chars), _GROUP_SIZE))


def _b32decode(text: str, size: int) -> bytes:
    cleaned = text.strip().upper().replace("-", "").replace(" ", "").replace("\n", "")
    expected = (size * 8 + 4) // 5
    if len(cleaned) != expected:
        raise ExchangeError(
            f"код администратора состоит из {expected} знаков, получено {len(cleaned)}"
        )
    value = 0
    for char in cleaned:
        char = _LOOKALIKES.get(char, char)
        index = _ALPHABET.find(char)
        if index < 0:
            raise ExchangeError(f"недопустимый знак в коде: «{char}»")
        value = (value << 5) | index
    return value.to_bytes(size, "big")


def _checksum(payload: bytes) -> bytes:
    return hashlib.sha256(payload).digest()[:_CHECKSUM_LENGTH]


@dataclass(frozen=True)
class AdminIdentity:
    """Публичная половина администратора: чем ему шифруют и чем он подписывает."""

    encryption_key: bytes
    signing_key: bytes

    @property
    def code(self) -> str:
        payload = self.encryption_key + self.signing_key
        return _b32encode(payload + _checksum(payload))

    @property
    def fingerprint(self) -> str:
        """Короткий отпечаток -- то, что показывают рядом с именем отправителя.

        Полный код читать глазами незачем: чтобы убедиться, что посылка от
        того самого коллеги, хватает восьми знаков, а сверять их по телефону
        быстрее, чем сто три.
        """
        return _b32encode(_checksum(self.encryption_key + self.signing_key) * 3)[:9]

    @classmethod
    def from_code(cls, code: str) -> "AdminIdentity":
        raw = _b32decode(code, _CODE_BYTES)
        payload, checksum = raw[:-_CHECKSUM_LENGTH], raw[-_CHECKSUM_LENGTH:]
        if _checksum(payload) != checksum:
            raise ExchangeError(
                "код администратора не сходится с контрольной суммой -- "
                "проверьте, что он скопирован целиком"
            )
        return cls(encryption_key=payload[:_KEY_LENGTH], signing_key=payload[_KEY_LENGTH:])


def _keys_from_seed(seed: bytes) -> "tuple[x25519.X25519PrivateKey, ed25519.Ed25519PrivateKey]":
    """Две пары из одного семени -- чтобы в контейнере хранилось одно число.

    Разделены разными `info`: иначе один и тот же материал служил бы и для
    шифрования, и для подписи, а это ровно то, чего в криптографии не делают.
    """
    encryption = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"poa-exchange-x25519"
    ).derive(seed)
    signing = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"poa-exchange-ed25519"
    ).derive(seed)
    return (
        x25519.X25519PrivateKey.from_private_bytes(encryption),
        ed25519.Ed25519PrivateKey.from_private_bytes(signing),
    )


def _raw_public(key) -> bytes:
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def identity_of(container) -> AdminIdentity:
    """Публичный код этого контейнера, заводя семя при первом обращении.

    Семя появляется тогда, когда впервые понадобилось, а не при создании
    контейнера: контейнеры, сделанные раньше, обмен получают без пересоздания.
    """
    seed = container.exchange_seed
    encryption, signing = _keys_from_seed(seed)
    return AdminIdentity(_raw_public(encryption), _raw_public(signing))


def pack(container, configs: "list[tuple[str, dict]]", recipient_code: str) -> bytes:
    """Посылка для получателя: зашифрована его ключом, подписана нашим.

    `configs` -- пары «имя, содержимое», как они лежат в контейнере. Имена
    сохраняются: получатель увидит их до того, как согласится принять.
    """
    if not configs:
        raise ExchangeError("нечего отправлять: не выбрано ни одной конфигурации")
    recipient = AdminIdentity.from_code(recipient_code)
    sender_encryption, sender_signing = _keys_from_seed(container.exchange_seed)
    sender = AdminIdentity(_raw_public(sender_encryption), _raw_public(sender_signing))

    payload = json.dumps(
        {"configs": [{"name": name, "data": data} for name, data in configs]},
        ensure_ascii=False,
    ).encode("utf-8")

    ephemeral = x25519.X25519PrivateKey.generate()
    ephemeral_public = _raw_public(ephemeral)
    shared = ephemeral.exchange(x25519.X25519PublicKey.from_public_bytes(recipient.encryption_key))
    # В вывод ключа входят обе публичные части, а не только общий секрет:
    # так ключ привязан к этой паре сторон, и посылку нельзя переадресовать,
    # подставив другого получателя в заголовок.
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"poa-exchange-v1" + ephemeral_public + recipient.encryption_key,
    ).derive(shared)

    nonce = AESGCM.generate_key(bit_length=128)[:12]
    ciphertext = AESGCM(key).encrypt(nonce, payload, ephemeral_public + recipient.encryption_key)
    signature = sender_signing.sign(nonce + ciphertext + ephemeral_public + recipient.encryption_key)

    return json.dumps(
        {
            "format": EXCHANGE_FORMAT,
            "version": EXCHANGE_VERSION,
            "recipient": recipient.code,
            "sender": sender.code,
            "ephemeral": base64.b64encode(ephemeral_public).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(ciphertext).decode(),
            "signature": base64.b64encode(signature).decode(),
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")


def unpack(container, blob: bytes) -> "tuple[AdminIdentity, list[tuple[str, dict]]]":
    """Разобрать посылку: кто прислал и что внутри.

    Порядок проверок -- от дешёвых к дорогим и от общих к частным: формат,
    адресат, подпись, расшифровка. Каждая отвечает своей ошибкой, потому что
    администратору важно различать «это не мне» и «это подделали».
    """
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExchangeError(f"файл не похож на посылку: {exc}") from exc
    if envelope.get("format") != EXCHANGE_FORMAT:
        raise ExchangeError("файл не является посылкой этой программы")
    if envelope.get("version") != EXCHANGE_VERSION:
        raise ExchangeError(
            f"посылка версии {envelope.get('version')}, эта программа понимает "
            f"версию {EXCHANGE_VERSION}"
        )

    me = identity_of(container)
    if envelope.get("recipient") != me.code:
        raise ExchangeError(
            "посылка адресована другому администратору -- расшифровать её этим "
            "контейнером нельзя"
        )
    sender = AdminIdentity.from_code(envelope.get("sender", ""))

    try:
        ephemeral_public = base64.b64decode(envelope["ephemeral"])
        nonce = base64.b64decode(envelope["nonce"])
        ciphertext = base64.b64decode(envelope["ciphertext"])
        signature = base64.b64decode(envelope["signature"])
    except (KeyError, ValueError) as exc:
        raise ExchangeError(f"посылка повреждена: {exc}") from exc

    signed = nonce + ciphertext + ephemeral_public + me.encryption_key
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(sender.signing_key).verify(signature, signed)
    except InvalidSignature as exc:
        raise ExchangeError(
            "подпись отправителя не сходится: посылку изменили по дороге или "
            "прислал не тот, чей код указан"
        ) from exc

    encryption, _ = _keys_from_seed(container.exchange_seed)
    shared = encryption.exchange(x25519.X25519PublicKey.from_public_bytes(ephemeral_public))
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"poa-exchange-v1" + ephemeral_public + me.encryption_key,
    ).derive(shared)
    try:
        payload = AESGCM(key).decrypt(
            nonce, ciphertext, ephemeral_public + me.encryption_key
        )
    except InvalidTag as exc:
        raise ExchangeError("посылку не удалось расшифровать: она повреждена") from exc

    try:
        parsed = json.loads(payload.decode("utf-8"))
        configs = [(item["name"], item["data"]) for item in parsed["configs"]]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ExchangeError(f"содержимое посылки не разобрано: {exc}") from exc
    if not configs:
        raise ExchangeError("в посылке нет ни одной конфигурации")
    return sender, configs
