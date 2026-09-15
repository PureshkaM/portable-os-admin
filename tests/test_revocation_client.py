"""Команды ресурсу отзыва: что именно уходит на сервер и как читается ответ.

Сеть здесь не нужна и вредна: проверять надо форму команды и разбор ответов, а
не работоспособность чужой машины. Живой прогон против настоящего сервера
проведён отдельно (см. `docs/REVOCATION-SERVER.md`).
"""
from __future__ import annotations

import base64
import urllib.error

import pytest

from portable_os_admin.admin import revocation_client as client

SEED = b"\x07" * 32


class _Answer:
    """Ответ сервера в том виде, в каком его отдаёт urlopen."""

    def __init__(self, code: int) -> None:
        self._code = code

    def getcode(self) -> int:
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _capture(code: int = 204):
    """Подменный отправитель: запоминает запрос и отвечает заданным кодом."""
    seen = {}

    def opener(request, timeout=None):
        seen["request"] = request
        seen["timeout"] = timeout
        return _Answer(code)

    return opener, seen


class _Reply(_Answer):
    """Ответ с телом: его читают знакомство и запрос открытого ключа."""

    def __init__(self, code: int, body: bytes) -> None:
        super().__init__(code)
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body


PEM = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAVDgvWTswVDgvWTswVDgvWTswVDgvWTswVDgvWTswVDg=\n"
    "-----END PUBLIC KEY-----\n"
)


def test_the_public_key_is_taken_from_the_server():
    """Ключ отзыва рождается на сервере: его разворачивают раньше приложения."""
    seen = {}

    def opener(request, timeout=None):
        seen["url"] = request.full_url
        return _Reply(200, PEM.encode("ascii"))

    assert client.fetch_public_key("https://revoke.example.org/", opener=opener) == PEM
    assert seen["url"] == "https://revoke.example.org/pubkey"


def test_a_wrong_address_does_not_pass_for_a_public_key():
    """Опечатка в адресе не должна кончиться носителем с мусором вместо ключа:
    такой носитель не проверит ни одного ответа, и это выяснится не сразу."""

    def opener(request, timeout=None):
        return _Reply(200, b"<html>It works!</html>")

    with pytest.raises(client.RevocationServerError) as error:
        client.fetch_public_key("https://example.org", opener=opener)
    assert "не ресурс отзыва" in str(error.value)


def test_enrollment_sends_the_admin_public_key_with_the_code():
    seen = {}

    def opener(request, timeout=None):
        seen["request"] = request
        return _Reply(201, b"0123456789abcdef\n")

    said = client.enroll("https://revoke.example.org", SEED, " 2ca0f791 ", opener=opener)

    request = seen["request"]
    assert request.full_url == "https://revoke.example.org/admin/enroll"
    assert request.data == client.public_key_pem(SEED).encode("ascii")
    # Пробелы по краям кода -- обычное дело при переносе из терминала.
    assert request.get_header("X-poa-enrollment") == "2ca0f791"
    assert client.key_id(SEED) in said


def test_a_burnt_code_is_refused_in_plain_words():
    """Код одноразовый, и администратор должен прочитать именно это."""

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(client.RevocationServerError) as error:
        client.enroll("https://revoke.example.org", SEED, "2ca0f791", opener=opener)
    assert "одноразов" in str(error.value)


def test_an_empty_code_is_refused_before_anything_is_sent():
    opener, seen = _capture(201)
    with pytest.raises(client.RevocationServerError):
        client.enroll("https://revoke.example.org", SEED, "   ", opener=opener)
    assert "request" not in seen


def test_the_command_names_one_medium_and_is_signed():
    """Команда всегда про один носитель: сервер по ней узнаёт и владельца, и
    что именно менять."""
    from cryptography.hazmat.primitives import serialization

    opener, seen = _capture(204)
    client.publish_state("https://revoke.example.org/", SEED, "cafe0001", "revoked", 5,
                         opener=opener)

    request = seen["request"]
    assert request.full_url == "https://revoke.example.org/admin/media/cafe0001"
    assert request.get_method() == "POST"
    assert request.data == b"poa-media cafe0001 5 revoked"

    # Подпись проверяется тем же ключом, чью открытую часть получает сервер.
    public = serialization.load_pem_public_key(client.public_key_pem(SEED).encode("ascii"))
    signature = base64.b64decode(request.get_header("X-poa-signature"))
    public.verify(signature, request.data)  # не бросает -- значит сходится


def test_the_key_id_is_the_same_one_the_server_stores():
    """Отпечаток считается по сырым байтам открытой части — как на сервере.

    Разойдись эти два способа, и администратор не смог бы распорядиться
    собственным носителем: сервер считал бы владельцем кого-то другого.
    """
    import hashlib

    from cryptography.hazmat.primitives import serialization

    raw = client.signing_key(SEED).public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    assert client.key_id(SEED) == hashlib.sha256(raw).hexdigest()[:16]
    assert len(client.key_id(SEED)) == 16


