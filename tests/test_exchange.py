"""Передача конфигураций между администраторами.

Конфигурации живут внутри контейнера, и вынести их наружу можно только так:
зашифрованными ключом получателя и подписанными ключом отправителя. Здесь
проверяется не только то, что посылка доходит, но и то, что не доходит
подделанная, чужая и переадресованная.
"""
from __future__ import annotations

import base64
import json

import pytest

from portable_os_admin.config import exchange
from portable_os_admin.container import ContainerManager


@pytest.fixture
def two_admins(tmp_path):
    """Два контейнера: отправитель и получатель."""
    manager = ContainerManager()
    sender = manager.create(tmp_path / "sender.container", "CorrectHorse123")
    recipient = manager.create(tmp_path / "recipient.container", "CorrectHorse456")
    yield sender, recipient
    sender.close()
    recipient.close()


def _config(name="stick1"):
    return (name, {"name": name, "target_os": "debian", "packages": ["mate-calc"]})


def test_a_configuration_travels_from_one_admin_to_another(two_admins):
    """Круг замыкается: что отправили, то и приняли, и видно, от кого."""
    sender, recipient = two_admins

    blob = exchange.pack(sender, [_config()], exchange.identity_of(recipient).code)
    who, configs = exchange.unpack(recipient, blob)

    assert [name for name, _ in configs] == ["stick1"]
    assert configs[0][1]["packages"] == ["mate-calc"]
    assert who.code == exchange.identity_of(sender).code


def test_all_configurations_go_in_one_parcel(two_admins):
    """Экспорт всех -- та же посылка, просто с несколькими конфигурациями."""
    sender, recipient = two_admins
    payload = [_config("stick1"), _config("stick2"), _config("stick3")]

    blob = exchange.pack(sender, payload, exchange.identity_of(recipient).code)
    _, configs = exchange.unpack(recipient, blob)

    assert [name for name, _ in configs] == ["stick1", "stick2", "stick3"]


def test_the_parcel_says_nothing_in_the_clear(two_admins):
    """В файле посылки нет ни имени конфигурации, ни её содержимого.

    Проверка смотрит в байты файла, а не на структуру: посылка идёт почтой и
    через чужие руки, и единственное, что о ней должно быть видно, -- кому и
    от кого.
    """
    sender, recipient = two_admins
    named = ("буxгалтерия", {"name": "буxгалтерия", "account": {"username": "buhgalter"}})

    blob = exchange.pack(sender, [named], exchange.identity_of(recipient).code)

    assert b"buhgalter" not in blob
    assert "буxгалтерия".encode() not in blob
    assert exchange.EXCHANGE_FORMAT.encode() in blob


def test_a_parcel_for_someone_else_is_refused(two_admins, tmp_path):
    """Чужая посылка отвергается по адресату, а не по неудачной расшифровке.

    Разница видна администратору: «это не мне» и «это подделали» -- разные
    события, и первое не должно выглядеть как второе.
    """
    sender, recipient = two_admins
    stranger = ContainerManager().create(tmp_path / "stranger.container", "CorrectHorse789")

    blob = exchange.pack(sender, [_config()], exchange.identity_of(recipient).code)

    with pytest.raises(exchange.ExchangeError, match="другому администратору"):
        exchange.unpack(stranger, blob)
    stranger.close()


def test_a_forged_signature_is_refused(two_admins, tmp_path):
    """Кто угодно знает публичный код -- и мог бы прислать от чужого имени.

    Здесь посторонний шифрует настоящей посылкой для настоящего получателя,
    но подписывает своим ключом, а в заголовке называет чужой код. Приём
    обязан это заметить: по присланной конфигурации собирают носитель.
    """
    sender, recipient = two_admins
    impostor = ContainerManager().create(tmp_path / "impostor.container", "CorrectHorse789")

    blob = exchange.pack(impostor, [_config()], exchange.identity_of(recipient).code)
    envelope = json.loads(blob)
    envelope["sender"] = exchange.identity_of(sender).code

    with pytest.raises(exchange.ExchangeError, match="подпись"):
        exchange.unpack(recipient, json.dumps(envelope).encode())
    impostor.close()


def test_a_tampered_parcel_is_refused(two_admins):
    """Изменённый шифртекст не проходит: подпись покрывает и его."""
    sender, recipient = two_admins

    blob = exchange.pack(sender, [_config()], exchange.identity_of(recipient).code)
    envelope = json.loads(blob)
    raw = bytearray(base64.b64decode(envelope["ciphertext"]))
    raw[0] ^= 0x01
    envelope["ciphertext"] = base64.b64encode(bytes(raw)).decode()

    with pytest.raises(exchange.ExchangeError):
        exchange.unpack(recipient, json.dumps(envelope).encode())


def test_a_mistyped_code_is_caught_by_its_checksum():
    """Опечатка в коде ловится до всякой криптографии.

    Код длинный, его переписывают руками, и ошибка в одном знаке иначе
    обернулась бы посылкой, которую получатель не сможет открыть, -- причём
    выяснилось бы это у него, а не у отправителя.
    """
    manager = ContainerManager()
    import tempfile, pathlib

    tmp = pathlib.Path(tempfile.mkdtemp())
    container = manager.create(tmp / "admin.container", "CorrectHorse123")
    code = exchange.identity_of(container).code

    broken = list(code.replace("-", ""))
    broken[5] = "0" if broken[5] != "0" else "1"
    with pytest.raises(exchange.ExchangeError, match="контрольной суммой"):
        exchange.AdminIdentity.from_code("".join(broken))
    container.close()


def test_the_code_survives_being_retyped_by_hand():
    """Пробелы, регистр и похожие знаки не должны ломать код.

    Те же поблажки, что у кодов восстановления: человек, переписывающий код с
    экрана, ставит пробелы где придётся и путает `O` с нулём.
    """
    import pathlib
    import tempfile

    tmp = pathlib.Path(tempfile.mkdtemp())
    container = ContainerManager().create(tmp / "admin.container", "CorrectHorse123")
    identity = exchange.identity_of(container)

    retyped = identity.code.lower().replace("-", " ")
    assert exchange.AdminIdentity.from_code(retyped).code == identity.code
    container.close()


def test_the_same_container_always_shows_the_same_code(tmp_path):
    """Код не меняется от запуска к запуску: он выведен из семени в контейнере.

    Иначе коллега, записавший код вчера, сегодня прислал бы посылку, которую
    невозможно открыть.
    """
    manager = ContainerManager()
    path = tmp_path / "admin.container"
    first = manager.create(path, "CorrectHorse123")
    code = exchange.identity_of(first).code
    manager.save(first)
    first.close()

    reopened = manager.open(path, "CorrectHorse123")
    assert exchange.identity_of(reopened).code == code
    reopened.close()


def test_an_empty_parcel_is_refused(two_admins):
    sender, recipient = two_admins
    with pytest.raises(exchange.ExchangeError, match="нечего отправлять"):
        exchange.pack(sender, [], exchange.identity_of(recipient).code)


def test_a_foreign_file_is_refused_by_format(two_admins):
    _, recipient = two_admins
    with pytest.raises(exchange.ExchangeError, match="не является посылкой"):
        exchange.unpack(recipient, json.dumps({"format": "something-else"}).encode())
