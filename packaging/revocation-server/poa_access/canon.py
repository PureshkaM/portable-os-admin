"""Канонический JSON и подписи поверх него.

Подписывается не «тот JSON, что пришёл», а его каноническая форма: иначе
порядок ключей или пробелы у отправителя и получателя разошлись бы, и подпись
одного и того же содержания не сходилась бы. Правило одно на все стороны и
записано в `docs/ACCESS-PROTOCOL.md`, раздел 4.
"""
from __future__ import annotations

import base64
import hashlib
import json


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signed(payload_name: str, payload: dict, key) -> dict:
    """`{payload_name: payload, "signature": ...}` -- подпись канонической формы."""
    return {payload_name: payload, "signature": b64(key.sign(canonical(payload)))}


def device_message(method: str, path: str, media: str, nonce: str, body: bytes) -> bytes:
    """Строка, которую подписывает носитель (раздел 3 протокола)."""
    return "\n".join(("poa-v3", f"{method} {path}", media, nonce, sha256_hex(body))).encode("utf-8")
