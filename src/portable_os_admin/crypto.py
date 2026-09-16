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
import hmac
import os
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

# scrypt cost parameters. SCRYPT_N is the floor, not the working value: new
# containers get the most expensive N this machine opens within
# KDF_BUDGET_SECONDS (see calibrate_scrypt_n), up to SCRYPT_N_CEILING. The
# floor is what containers created before 11.09.2026 carry and what the
# calibration probe measures.
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


#: Сколько отводится на открытие контейнера. Пять секунд -- решение
#: пользователя: администратор открывает контейнер каждый рабочий заход, и
#: дороже этого защита начинает мешать работе.
KDF_BUDGET_SECONDS = 5.0

#: Потолок стоимости. scrypt тратит 128 * N * r байт, то есть при r=8 это
#: N=2^18 -> 256 МиБ -- ровно столько же, сколько argon2id на самом носителе.
#: Выше поднимать нечего: контейнер перестал бы открываться на машине с
#: небольшой памятью, а равенство с носителем и было целью. До 11.09.2026
#: здесь стояло N=2^15, то есть 32 МиБ: контейнер, где лежит секрет **всего
#: парка** -- коды восстановления каждого носителя, служебные ключи, описи
#: загрузчиков, -- был защищён в восемь раз слабее любого носителя, который
#: сам же и порождает.
SCRYPT_N_CEILING = 2 ** 18

#: Результат калибровки на этой машине. Считается один раз за запуск: замер
#: стоит столько же, сколько само выведение ключа, и платить его при каждом
#: создании контейнера незачем -- машина за время работы программы быстрее не
#: становится.
_calibrated_n: "int | None" = None


def calibrate_scrypt_n(budget_seconds: float = KDF_BUDGET_SECONDS) -> int:
    """Наибольшее N, при котором открытие укладывается в бюджет этой машины.

    Почему калибровка, а не число в коде. Стоимость подбирается под машину
    администратора, а платит её тот, кто подбирает пароль: у нападающего
    железо своё, и единственное, чем мы управляем, -- сколько работы стоит одна
    попытка. Зашитое число пришлось бы выбирать по самой слабой мыслимой
    машине, то есть отдавать стойкость даром на всех остальных.

    Замеряется один раз, на нижней ступени, и пересчитывается умножением:
    время scrypt растёт с N линейно. Полный перебор ступеней стоил бы столько
    же, сколько все проверенные ступени вместе, то есть вдвое дороже
    последней, -- и это при каждом создании контейнера.

    Результат округляется вниз до степени двойки: N обязан быть ею, иначе
    scrypt откажется работать.

    Ошибиться здесь можно только в одну сторону. Если машина в момент замера
    занята чем-то ещё, калибровка выйдет осторожнее -- контейнер станет
    защищён слабее задуманного, но откроется; обратной ошибки (не открыть
    вовсе) быть не может, потому что параметры едут в самом контейнере.
    """
    global _calibrated_n
    if _calibrated_n is not None:
        return _calibrated_n

    import time

    # Бюджет можно занизить снаружи, и это нужно ровно двум: тестам и стенду.
    # Тесты создают контейнеры десятками, и честные пять секунд на каждый
    # превратили бы прогон из полуминуты в часы, ничего при этом не проверив --
    # стойкость параметра проверяется его выбором, а не ожиданием. Занизить
    # можно только вниз и только явным действием: умолчание остаётся рабочим,
    # поэтому забытая переменная окружения ослабит контейнер лишь там, где её
    # задали нарочно.
    override = os.environ.get("POA_KDF_BUDGET_SECONDS")
    if override:
        try:
            budget_seconds = float(override)
        except ValueError:
            pass

    probe = KdfParams(
        algo="scrypt", n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, salt=new_salt(), length=KEY_LENGTH
    )
    started = time.monotonic()
    derive_key("calibration", probe)
    elapsed = max(time.monotonic() - started, 1e-6)

    affordable = SCRYPT_N * (budget_seconds / elapsed)
    n = SCRYPT_N
    while n * 2 <= affordable and n * 2 <= SCRYPT_N_CEILING:
        n *= 2
    _calibrated_n = n
    return n


