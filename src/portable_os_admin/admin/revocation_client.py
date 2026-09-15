"""Команды ресурсу отзыва: приложение говорит серверу о своих носителях.

Почему это здесь, а не выгрузкой файлов. Выгрузка означала, что администратор
идёт на сервер по SSH и кладёт файлы руками, -- то есть имеет там права,
которые для его работы не нужны. Здесь он не имеет никаких: он подписывает
команду про один свой носитель ключом, который не покидает контейнера, а
сервер эту подпись проверяет.

Команда всегда про **один** носитель, и это не мелочь. Администраторов может
быть несколько; публикация общего списка означала бы, что любой из них
переписывает чужие записи -- в том числе снимает отзыв, поставленный другим.
Сервер помнит, каким ключом носитель заведён, и принимает команды о нём только
от того же ключа.

Номер команды строго возрастает и хранится в записи носителя: без него однажды
подписанную команду «этот носитель в порядке» можно было бы прислать повторно
и оживить отозванный.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..errors import PortableOSAdminError

#: Метка вывода ключа полномочий. Та же, что в install/revocation.py, потому
#: что ключ тот же -- он выводится из отдельного семени контейнера.
_KEY_INFO = b"poa-revocation-admin-ed25519"

#: Сколько ждать сервер. Команда короткая; если ресурс не отвечает за это
#: время, администратору важнее увидеть отказ, чем ждать.
TIMEOUT_SECONDS = 15


class RevocationServerError(PortableOSAdminError):
    """Сервер не принял команду -- с причиной, понятной человеку."""


def signing_key(seed: bytes):
    """Ключ полномочий администратора из семени контейнера."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    material = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_INFO
    ).derive(bytes(seed))
    return ed25519.Ed25519PrivateKey.from_private_bytes(material)


