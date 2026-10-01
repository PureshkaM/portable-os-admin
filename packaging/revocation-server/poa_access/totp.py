"""Коды TOTP по RFC 6238: SHA-1, 6 цифр, шаг 30 секунд.

Параметры не настраиваются: их понимает любое приложение-аутентификатор, а
выигрыш от SHA-256 или 8 цифр съедается тем, что часть приложений их не умеет.
Окно -- ±1 шаг на расхождение часов телефона. Повтор отсекает `last_step`:
принимается только шаг строго больше последнего принятого, поэтому один и тот
же код не проходит дважды даже внутри своего окна.
"""
from __future__ import annotations

import base64
import hmac
import secrets
import struct
import time

DIGITS = 6
PERIOD = 30
WINDOW = 1


def new_secret() -> str:
    """Семя: 20 случайных байт в base32 без выравнивания, как его ждёт QR."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_decode(secret), struct.pack(">Q", step), "sha1").digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def accept(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """Шаг, которому соответствует `code`, или None.

    None -- код неверен либо его шаг не больше `last_step` (повтор).
    """
    if not (isinstance(code, str) and len(code) == DIGITS and code.isascii() and code.isdigit()):
        return None
    current = int((time.time() if now is None else now) // PERIOD)
    for step in range(current - WINDOW, current + WINDOW + 1):
        if step > last_step and hmac.compare_digest(code_at(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, label: str, issuer: str) -> str:
    """Строка для QR-кода. Показывается только на экране самого носителя."""
    from urllib.parse import quote

    return (f"otpauth://totp/{quote(issuer)}:{quote(label)}?secret={secret}"
            f"&issuer={quote(issuer)}&digits={DIGITS}&period={PERIOD}&algorithm=SHA1")
