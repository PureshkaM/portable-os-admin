"""Служебные действия владельца сервера: ключи v3 и комплект шлюза.

    python3 -m poa_access.tool init-keys <каталог ключей> <организация>
    python3 -m poa_access.tool gateway-kit <каталог ключей> <имя шлюза> <каталог комплекта>

`init-keys` создаёт только недостающее и ничего не перезаписывает: новый CA
сделал бы недействительным доверие шлюза, новый ключ данных -- нечитаемыми
семена TOTP в базе. Запускается сценарием установки.

`gateway-kit` собирает то, что нужно OpenVPN-шлюзу компании: сертификат CA,
ключ и сертификат шлюза, серверный ключ `tls-crypt-v2`. Комплект содержит
секреты; переносится на шлюз один раз и удаляется.
"""
from __future__ import annotations

import os
import pathlib
import sys

from cryptography.hazmat.primitives import serialization

from . import ca, secrets_store, tlscrypt


def _write(path: pathlib.Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def init_keys(directory: pathlib.Path, org: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / secrets_store.CA_KEY).exists():
        key_pem, cert_pem = ca.new_ca(org)
        _write(directory / secrets_store.CA_KEY, key_pem)
        _write(directory / secrets_store.CA_CERT, cert_pem)
        print("создан CA для VPN")
    if not (directory / secrets_store.DATA_KEY).exists():
        _write(directory / secrets_store.DATA_KEY, os.urandom(32))
        print("создан ключ данных")
    if not (directory / secrets_store.TLS_CRYPT_V2).exists():
        if tlscrypt.available():
            _write(directory / secrets_store.TLS_CRYPT_V2, tlscrypt.new_server_key())
            print("создан серверный ключ tls-crypt-v2")
        else:
            print("openvpn не найден: ключ tls-crypt-v2 не создан", file=sys.stderr)


def gateway_kit(directory: pathlib.Path, name: str, out: pathlib.Path) -> None:
    ca_key = serialization.load_pem_private_key(
        (directory / secrets_store.CA_KEY).read_bytes(), password=None)
    ca_cert = (directory / secrets_store.CA_CERT).read_bytes()
    key_pem, cert_pem = ca.issue_server(ca_key, ca_cert, name)
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    _write(out / "ca.crt", ca_cert)
    _write(out / "gateway.key", key_pem)
    _write(out / "gateway.crt", cert_pem)
    tc2 = directory / secrets_store.TLS_CRYPT_V2
    if tc2.exists():
        _write(out / "tls-crypt-v2-server.key", tc2.read_bytes())
    _write(out / "name", name.encode() + b"\n")
    print(f"комплект шлюза {name}: {out}")


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "init-keys":
        init_keys(pathlib.Path(argv[1]), argv[2])
        return 0
    if len(argv) == 4 and argv[0] == "gateway-kit":
        gateway_kit(pathlib.Path(argv[1]), argv[2], pathlib.Path(argv[3]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
