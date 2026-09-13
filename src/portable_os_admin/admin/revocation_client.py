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
import hashlib
import urllib.error
import urllib.request

from ..errors import PortableOsAdminError

#: Метка вывода ключа полномочий. Та же, что в install/revocation.py, потому
#: что ключ тот же -- он выводится из отдельного семени контейнера.
_KEY_INFO = b"poa-revocation-admin-ed25519"

#: Сколько ждать сервер. Команда короткая; если ресурс не отвечает за это
#: время, администратору важнее увидеть отказ, чем ждать.
TIMEOUT_SECONDS = 15


class RevocationServerError(PortableOsAdminError):
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


def publish_state(
    url: str,
    seed: bytes,
    media_id: str,
    state: str,
    counter: int,
    *,
    timeout: int = TIMEOUT_SECONDS,
    opener=urllib.request.urlopen,
) -> str:
    """Сообщает серверу состояние одного носителя. Возвращает строку для человека.

    `opener` подменяется в тестах: сеть в них не нужна, а проверять надо то,
    что уходит на сервер, и то, как разбирается ответ.
    """
    if state not in ("ok", "revoked"):
        raise RevocationServerError(f"неизвестное состояние носителя: {state}")

    body = f"poa-media {media_id} {counter} {state}".encode("ascii")
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