def key_id(seed: bytes) -> str:
    """Отпечаток ключа: по нему сервер узнаёт владельца записи.

    Считается ровно так же, как на сервере -- по сырым байтам открытой части,
    -- иначе владелец записи и предъявитель команды не совпали бы, и
    администратор не смог бы распорядиться собственным носителем.
    """
    from cryptography.hazmat.primitives import serialization

    raw = signing_key(seed).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def public_key_pem(seed: bytes) -> str:
    """Открытая часть: её владелец сервера кладёт в каталог допущенных.

    Это всё, что сервер узнаёт об администраторе. Закрытая часть остаётся в
    контейнере, поэтому взломанный сервер не может выдать себя за него.
    """
    from cryptography.hazmat.primitives import serialization

    return (
        signing_key(seed)
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def fetch_public_key(
    url: str,
    *,
    timeout: int = TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
) -> str:
    """Забирает у сервера открытую часть ключа отзыва.

    Ключ рождается на сервере, а не здесь: сервер разворачивают первым, когда
    приложения ещё нет. Носители носят в себе эту открытую часть и по ней
    проверяют подпись ответа «отозван».
    """
    request = urllib.request.Request(f"{url.rstrip('/')}/pubkey", method="GET")
    try:
        with opener(request, timeout=timeout) as response:
            pem = response.read(4096).decode("ascii")
    except urllib.error.HTTPError as error:
        raise RevocationServerError(
            f"ресурс отзыва не отдал открытый ключ (код {error.code})"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise RevocationServerError(f"ресурс отзыва недоступен: {error}") from error

    if "BEGIN PUBLIC KEY" not in pem:
        raise RevocationServerError(
            "по этому адресу отвечает не ресурс отзыва: вместо открытого ключа "
            "пришло что-то другое"
        )
    return pem


def enroll(
    url: str,
    seed: bytes,
    code: str,
    *,
    timeout: int = TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
) -> str:
    """Знакомство: отправляет серверу открытую часть ключа полномочий.

    По одноразовому коду, который печатает установка сервера. Иначе выбор был
    бы между «сервер верит любому, кто постучится первым» и «администратор
    лезет на сервер по SSH» -- а лишних прав там у него быть не должно.

    Закрытая часть при этом никуда не уходит и контейнера не покидает.
    """
    if not code.strip():
        raise RevocationServerError("нужен код знакомства, выданный при установке сервера")

    body = public_key_pem(seed).encode("ascii")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/admin/enroll",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-pem-file",
            "X-POA-Enrollment": code.strip(),
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            response.read(256)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise RevocationServerError(
                "код знакомства не подошёл. Он одноразовый: если этим сервером уже "
                "пользуется другой администратор, новый код выписывает владелец сервера"
            ) from error
        raise RevocationServerError(
            f"ресурс отзыва отказал в знакомстве (код {error.code})"
        ) from error
    except OSError as error:
        raise RevocationServerError(f"ресурс отзыва недоступен: {error}") from error

    return f"контейнер допущен к ресурсу отзыва, отпечаток ключа {key_id(seed)}"


def publish_state(
    url: str,
    seed: bytes,
    media_id: str,
    state: str,
    counter: int,
    *,
    device_public_key_b64: str = "",
    timeout: int = TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
) -> str:
    """Сообщает серверу состояние одного носителя. Возвращает строку для человека.

    `device_public_key_b64` -- открытый ключ носителя. Есть -- уходит пятым
    полем при каждой команде: для нового носителя без него сервер запись не
    заведёт, для известного тот же ключ ничего не меняет, а другой означает
    перевыпуск. Нет (носитель записан до 1.1.0) -- команда из четырёх полей,
    которую сервер примет только для уже заведённой записи.

    `opener` подменяется в тестах: сеть в них не нужна, а проверять надо то,
    что уходит на сервер, и то, как разбирается ответ.
    """
    if state not in ("ok", "revoked"):
        raise RevocationServerError(f"неизвестное состояние носителя: {state}")
    device_key = device_public_key_b64.strip()
    if device_key and not _is_device_key(device_key):
        # Испорченный ключ лучше поймать здесь, чем получить от сервера
        # безликое «не разобрал команду».
        raise RevocationServerError(
            f"ключ носителя {media_id} в контейнере повреждён: это не открытый ключ Ed25519"
        )

    fields = f"poa-media {media_id} {counter} {state}"
    if device_key:
        fields += f" {device_key}"
    body = fields.encode("ascii")
    signature = base64.b64encode(signing_key(seed).sign(body)).decode("ascii")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/admin/media/{media_id}",
        data=body,
        method="POST",
        headers={
            "Content-Type": "text/plain; charset=ascii",
            "X-POA-Key-Id": key_id(seed),
            "X-POA-Signature": signature,
        },
    )

    try:
        with opener(request, timeout=timeout) as response:
            code = response.getcode()
    except urllib.error.HTTPError as error:
        if error.code == 400 and not device_key:
            # Самый вероятный 400 без ключа -- сервер впервые слышит о носителе
            # и без ключа запись не заводит. Сказать об этом надо прямо: иначе
            # администратор будет искать ошибку в команде, а дело в носителе.
            raise RevocationServerError(
                f"носитель {media_id} записан версией до 1.1.0: у него нет ключа "
                "носителя, а без ключа ресурс отзыва носитель не заводит. "
                "Перезапишите носитель, чтобы он получил ключ"
            ) from error
        raise RevocationServerError(_explain(error.code, media_id)) from error
    except OSError as error:
        # Сеть, имя, сертификат -- всё сюда: администратору важно, что команда
        # не дошла, а не какой слой этому виной.
        raise RevocationServerError(
            f"ресурс отзыва недоступен: {error}"
        ) from error

    if code == 201:
        return f"носитель {media_id} заведён на ресурсе отзыва, состояние «{state}»"
    return f"носитель {media_id}: состояние «{state}» принято ресурсом отзыва"


@dataclass(frozen=True)
class MediaStatus:
    """Что сервер знает о носителе. Пустые строки -- носитель не выходил на связь."""

    state: str
    last_seen: str = ""   # UTC ISO, как записал сервер
    version: str = ""
    machine: str = ""


def query_media(
    url: str,
    seed: bytes,
    media_id: str,
    *,
    timeout: int = TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
) -> MediaStatus:
    """Спрашивает сервер о своём носителе: состояние и последнюю связь.

    Подписывается строка `poa-query <id> <время>`, а не случайное число:
    запрос только читает, повтор перехваченного в пределах пяти минут отдаст
    тот же ответ и ничего больше, а часам машины администратора, в отличие от
    переносного носителя, доверять можно.
    """
    moment = str(int(time.time()))
    message = f"poa-query {media_id} {moment}".encode("ascii")
    signature = base64.b64encode(signing_key(seed).sign(message)).decode("ascii")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/admin/media/{media_id}",
        method="GET",
        headers={
            "X-POA-Key-Id": key_id(seed),
            "X-POA-Time": moment,
            "X-POA-Signature": signature,
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            text = response.read(1024).decode("ascii")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RevocationServerError(
                f"носитель {media_id} не заведён на ресурсе отзыва"
            ) from error
        if error.code == 403:
            raise RevocationServerError(
                f"ресурс отзыва не ответил о носителе {media_id}: этот контейнер не "
                "владелец носителя, ключ контейнера не допущен или часы этой машины "
                "расходятся с сервером больше чем на пять минут"
            ) from error
        raise RevocationServerError(
            f"ресурс отзыва ответил отказом (код {error.code})"
        ) from error
    except (OSError, UnicodeDecodeError) as error:
        raise RevocationServerError(f"ресурс отзыва недоступен: {error}") from error

    parts = text.split()
    if (
        len(parts) != 6
        or parts[0] != "poa-record"
        or parts[1] != media_id
        or parts[2] not in ("ok", "revoked")
    ):
        raise RevocationServerError(
            "ответ ресурса отзыва не разобран: по этому адресу отвечает не он "
            "или сервер старее 1.1.0"
        )
    last_seen, version, machine = (value if value != "-" else "" for value in parts[3:])
    return MediaStatus(parts[2], last_seen, version, machine)


def _is_device_key(text: str) -> bool:
    """Похоже ли на открытый ключ носителя: base64 ровно 32 байт."""
    try:
        return len(base64.b64decode(text, validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def _explain(code: int, media_id: str) -> str:
    """Отказ сервера человеческими словами, а не кодом."""
    if code == 403:
        return (
            f"ресурс отзыва не признал этот контейнер владельцем носителя {media_id}. "
            "Носителем распоряжается тот администратор, который его выпустил; "
            "если ключ этого контейнера ещё не допущен на сервер, его открытую "
            "часть нужно передать владельцу сервера"
        )
    if code == 409:
        return (
            f"номер команды для носителя {media_id} устарел: на сервере уже есть "
            "более поздняя. Обновите сведения о носителе и повторите"
        )
    if code == 400:
        return f"ресурс отзыва не разобрал команду про носитель {media_id}"
    return f"ресурс отзыва ответил отказом (код {code})"