def test_the_first_command_about_a_medium_reads_as_registration():
    opener, _ = _capture(201)
    said = client.publish_state("https://revoke.example.org", SEED, "cafe0002", "ok", 1,
                                opener=opener)
    assert "заведён" in said


def test_a_foreign_medium_is_refused_in_plain_words():
    """403 — это «носитель не ваш», и администратор должен прочитать именно
    это, а не код ошибки."""

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    with pytest.raises(client.RevocationServerError) as error:
        client.publish_state("https://revoke.example.org", SEED, "cafe0003", "revoked", 2,
                             opener=opener)
    assert "владельцем" in str(error.value)


def test_a_stale_counter_is_refused_in_plain_words():
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 409, "Conflict", {}, None)

    with pytest.raises(client.RevocationServerError) as error:
        client.publish_state("https://revoke.example.org", SEED, "cafe0004", "ok", 1,
                             opener=opener)
    assert "устарел" in str(error.value)


def test_an_unreachable_server_is_not_a_crash():
    """Недоступный ресурс — обычное дело, и это не повод показывать
    администратору трассировку."""

    def opener(request, timeout=None):
        raise OSError("нет маршрута до узла")

    with pytest.raises(client.RevocationServerError) as error:
        client.publish_state("https://revoke.example.org", SEED, "cafe0005", "ok", 1,
                             opener=opener)
    assert "недоступен" in str(error.value)


DEVICE_KEY = base64.b64encode(b"\x11" * 32).decode("ascii")


def test_the_device_key_goes_as_the_fifth_field():
    """Без ключа носителя сервер новую запись не заводит."""
    opener, seen = _capture(201)
    client.publish_state("https://revoke.example.org", SEED, "cafe0001", "ok", 1,
                         device_public_key_b64=DEVICE_KEY, opener=opener)
    assert seen["request"].data == f"poa-media cafe0001 1 ok {DEVICE_KEY}".encode("ascii")


def test_a_medium_without_a_key_that_the_server_does_not_know_is_explained():
    """Носитель до 1.1.0: администратор должен прочитать «перезапишите», а не 400."""

    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, None)

    with pytest.raises(client.RevocationServerError) as error:
        client.publish_state("https://revoke.example.org", SEED, "cafe0001", "ok", 1,
                             opener=opener)
    assert "до 1.1.0" in str(error.value)


def test_a_broken_device_key_is_refused_before_anything_is_sent():
    opener, seen = _capture(201)
    with pytest.raises(client.RevocationServerError):
        client.publish_state("https://revoke.example.org", SEED, "cafe0001", "ok", 1,
                             device_public_key_b64="bm90IGEga2V5", opener=opener)
    assert "request" not in seen


def test_the_query_is_signed_with_the_time():
    from cryptography.hazmat.primitives import serialization

    seen = {}

    def opener(request, timeout=None):
        seen["request"] = request
        return _Reply(200, b"poa-record cafe0001 ok 2026-09-15T10:00:00+00:00 1.1.0 ab12cd34\n")

    status = client.query_media("https://revoke.example.org/", SEED, "cafe0001", opener=opener)

    assert status == client.MediaStatus("ok", "2026-09-15T10:00:00+00:00", "1.1.0", "ab12cd34")
    request = seen["request"]
    assert request.full_url == "https://revoke.example.org/admin/media/cafe0001"
    assert request.get_method() == "GET"
    assert request.get_header("X-poa-key-id") == client.key_id(SEED)
    moment = request.get_header("X-poa-time")
    public = serialization.load_pem_public_key(client.public_key_pem(SEED).encode("ascii"))
    public.verify(base64.b64decode(request.get_header("X-poa-signature")),
                  f"poa-query cafe0001 {moment}".encode("ascii"))


def test_a_medium_that_never_called_reads_as_empty_fields():
    def opener(request, timeout=None):
        return _Reply(200, b"poa-record cafe0001 revoked - - -\n")

    assert client.query_media("https://r.example.org", SEED, "cafe0001", opener=opener) == \
        client.MediaStatus("revoked")


@pytest.mark.parametrize("code, words", [(403, "владелец"), (404, "не заведён")])
def test_query_refusals_are_explained(code, words):
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "", {}, None)

    with pytest.raises(client.RevocationServerError) as error:
        client.query_media("https://r.example.org", SEED, "cafe0001", opener=opener)
    assert words in str(error.value)


def test_a_foreign_answer_to_the_query_is_not_trusted():
    def opener(request, timeout=None):
        return _Reply(200, b"<html>It works!</html>")

    with pytest.raises(client.RevocationServerError):
        client.query_media("https://r.example.org", SEED, "cafe0001", opener=opener)


def test_unknown_states_are_refused_before_anything_is_sent():
    """Опечатка в состоянии не должна доехать до сервера подписанной."""
    opener, seen = _capture(204)
    with pytest.raises(client.RevocationServerError):
        client.publish_state("https://revoke.example.org", SEED, "cafe0006", "отозван", 1,
                             opener=opener)
    assert "request" not in seen
