"""Цепочка доверия носителя (LIVE-2.0, этап Э18): корни → рабочие ключи → документы.

Самодостаточный модуль: им пользуются и программа администратора (выпуск), и
носитель (проверка). Формат один, поэтому и код один. Схема согласована
02.10.2026 (`docs/LIVE-2.0.md`, «Корень организации»).

* **Корни организации** -- два ключа ГОСТ Р 34.10-2012: основной на токене,
  резервный в зашифрованном файле. Их открытые части -- **якоря**: лежат на
  ESP и в хранилище LUKS носителя, но не в образе (образ подписан ключом
  администратора и не может нести собственный якорь). Корень подписывает
  только сертификаты рабочих ключей, их отзыв и замену другого корня.
* **Замена корня -- асимметрична**: резервный корень (сейф, файл и фраза
  раздельно) может заменить основной; основной (токен, в работе чаще)
  резервный -- не может. Симметричное правило «заменяет другой» позволило бы
  вору основного заменить резервный на свой и закрепиться навсегда (найдено
  тестом 02.10.2026). Утерянный резервный восстанавливается перезаписью
  якорей носителя -- редкой плановой процедурой.
* **Рабочий ключ администратора** (Ed25519) подписывает выпуски образа и
  описатели сервера; к подписи прикладывается его сертификат.

Подписывается каноническая форма JSON -- как во всём протоколе v3. Алгоритм
указан у каждого ключа и подписи: корень можно будет перевести на другой
(в том числе постквантовый), не меняя формата.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

try:  # на носителе модули лежат рядом, в программе -- это пакет
    import poa_gost
except ImportError:  # pragma: no cover - зависит от способа импорта
    from . import poa_gost

ALG_GOST = "gost2012-256-A"
ALG_ED25519 = "ed25519"
ROLE_MAIN = "main"
ROLE_RESERVE = "reserve"


class TrustError(Exception):
    """Документ не прошёл проверку цепочки."""


def canonical(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def raw_public(key) -> bytes:
    """Сырые 32 байта открытого ключа Ed25519 (из закрытого или открытого)."""
    public = key.public_key() if hasattr(key, "public_key") else key
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id(entry: dict) -> str:
    """Короткое имя якоря: первые 16 знаков SHA-256 от алгоритма и ключа.
    Его показывают людям для сверки и пишут в поле issuer."""
    return hashlib.sha256(f"{entry['alg']}:{entry['key']}".encode()).hexdigest()[:16]


# -- подписанты ------------------------------------------------------------------
class GostSigner:
    """Корень, ключ которого в памяти (резервный файл). Токен -- свой подписант
    с тем же интерфейсом: `alg`, `entry`, `sign(data)`."""

    alg = ALG_GOST

    def __init__(self, d: int, role: str = ROLE_RESERVE) -> None:
        self._d = d
        public = poa_gost.public_key(poa_gost.TC26_256_A, d)
        self.entry = {"alg": ALG_GOST, "key": b64(poa_gost.encode_public(public)), "role": role}

    def sign(self, data: bytes) -> bytes:
        return poa_gost.sign(poa_gost.TC26_256_A, self._d, data)


def _verify(entry: dict, signature_b64: str, payload) -> None:
    try:
        signature = base64.b64decode(signature_b64)
        raw = base64.b64decode(entry["key"])
        if entry["alg"] == ALG_GOST:
            ok = poa_gost.verify(poa_gost.TC26_256_A, poa_gost.decode_public(raw),
                                 canonical(payload), signature)
        elif entry["alg"] == ALG_ED25519:
            ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(signature, canonical(payload))
            ok = True
        else:
            ok = False
    except Exception as exc:
        raise TrustError("подпись не сходится") from exc
    if not ok:
        raise TrustError("подпись не сходится")


def _signed(signer, body: dict, name: str) -> dict:
    return {name: body, "issuer": key_id(signer.entry), "signature": b64(signer.sign(canonical(body)))}


def _issuer(anchors: list[dict], document: dict) -> dict:
    for entry in anchors:
        if key_id(entry) == document.get("issuer"):
            return entry
    raise TrustError("документ подписан не корнем этой организации")


# -- выпуск (машина администратора) ----------------------------------------------
def certify(root, working_public: bytes, not_after: int, serial: int, name: str = "") -> dict:
    """Сертификат рабочего ключа администратора, подписанный корнем."""
    body = {"kind": "poa-admin-key", "key": b64(working_public), "alg": ALG_ED25519,
            "name": name, "not_after": int(not_after), "serial": int(serial)}
    return _signed(root, body, "cert")


def revocation_list(root, revoked_keys: list[bytes], serial: int) -> dict:
    """Отзыв сертификатов: перечисленные рабочие ключи больше не принимаются."""
    body = {"kind": "poa-revoked-keys", "keys": sorted(b64(k) for k in revoked_keys),
            "serial": int(serial)}
    return _signed(root, body, "list")


def root_replacement(reserve_root, old_main: dict, new_main: dict, serial: int) -> dict:
    """Замена основного корня новым -- подписывает только резервный."""
    if reserve_root.entry.get("role") != ROLE_RESERVE or old_main.get("role") != ROLE_MAIN:
        raise TrustError("заменить можно только основной корень, и только резервным")
    body = {"kind": "poa-root-replace", "old": old_main,
            "new": {**new_main, "role": ROLE_MAIN}, "serial": int(serial)}
    return _signed(reserve_root, body, "replace")


def sign_document(working_key, certificate: dict, name: str, payload: dict) -> dict:
    """Документ (описатель, выпуск), подписанный рабочим ключом с сертификатом."""
    return {name: payload, "signature": b64(working_key.sign(canonical(payload))),
            "signer": certificate}


# -- проверка (носитель и программа) ----------------------------------------------
def accepted_key(anchors: list[dict], certificate: dict, revoked: dict | None = None,
                 now: float | None = None) -> str:
    """Открытый рабочий ключ из сертификата -- если его выдал один из корней,
    срок не истёк и он не отозван. Иначе TrustError."""
    body = certificate.get("cert")
    if not isinstance(body, dict) or body.get("kind") != "poa-admin-key":
        raise TrustError("это не сертификат рабочего ключа")
    _verify(_issuer(anchors, certificate), certificate.get("signature", ""), body)
    if (now if now is not None else time.time()) > body["not_after"]:
        raise TrustError("срок сертификата рабочего ключа истёк")
    if revoked:
        rbody = revoked.get("list")
        _verify(_issuer(anchors, revoked), revoked.get("signature", ""), rbody)
        if body["key"] in rbody.get("keys", []):
            raise TrustError("рабочий ключ отозван")
    return body["key"]


def check_revocation_list(anchors: list[dict], document: dict) -> dict:
    """Тело списка отзыва, если его подписал один из корней. Иначе TrustError."""
    body = document.get("list")
    if not isinstance(body, dict) or body.get("kind") != "poa-revoked-keys":
        raise TrustError("это не список отзыва")
    _verify(_issuer(anchors, document), document.get("signature", ""), body)
    return body


def verify_document(anchors: list[dict], document: dict, name: str,
                    revoked: dict | None = None, now: float | None = None) -> dict:
    """Содержимое документа после проверки всей цепочки."""
    key = accepted_key(anchors, document.get("signer") or {}, revoked, now)
    payload = document.get(name)
    if not isinstance(payload, dict):
        raise TrustError(f"в документе нет поля {name}")
    _verify({"alg": ALG_ED25519, "key": key}, document.get("signature", ""), payload)
    return payload


def newer_descriptor(anchors: list[dict], candidate: dict, current: dict | None,
                     revoked: dict | None = None) -> dict | None:
    """Описатель, если его стоит принять вместо текущего: подлинный и с
    большим номером. Старый подлинный (откат на перехваченный адрес) -- None."""
    payload = verify_document(anchors, candidate, "descriptor", revoked)
    if current is not None and payload.get("serial", 0) <= current.get("serial", 0):
        return None
    return payload


def apply_root_replacement(anchors: list[dict], document: dict) -> list[dict]:
    """Новые якоря после замены корня. Подписать замену может только якорь,
    отличный от заменяемого; заменяемый обязан быть среди якорей."""
    body = document.get("replace")
    if not isinstance(body, dict) or body.get("kind") != "poa-root-replace":
        raise TrustError("это не замена корня")
    signer = _issuer(anchors, document)
    old, new = body["old"], body["new"]
    known = {key_id(a): a for a in anchors}
    if signer.get("role") != ROLE_RESERVE:
        raise TrustError("заменить корень может только резервный корень")
    if key_id(old) not in known or known[key_id(old)].get("role") != ROLE_MAIN:
        raise TrustError("заменяемого основного корня среди якорей нет")
    _verify(signer, document.get("signature", ""), body)
    return [a for a in anchors if key_id(a) != key_id(old)] + [{**new, "role": ROLE_MAIN}]
