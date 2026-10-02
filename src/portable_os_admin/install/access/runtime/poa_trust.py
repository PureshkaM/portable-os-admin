"""Цепочка доверия носителя (LIVE-2.0, этап Э18): корень → рабочий ключ → документ.

Самодостаточный модуль: им пользуются и программа администратора (выпуск), и
носитель (проверка). Формат один, поэтому и код один.

* **Корневой ключ организации** (Ed25519) -- офлайн, в сейфе. Его открытая
  часть закреплена в образе и на ESP. Он подписывает только **заверения**
  рабочих ключей и **отзыв** заверений.
* **Рабочий ключ администратора** подписывает выпуски образа и описатели
  сервера. К каждой подписи прикладывается его заверение.
* **Описатель сервера** -- адреса, отпечатки TLS, ключи сервера; принимается,
  только если подписан заверенным и не отозванным рабочим ключом и его номер
  больше сохранённого.

Подписывается каноническая форма JSON -- как во всём протоколе v3.
"""
from __future__ import annotations

import base64
import json
import time

from cryptography.hazmat.primitives.asymmetric import ed25519


class TrustError(Exception):
    """Документ не прошёл проверку цепочки."""


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def raw_public(key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) \
        if hasattr(key, "public_key") else key.public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)


def _verify(public_b64: str, signature_b64: str, payload) -> None:
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(
            base64.b64decode(signature_b64), canonical(payload))
    except Exception as exc:
        raise TrustError("подпись не сходится") from exc


# -- выпуск (машина администратора) ----------------------------------------------
def certify(root_key, working_public: bytes, not_after: int, serial: int) -> dict:
    """Заверение рабочего ключа корнем."""
    body = {"kind": "poa-admin-key", "key": b64(working_public), "not_after": int(not_after),
            "serial": int(serial)}
    return {"cert": body, "signature": b64(root_key.sign(canonical(body)))}


def revocation_list(root_key, revoked_keys: list[bytes], serial: int) -> dict:
    """Отзыв заверений: перечисленные рабочие ключи больше не принимаются."""
    body = {"kind": "poa-revoked-keys", "keys": sorted(b64(k) for k in revoked_keys),
            "serial": int(serial)}
    return {"list": body, "signature": b64(root_key.sign(canonical(body)))}


def sign_document(working_key, certificate: dict, name: str, payload: dict) -> dict:
    """Документ (описатель, выпуск), подписанный рабочим ключом с заверением."""
    return {name: payload, "signature": b64(working_key.sign(canonical(payload))),
            "signer": certificate}


# -- проверка (носитель и программа) ----------------------------------------------
def accepted_key(root_public_b64: str, certificate: dict, revoked: dict | None = None,
                 now: float | None = None) -> str:
    """Открытый рабочий ключ из заверения -- если корень его заверил, срок не
    истёк и он не отозван. Иначе TrustError."""
    body = certificate.get("cert")
    if not isinstance(body, dict) or body.get("kind") != "poa-admin-key":
        raise TrustError("это не заверение рабочего ключа")
    _verify(root_public_b64, certificate.get("signature", ""), body)
    if (now if now is not None else time.time()) > body["not_after"]:
        raise TrustError("срок заверения рабочего ключа истёк")
    if revoked:
        rbody = revoked.get("list")
        _verify(root_public_b64, revoked.get("signature", ""), rbody)
        if body["key"] in rbody.get("keys", []):
            raise TrustError("рабочий ключ отозван")
    return body["key"]


def verify_document(root_public_b64: str, document: dict, name: str,
                    revoked: dict | None = None, now: float | None = None) -> dict:
    """Содержимое документа после проверки всей цепочки."""
    key = accepted_key(root_public_b64, document.get("signer") or {}, revoked, now)
    payload = document.get(name)
    if not isinstance(payload, dict):
        raise TrustError(f"в документе нет поля {name}")
    _verify(key, document.get("signature", ""), payload)
    return payload


def newer_descriptor(root_public_b64: str, candidate: dict, current: dict | None,
                     revoked: dict | None = None) -> dict | None:
    """Описатель, если его стоит принять вместо текущего: подлинный и с
    большим номером. Старый подлинный (откат на перехваченный адрес) -- None."""
    payload = verify_document(root_public_b64, candidate, "descriptor", revoked)
    if current is not None and payload.get("serial", 0) <= current.get("serial", 0):
        return None
    return payload