def calibrated_kdf_params(salt: bytes | None = None) -> KdfParams:
    """Параметры для **нового** контейнера -- самые дорогие, какие машина тянет.

    Старых контейнеров это не касается и миграции не требует: параметры лежат
    в самом контейнере, и `open` берёт их оттуда. Контейнер, созданный прежней
    версией, откроется своими -- слабее, но откроется, -- а сменить их можно
    сменой пароля, которая перешифровывает контейнер целиком.
    """
    return KdfParams(
        algo="scrypt",
        n=calibrate_scrypt_n(),
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
    """Случайный код восстановления -- как их делали до 29.08.2026.

    Установщик им больше не пользуется: коды выводятся из секрета
    администратора (`derive_recovery_code`) и не хранятся нигде. Функция
    оставлена, потому что контейнеры, созданные раньше, несут в себе именно
    такие коды, и тесты совместимости должны уметь их изготовить.
    """
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


def new_media_id(prefix: str) -> str:
    """ID носителя: приставка администратора, дефис, восемь знаков носителя.

    Восьми знаков на весь мир мало: на общем сервере отзыва носители разных
    администраторов складываются в одну таблицу, и совпадение там достижимо.
    Приставка разводит их по своим пространствам -- столкнуться могут только
    носители одного контейнера, и по ID сразу видно, чей носитель.

    Приставку даёт `distribution.key_fingerprint` -- это хеш от **открытой**
    части ключа. Секрет контейнера в вычислении не участвует, и обратного хода
    нет: из восьми знаков не получить ни ключа, ни секрета.
    """
    return f"{prefix}-{new_short_id()}"


def new_short_id() -> str:
    """Восемь знаков случайности -- на временные имена, а не на ID носителя."""
    return secrets.token_hex(4)


def derive_recovery_passphrase(code: str, salt: bytes) -> bytes:
    """Derives the actual LUKS recovery-keyslot passphrase from a recovery
    code, so the code handed to the user is never the passphrase itself
    ("Восстановление.Код.НеПароль")."""
    params = recovery_kdf_params(salt=salt)
    return derive_key(code, params)


#: Длина секрета администратора. Из него выводятся все коды восстановления
#: всех носителей организации, и запоминать его человеку не нужно.
ADMIN_SECRET_BYTES = 32


def new_admin_secret() -> bytes:
    return secrets.token_bytes(ADMIN_SECRET_BYTES)


def derive_recovery_code(secret: bytes, media_id: str, slot: int) -> str:
    """Код восстановления, выведенный из секрета администратора.

    Раньше код был случайным и хранился в контейнере готовым. Разница не в
    стойкости -- она та же, -- а в том, сколько раз секретное пересекает
    границу приложения. Хранимый код уходил наружу при каждой выдаче и,
    хуже того, показывался на экране при развёртывании с припиской «запишите
    его»: то есть содержимое контейнера штатно выносилось наружу на каждом
    собранном носителе. Выведенный код не хранится нигде и вычисляется в
    момент звонка.

    Секрет из кода не восстановить: `media_id` и номер слота входят в хеш, а
    сама функция односторонняя. Компрометация одного выданного кода не даёт
    ни других кодов того же носителя, ни чужих носителей.

    HMAC, а не просто конкатенация с sha256: у HMAC ключ и данные разделены
    по построению, и склеить «секрет ‖ media_id» так, чтобы два разных набора
    дали одну строку, невозможно.
    """
    material = hmac.new(
        secret, f"{media_id}:{slot}".encode("utf-8"), hashlib.sha256
    ).digest()
    # Тот же вид, что у прежних кодов: четыре группы по четыре знака. Человек
    # диктует их по телефону, и менять привычную форму незачем.
    chars = [_RECOVERY_ALPHABET[b % len(_RECOVERY_ALPHABET)] for b in material[:16]]
    return "-".join("".join(chars[i:i + 4]) for i in range(0, 16, 4))
