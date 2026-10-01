"""Клиентские ключи `tls-crypt-v2` на каждый сеанс.

Формат ключа -- дело OpenVPN, и переписывать его здесь значило бы завести свою
реализацию чужой криптографии. Поэтому ключ делает сам `openvpn --genkey
tls-crypt-v2-client`. Серверный ключ отдаётся ему через memfd: на диск он не
попадает (после Э21 он и не лежит на диске в открытом виде).
"""
from __future__ import annotations

import os
import shutil
import subprocess

OPENVPN = shutil.which("openvpn") or "/usr/sbin/openvpn"


def available() -> bool:
    return os.path.exists(OPENVPN)


def _memfd(name: str, data: bytes) -> int:
    fd = os.memfd_create(name, 0)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def new_server_key() -> bytes:
    """Серверный ключ -- для сценария установки и комплекта шлюза."""
    out = _memfd("poa-tc2-server", b"")
    try:
        subprocess.run([OPENVPN, "--genkey", "tls-crypt-v2-server", f"/proc/self/fd/{out}"],
                       check=True, capture_output=True, timeout=30, pass_fds=(out,))
        os.lseek(out, 0, os.SEEK_SET)
        return os.read(out, 1 << 16)
    finally:
        os.close(out)


def client_key(server_key: bytes) -> bytes:
    """Новый клиентский ключ, обёрнутый серверным."""
    src = _memfd("poa-tc2-in", server_key)
    out = _memfd("poa-tc2-out", b"")
    try:
        subprocess.run(
            [OPENVPN, "--tls-crypt-v2", f"/proc/self/fd/{src}",
             "--genkey", "tls-crypt-v2-client", f"/proc/self/fd/{out}"],
            check=True, capture_output=True, timeout=30, pass_fds=(src, out),
        )
        os.lseek(out, 0, os.SEEK_SET)
        return os.read(out, 1 << 16)
    finally:
        os.close(src)
        os.close(out)
