"""Обработчики протокола v3. Договор -- `docs/ACCESS-PROTOCOL.md`.

Функции принимают обработчик HTTP (`Handler` из `poa-revocation-server.py`) и
пользуются его атрибутами класса: `store`, `secrets`, `state_dir`, `admins`.
Так v3 добавляется к v2, не переписывая его.

Порядок каждого шага носителя один и тот же: подпись проверяется вне
транзакции (дорого и ничего не меняет), а всё, что меняет состояние, --
nonce, TOTP, звено, сертификаты -- внутри одной транзакции `store.tx()`.
Отказ внутри транзакции не откатывает её: погашенный nonce и счётчик неудач
TOTP должны сохраниться, иначе перебор шёл бы бесплатно.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import pathlib
import re
import secrets
import time

from cryptography.hazmat.primitives.asymmetric import ed25519

from . import ca, canon, catalog, tlscrypt, totp
from .store import ROOT_GROUP, Store

MEDIA_RE = re.compile(r"\A[0-9a-f]{8}(?:-[0-9a-f]{8})?\Z")
NONCE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
HEX64_RE = re.compile(r"\A[0-9a-f]{64}\Z")
NONCE_TTL = 120
#: Ответ на пробу портала (`GET /v3/probe`); носитель сверяет его побайтно.
PROBE_BODY = b"poa-probe 1\n"
#: Неудач TOTP подряд, после которых носитель блокируется до администратора.
#: Перебор шести цифр онлайн с таким лимитом бессмыслен, а пользователь,
#: ошибившийся десять раз подряд, всё равно звонит администратору.
TOTP_FAIL_LIMIT = 10
BODY_LIMIT = 64 * 1024
EVENTS_LIMIT = 256 * 1024
TARGET_TYPES = ("rdp", "vnc", "ssh")

FORBIDDEN = {"error": "forbidden"}


class Refused(Exception):
    """Отказ шага с кодом и телом ответа; транзакция при этом фиксируется."""

    def __init__(self, code: int, body: dict) -> None:
        super().__init__(body.get("error"))
        self.code, self.body = code, body


# ---------------------------------------------------------------------------
# общее
# ---------------------------------------------------------------------------
def send_json(h, code: int, obj: dict) -> None:
    h._send(code, canon.canonical(obj), content_type="application/json", no_store=True)


def read_body(h, limit: int = BODY_LIMIT) -> bytes:
    raw = (h.headers.get("Content-Length") or "").strip()
    if not raw.isascii() or not raw.isdigit() or not 0 <= int(raw) <= limit:
        raise Refused(400, {"error": "bad_length"})
    return h.rfile.read(int(raw)) if int(raw) else b""


def parse_json(body: bytes) -> dict:
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Refused(400, {"error": "bad_json"}) from None
    if not isinstance(obj, dict):
        raise Refused(400, {"error": "bad_json"})
    return obj


def link_hash(link_b64: str) -> str:
    try:
        raw = canon.unb64(link_b64)
    except (binascii.Error, ValueError, TypeError):
        raise Refused(403, FORBIDDEN) from None
    if len(raw) != 32:
        raise Refused(403, FORBIDDEN)
    return hashlib.sha256(raw).hexdigest()


def new_link() -> tuple[str, str]:
    raw = secrets.token_bytes(32)
    return canon.b64(raw), hashlib.sha256(raw).hexdigest()


def device_key_of(record):
    if record is None or not record["device_key"]:
        return None
    if (record["alg"] or "ed25519") != "ed25519":
        return None  # другие алгоритмы -- вместе с токеном
    try:
        raw = base64.b64decode(record["device_key"], validate=True)
        return ed25519.Ed25519PublicKey.from_public_bytes(raw) if len(raw) == 32 else None
    except (binascii.Error, ValueError):
        return None


def authenticate(h, path: str, limit: int = BODY_LIMIT) -> tuple[str, str, dict]:
    """(носитель, nonce, тело) после проверки подписи. Nonce гасит вызывающий."""
    media = (h.headers.get("X-POA-Media") or "").strip()
    nonce = (h.headers.get("X-POA-Nonce") or "").strip()
    signature = (h.headers.get("X-POA-Signature") or "").strip()
    body = read_body(h, limit)
    if not MEDIA_RE.match(media) or not NONCE_RE.match(nonce):
        raise Refused(403, FORBIDDEN)
    with h.store.tx() as db:
        record = Store.media(db, media)
    key = device_key_of(record)
    if key is None:
        raise Refused(403, FORBIDDEN)
    try:
        key.verify(canon.unb64(signature), canon.device_message("POST", path, media, nonce, body))
    except Exception:
        raise Refused(403, FORBIDDEN) from None
    return media, nonce, parse_json(body)


def begin_step(db, media: str, nonce: str):
    """Общее начало шага в транзакции: nonce, отзыв, блокировка."""
    if not Store.consume_nonce(db, media, nonce):
        raise Refused(403, FORBIDDEN)
    record = Store.media(db, media)
    if record is None or record["state"] != "ok":
        raise Refused(403, FORBIDDEN)
    if record["blocked"]:
        raise Refused(423, {"error": "blocked"})
    return record


def run_step(h, step) -> None:
    """Выполняет шаг в транзакции; отказ фиксирует транзакцию и отвечает."""
    try:
        with h.store.tx() as db:
            try:
                code, body = step(db)
            except Refused as refusal:
                code, body = refusal.code, refusal.body
        if body.get("error") == "clone":
            # После фиксации отзыва, а не внутри транзакции: иначе CRL,
            # запрошенный в этот промежуток, закэшировался бы без отозванных.
            invalidate_crl(h.store)
        send_json(h, code, body)
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)


def signed_grant(h, grant: dict) -> dict:
    grant["server_time"] = int(time.time())
    return canon.signed("grant", grant, h.secrets.sign_key)


# ---------------------------------------------------------------------------
# носитель
# ---------------------------------------------------------------------------
def nonce(h) -> None:
    try:
        obj = parse_json(read_body(h))
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return
    media = obj.get("media")
    if not isinstance(media, str) or not MEDIA_RE.match(media):
        send_json(h, 400, {"error": "bad_media"})
        return
    # Выдаётся и неизвестному носителю: иначе ответ подсказывал бы, какие
    # носители существуют. Ничего, кроме права на одну подпись, nonce не даёт.
    value = secrets.token_hex(16)
    h.store.issue_nonce(media, value, NONCE_TTL)
    send_json(h, 200, {"nonce": value, "ttl": NONCE_TTL})


def _normalized_code(code) -> str:
    if not isinstance(code, str):
        return ""
    return code.replace("-", "").replace(" ", "").upper()


def activation_hash(code: str) -> str:
    return hashlib.sha256(_normalized_code(code).encode("ascii", "replace")).hexdigest()


def activate(h) -> None:
    try:
        media, nonce_value, obj = authenticate(h, "/v3/activate")
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return

    def step(db):
        begin_step(db, media, nonce_value)
        row = db.execute("SELECT * FROM activations WHERE code_hash = ?",
                         (activation_hash(obj.get("code")),)).fetchone()
        if row is None:
            Store.log(db, "activation_unknown_code", media)
            raise Refused(403, FORBIDDEN)
        if row["media_id"] != media:
            Store.log(db, "activation_foreign_code", media, owner=row["media_id"])
            raise Refused(403, FORBIDDEN)
        if row["expires"] < time.time():
            raise Refused(410, {"error": "expired"})
        access = Store.access(db, media)
        if access["activated"]:
            Store.log(db, "activation_repeated", media)
            raise Refused(409, {"error": "already_activated"})
        # До первого подтверждённого входа повтор с тем же кодом разрешён:
        # пользователь мог не дойти до QR. Прежнее L0 уходит в историю -- его
        # предъявление дальше будет клоном.
        if access["link_c"]:
            db.execute("INSERT OR IGNORE INTO link_history (media_id, h) VALUES (?, ?)",
                       (media, access["link_c"]))
        secret = totp.new_secret()
        link, link_h = new_link()
        db.execute("UPDATE access SET totp = ?, last_step = 0, link_c = ?, link_p = NULL,"
                   " fails = 0 WHERE media_id = ?",
                   (h.secrets.seal("totp", media, secret.encode("ascii")), link_h, media))
        db.execute("UPDATE activations SET used_by = ? WHERE code_hash = ?",
                   (media, row["code_hash"]))
        Store.log(db, "activation", media)
        grant = {"totp": {"secret": secret, "digits": totp.DIGITS, "period": totp.PERIOD,
                          "algorithm": "SHA1"},
                 "link": link}
        return 200, signed_grant(h, grant)

    run_step(h, step)


def _release_info(db, record, status: dict) -> tuple[dict | None, int]:
    distribution = Store.effective(db, record["group_id"], "distribution")
    if not distribution:
        return None, 0
    dist = db.execute("SELECT * FROM distributions WHERE id = ?", (distribution,)).fetchone()
    latest = Store.latest_release(db, distribution)
    if dist is None or latest is None:
        return None, 0
    info = {"distribution": distribution, "serial": latest["serial"],
            "min_serial": dist["min_serial"],
            "url": f"/v3/release/{distribution}/{latest['serial']}/",
            "files": sorted(json.loads(latest["files"]))}
    return info, dist["min_serial"]


def session(h) -> None:
    try:
        media, nonce_value, obj = authenticate(h, "/v3/session")
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return
    status = obj.get("status") if isinstance(obj.get("status"), dict) else {}

    def step(db):
        record = begin_step(db, media, nonce_value)
        access = Store.access(db, media)
        if not access["totp"]:
            raise Refused(403, FORBIDDEN)

        virt = status.get("virt", "none")
        if virt not in ("none", "", None) and Store.effective(db, record["group_id"], "allow_vm") != 1:
            Store.log(db, "denied_virtualization", media, virt=str(virt)[:32])
            raise Refused(403, {"error": "virtualization"})

        secret = h.secrets.unseal("totp", media, access["totp"]).decode("ascii")
        step_no = totp.accept(secret, obj.get("totp"), access["last_step"])
        if step_no is None:
            fails = (access["fails"] or 0) + 1
            db.execute("UPDATE access SET fails = ? WHERE media_id = ?", (fails, media))
            Store.log(db, "totp_failed", media, fails=fails)
            if fails >= TOTP_FAIL_LIMIT:
                db.execute("UPDATE media SET blocked = 1 WHERE media_id = ?", (media,))
                Store.log(db, "blocked_totp_limit", media)
            raise Refused(401, {"error": "totp"})
        db.execute("UPDATE access SET last_step = ?, fails = 0 WHERE media_id = ?", (step_no, media))

        release, min_serial = _release_info(db, record, status)
        serial = status.get("serial")
        if release and min_serial and (not isinstance(serial, int) or serial < min_serial):
            Store.log(db, "denied_update_required", media, serial=serial, min_serial=min_serial)
            raise Refused(426, {"error": "update_required", "release": release})

        presented = link_hash(obj.get("link"))
        if presented == access["link_c"]:
            if access["link_p"]:
                db.execute("INSERT OR IGNORE INTO link_history (media_id, h) VALUES (?, ?)",
                           (media, access["link_p"]))
                Store.log(db, "link_anomaly", media)
        elif access["link_p"] and presented == access["link_p"]:
            # Запись удалась, подтверждение потерялось: засчитываем.
            db.execute("INSERT OR IGNORE INTO link_history (media_id, h) VALUES (?, ?)",
                       (media, access["link_c"]))
            db.execute("UPDATE access SET link_c = link_p WHERE media_id = ?", (media,))
        elif db.execute("SELECT 1 FROM link_history WHERE media_id = ? AND h = ?",
                        (media, presented)).fetchone():
            db.execute("UPDATE media SET blocked = 1 WHERE media_id = ?", (media,))
            revoked = Store.revoke_certs_of(db, media)
            Store.log(db, "clone", media, revoked_certs=revoked)
            raise Refused(409, {"error": "clone"})
        else:
            raise Refused(403, FORBIDDEN)

        proposal, proposal_h = new_link()
        db.execute("UPDATE access SET link_p = ? WHERE media_id = ?", (proposal_h, media))
        db.execute("UPDATE media SET last_seen = ?, version = ?, machine = ? WHERE media_id = ?",
                   (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                    str(status.get("version", ""))[:32] or None,
                    str(status.get("machine", ""))[:16] or None, media))
        return 200, {"proposal": proposal, "release": release, "server_time": int(time.time())}

    run_step(h, step)


def _targets(db, media: str) -> list[dict]:
    rows = db.execute("SELECT spec FROM targets WHERE media_id = ? ORDER BY target_id",
                      (media,)).fetchall()
    return [json.loads(r["spec"]) for r in rows]


def _vpn_settings(h) -> dict:
    path = h.state_dir / "vpn.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def confirm(h) -> None:
    try:
        media, nonce_value, obj = authenticate(h, "/v3/session/confirm")
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return
    vpn_request = obj.get("vpn") if isinstance(obj.get("vpn"), dict) else {}

    def step(db):
        begin_step(db, media, nonce_value)
        access = Store.access(db, media)
        presented = link_hash(obj.get("link"))
        if not access["link_p"] or presented != access["link_p"]:
            raise Refused(403, FORBIDDEN)
        provider = vpn_request.get("provider", "openvpn")
        if provider == "openvpn":
            if not h.secrets.v3_ready:
                raise Refused(503, {"error": "not_configured"})
            try:
                public_key = ca.load_client_public_key(vpn_request.get("public_key", ""))
            except Exception:
                raise Refused(400, {"error": "bad_vpn_key"}) from None
        elif provider != "vipnet":
            raise Refused(400, {"error": "bad_provider"})

        db.execute("INSERT OR IGNORE INTO link_history (media_id, h) VALUES (?, ?)",
                   (media, access["link_c"]))
        db.execute("UPDATE access SET link_c = link_p, link_p = NULL, activated = 1"
                   " WHERE media_id = ?", (media,))

        settings = _vpn_settings(h)
        if provider == "openvpn":
            cert, serial, not_after = ca.issue_client(h.secrets.ca_key, h.secrets.ca_cert_pem,
                                                      media, public_key)
            db.execute("INSERT INTO certs (serial, media_id, not_after) VALUES (?, ?, ?)",
                       (str(serial), media, not_after))
            vpn = {"provider": "openvpn", "cert": cert.decode("ascii"),
                   "ca": h.secrets.ca_cert_pem.decode("ascii"),
                   "remotes": settings.get("remotes", []),
                   # Имя в сертификате шлюза: клиент сверяет его (verify-x509-name).
                   "gateway_name": settings.get("gateway_name", "")}
            if h.secrets.tls_crypt_v2_server and tlscrypt.available():
                vpn["tls_crypt_v2"] = tlscrypt.client_key(h.secrets.tls_crypt_v2_server).decode("ascii")
        else:
            if not access["dst"]:
                raise Refused(503, {"error": "not_configured"})
            not_after = int(time.time()) + int(ca.CLIENT_LIFETIME.total_seconds())
            vpn = {"provider": "vipnet",
                   "dst_password": h.secrets.unseal("dst", media, access["dst"]).decode("utf-8")}
        Store.log(db, "session_granted", media, provider=provider)
        return 200, signed_grant(h, {"media": media, "not_after": not_after, "vpn": vpn,
                                     "targets": _targets(db, media)})

    run_step(h, step)


def events(h) -> None:
    try:
        media, nonce_value, obj = authenticate(h, "/v3/events", limit=EVENTS_LIMIT)
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return
    batch = obj.get("events") if isinstance(obj.get("events"), list) else []

    def step(db):
        begin_step(db, media, nonce_value)
        access = Store.access(db, media)
        acked = access["events_acked"] or 0
        now = int(time.time())
        good = []
        for item in batch:
            if not isinstance(item, dict):
                continue
            seq, code = item.get("seq"), item.get("code")
            if not isinstance(seq, int) or seq <= acked or not isinstance(code, str) \
                    or not re.fullmatch(r"[A-Z][0-9]{2}", code):
                continue
            good.append(item)
        good.sort(key=lambda e: e["seq"])
        if good and good[0]["seq"] > acked + 1:
            # Пропуск: носитель потерял часть очереди (переполнение -- E03).
            Store.log(db, "events_gap", media, first=acked + 1, last=good[0]["seq"] - 1)
            acked = good[0]["seq"] - 1
        for item in good:
            params = item.get("p") if isinstance(item.get("p"), dict) else {}
            db.execute("INSERT OR IGNORE INTO device_events (media_id, seq, code, t, boot, params,"
                       " received) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (media, item["seq"], item["code"],
                        item.get("t") if isinstance(item.get("t"), int) else None,
                        str(item.get("boot", ""))[:64] or None,
                        json.dumps(params, ensure_ascii=False, sort_keys=True)[:4096], now))
        while db.execute("SELECT 1 FROM device_events WHERE media_id = ? AND seq = ?",
                         (media, acked + 1)).fetchone():
            acked += 1
        db.execute("UPDATE access SET events_acked = ? WHERE media_id = ?", (acked, media))
        return 200, {"acked": acked}

    run_step(h, step)


# ---------------------------------------------------------------------------
# для шлюза и для всех
# ---------------------------------------------------------------------------
#: CRL по хранилищу: (время, PEM). Сбрасывается при каждом отзыве сертификатов,
#: чтобы клон попадал в список сразу, а не после истечения кэша.
_crl_cache: dict = {}


def invalidate_crl(store) -> None:
    _crl_cache.pop(str(store.path), None)


def crl(h) -> None:
    if not h.secrets.v3_ready:
        h._send(503, b"not configured\n")
        return
    key = str(h.store.path)
    cached = _crl_cache.get(key)
    if cached is None or time.time() - cached[0] > 10:
        cached = (time.time(), ca.crl(h.secrets.ca_key, h.secrets.ca_cert_pem, h.store.revoked_certs()))
        _crl_cache[key] = cached
    h._send(200, cached[1], content_type="application/x-pem-file", no_store=True)


def descriptor(h) -> None:
    path = h.state_dir / "descriptor.json"
    if not path.is_file():
        h.send_error(404, "no descriptor")
        return
    h._send(200, path.read_bytes(), content_type="application/json", no_store=True)


def release_file(h, rest: str) -> None:
    parts = rest.split("/")
    if len(parts) != 3 or not NAME_RE.match(parts[0]) or not parts[1].isdigit() \
            or not NAME_RE.match(parts[2]):
        h.send_error(404, "no such file")
        return
    path = h.state_dir / "releases" / parts[0] / parts[1] / parts[2]
    if not path.is_file():
        h.send_error(404, "no such file")
        return
    size = path.stat().st_size
    start, end = 0, size - 1
    ranged = (h.headers.get("Range") or "").strip()
    match = re.fullmatch(r"bytes=(\d+)-(\d*)", ranged)
    if match:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        if start >= size or end < start:
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{size}")
            h.end_headers()
            return
        end = min(end, size - 1)
    h.send_response(206 if match else 200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(end - start + 1))
    if match:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    h.end_headers()
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = end - start + 1
        while remaining:
            chunk = handle.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.wfile.write(chunk)
            remaining -= len(chunk)


# ---------------------------------------------------------------------------
# администратор
# ---------------------------------------------------------------------------
def _valid_target(spec) -> dict:
    if not isinstance(spec, dict):
        raise Refused(400, {"error": "bad_target"})
    out = {}
    for key in ("id", "name", "type", "host", "user", "domain", "pin"):
        value = spec.get(key, "")
        if not isinstance(value, str) or len(value) > 4096:
            raise Refused(400, {"error": "bad_target", "field": key})
        out[key] = value
    port = spec.get("port")
    smartcard = spec.get("smartcard", False)
    # Уровень безопасности RDP: NLA по умолчанию; TLS -- для серверов без
    # CredSSP (xrdp на Linux-местах). Учётные данные и тогда идут только
    # внутри TLS с закреплённым сертификатом.
    security = spec.get("security", "nla")
    if security not in ("nla", "tls"):
        raise Refused(400, {"error": "bad_target", "field": "security"})
    out["security"] = security
    if not NAME_RE.match(out["id"]) or out["type"] not in TARGET_TYPES or not out["host"] \
            or not isinstance(port, int) or not 0 < port < 65536 or not isinstance(smartcard, bool) \
            or not out["pin"]:
        raise Refused(400, {"error": "bad_target", "id": out.get("id")})
    out["port"], out["smartcard"] = port, smartcard
    return out


def _require_media(db, media) -> None:
    if not isinstance(media, str) or not MEDIA_RE.match(media) or Store.media(db, media) is None:
        raise Refused(404, {"error": "unknown_media"})


def _require_group(db, group) -> None:
    if not isinstance(group, str) or db.execute("SELECT 1 FROM groups WHERE id = ?",
                                                (group,)).fetchone() is None:
        raise Refused(404, {"error": "unknown_group"})


def _cmd_activation(h, db, obj):
    _require_media(db, obj.get("media"))
    code_hash, expires = obj.get("code_hash"), obj.get("expires")
    if not isinstance(code_hash, str) or not HEX64_RE.match(code_hash) \
            or not isinstance(expires, int) or expires <= time.time():
        raise Refused(400, {"error": "bad_activation"})
    db.execute("DELETE FROM activations WHERE media_id = ? AND used_by IS NULL", (obj["media"],))
    db.execute("INSERT OR REPLACE INTO activations (code_hash, media_id, expires, issued_at)"
               " VALUES (?, ?, ?, ?)", (code_hash, obj["media"], expires, int(time.time())))
    # Новая активация -- новый человек или новый телефон: прежняя привязка гаснет.
    db.execute("UPDATE access SET activated = 0, totp = NULL WHERE media_id = ?", (obj["media"],))
    return {}


def _cmd_targets(h, db, obj):
    _require_media(db, obj.get("media"))
    targets = obj.get("targets")
    if not isinstance(targets, list) or len(targets) > 64:
        raise Refused(400, {"error": "bad_targets"})
    specs = [_valid_target(t) for t in targets]
    db.execute("DELETE FROM targets WHERE media_id = ?", (obj["media"],))
    for spec in specs:
        db.execute("INSERT INTO targets (media_id, target_id, spec) VALUES (?, ?, ?)",
                   (obj["media"], spec["id"], json.dumps(spec, ensure_ascii=False, sort_keys=True)))
    return {"count": len(specs)}


def _cmd_get_targets(h, db, obj):
    _require_media(db, obj.get("media"))
    return {"targets": _targets(db, obj["media"])}


def _cmd_unblock(h, db, obj):
    _require_media(db, obj.get("media"))
    db.execute("UPDATE media SET blocked = 0 WHERE media_id = ?", (obj["media"],))
    db.execute("UPDATE access SET fails = 0 WHERE media_id = ?", (obj["media"],))
    return {}


def _cmd_group(h, db, obj):
    group, parent = obj.get("id"), obj.get("parent", ROOT_GROUP)
    if not isinstance(group, str) or not NAME_RE.match(group) or group == ROOT_GROUP:
        raise Refused(400, {"error": "bad_group"})
    _require_group(db, parent)
    ancestor, seen = parent, set()
    while ancestor and ancestor not in seen:
        if ancestor == group:
            raise Refused(409, {"error": "cycle"})
        seen.add(ancestor)
        row = db.execute("SELECT parent FROM groups WHERE id = ?", (ancestor,)).fetchone()
        ancestor = row["parent"] if row else None
    db.execute("INSERT INTO groups (id, parent) VALUES (?, ?)"
               " ON CONFLICT(id) DO UPDATE SET parent = excluded.parent", (group, parent))
    return {}


def _cmd_media_group(h, db, obj):
    _require_media(db, obj.get("media"))
    _require_group(db, obj.get("group"))
    db.execute("UPDATE media SET group_id = ? WHERE media_id = ?", (obj["group"], obj["media"]))
    return {}


def _cmd_assign(h, db, obj):
    _require_group(db, obj.get("group"))
    distribution = obj.get("distribution")
    if distribution is not None and db.execute("SELECT 1 FROM distributions WHERE id = ?",
                                               (distribution,)).fetchone() is None:
        raise Refused(404, {"error": "unknown_distribution"})
    db.execute("UPDATE groups SET distribution = ? WHERE id = ?", (distribution, obj["group"]))
    return {}


def _cmd_policy(h, db, obj):
    _require_group(db, obj.get("group"))
    allow_vm = obj.get("allow_vm")
    if allow_vm not in (True, False, None):
        raise Refused(400, {"error": "bad_policy"})
    db.execute("UPDATE groups SET allow_vm = ? WHERE id = ?",
               (None if allow_vm is None else int(allow_vm), obj["group"]))
    return {}


def _cmd_distribution(h, db, obj):
    dist = obj.get("id")
    family, arch, layout = obj.get("family"), obj.get("arch"), obj.get("layout")
    min_serial = obj.get("min_serial", 0)
    if not isinstance(dist, str) or not NAME_RE.match(dist) or family not in ("debian", "redos") \
            or not isinstance(arch, str) or not NAME_RE.match(arch) or not isinstance(layout, int) \
            or not isinstance(min_serial, int) or min_serial < 0:
        raise Refused(400, {"error": "bad_distribution"})
    db.execute("INSERT INTO distributions (id, family, arch, layout, min_serial) VALUES (?, ?, ?, ?, ?)"
               " ON CONFLICT(id) DO UPDATE SET family = excluded.family, arch = excluded.arch,"
               " layout = excluded.layout, min_serial = excluded.min_serial",
               (dist, family, arch, layout, min_serial))
    return {}


def _cmd_release(h, db, obj):
    dist, serial, files = obj.get("distribution"), obj.get("serial"), obj.get("files")
    if db.execute("SELECT 1 FROM distributions WHERE id = ?", (dist,)).fetchone() is None:
        raise Refused(404, {"error": "unknown_distribution"})
    if not isinstance(serial, int) or serial <= 0 or not isinstance(files, dict) or not files:
        raise Refused(400, {"error": "bad_release"})
    directory = h.state_dir / "releases" / dist / str(serial)
    for name, digest in files.items():
        if not NAME_RE.match(name) or not isinstance(digest, str) or not HEX64_RE.match(digest):
            raise Refused(400, {"error": "bad_release"})
        path = directory / name
        if not path.is_file() or _file_sha256(path) != digest:
            raise Refused(409, {"error": "file_missing_or_differs", "file": name})
    latest = Store.latest_release(db, dist)
    if latest is not None and serial <= latest["serial"]:
        raise Refused(409, {"error": "stale_serial"})
    db.execute("INSERT INTO releases (distribution, serial, files, published_at) VALUES (?, ?, ?, ?)",
               (dist, serial, json.dumps(files, sort_keys=True), int(time.time())))
    return {}


def _cmd_vpn(h, db, obj):
    remotes = obj.get("remotes")
    if not isinstance(remotes, list) or not remotes or len(remotes) > 16:
        raise Refused(400, {"error": "bad_vpn"})
    clean = []
    for r in remotes:
        if not isinstance(r, dict) or not isinstance(r.get("host"), str) or not r["host"] \
                or not isinstance(r.get("port"), int) or r.get("proto") not in ("udp", "tcp"):
            raise Refused(400, {"error": "bad_vpn"})
        clean.append({"host": r["host"], "port": r["port"], "proto": r["proto"]})
    name = obj.get("gateway_name", "")
    if not isinstance(name, str) or len(name) > 253:
        raise Refused(400, {"error": "bad_vpn"})
    tmp = h.state_dir / "vpn.json.tmp"
    tmp.write_text(json.dumps({"remotes": clean, "gateway_name": name}, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, h.state_dir / "vpn.json")
    return {}


def _cmd_dst(h, db, obj):
    _require_media(db, obj.get("media"))
    password = obj.get("password")
    if not isinstance(password, str) or not password or len(password) > 256:
        raise Refused(400, {"error": "bad_password"})
    Store.access(db, obj["media"])
    db.execute("UPDATE access SET dst = ? WHERE media_id = ?",
               (h.secrets.seal("dst", obj["media"], password.encode("utf-8")), obj["media"]))
    return {}


def _cmd_descriptor(h, db, obj):
    """Публикует описатель. Сервер его не проверяет -- корня организации он не
    знает; проверяет носитель. Сервер лишь раздаёт."""
    document = obj.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("descriptor"), dict):
        raise Refused(400, {"error": "bad_descriptor"})
    tmp = h.state_dir / "descriptor.json.tmp"
    tmp.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, h.state_dir / "descriptor.json")
    return {"serial": document["descriptor"].get("serial")}


def _cmd_log(h, db, obj):
    since = obj.get("since", 0)
    if not isinstance(since, int):
        raise Refused(400, {"error": "bad_since"})
    rows = db.execute("SELECT * FROM server_log WHERE id > ? ORDER BY id LIMIT 500",
                      (since,)).fetchall()
    events_since = obj.get("events_since", 0)
    events_rows = db.execute("SELECT rowid AS id, * FROM device_events WHERE rowid > ?"
                             " ORDER BY rowid LIMIT 2000", (events_since,)).fetchall()
    device = []
    for row in events_rows:
        record = dict(row)
        params = json.loads(record["params"] or "{}")
        record["text"] = catalog.describe(record["media_id"], record["code"], params)
        device.append(record)
    return {"server": [dict(r) for r in rows], "device": device}


def _cmd_overview(h, db, obj):
    """Всё, что окно «Выпуски и группы» показывает, -- одним ответом: группы с
    действующим (унаследованным) назначением, дистрибутивы с выпусками и то,
    в какой группе каждый носитель."""
    groups = []
    for row in db.execute("SELECT * FROM groups ORDER BY id").fetchall():
        groups.append({"id": row["id"], "parent": row["parent"],
                       "distribution": row["distribution"],
                       "allow_vm": None if row["allow_vm"] is None else bool(row["allow_vm"]),
                       "effective": Store.effective(db, row["id"], "distribution"),
                       "effective_vm": bool(Store.effective(db, row["id"], "allow_vm") or 0)})
    distributions = []
    for row in db.execute("SELECT * FROM distributions ORDER BY id").fetchall():
        releases = [r["serial"] for r in db.execute(
            "SELECT serial FROM releases WHERE distribution = ? ORDER BY serial DESC LIMIT 20",
            (row["id"],)).fetchall()]
        distributions.append({**dict(row), "releases": releases})
    media = [{"media_id": r["media_id"], "group": r["group_id"] or ROOT_GROUP}
             for r in db.execute("SELECT media_id, group_id FROM media ORDER BY media_id").fetchall()]
    return {"groups": groups, "distributions": distributions, "media": media}


COMMANDS = {
    "overview": _cmd_overview,
    "activation": _cmd_activation, "targets": _cmd_targets, "unblock": _cmd_unblock,
    "group": _cmd_group, "media_group": _cmd_media_group, "assign": _cmd_assign,
    "policy": _cmd_policy, "distribution": _cmd_distribution, "release": _cmd_release,
    "vpn": _cmd_vpn, "dst": _cmd_dst, "log": _cmd_log, "get_targets": _cmd_get_targets,
    "descriptor": _cmd_descriptor,
}
#: Команды, которые только читают: им не нужен счётчик (повтор безвреден).
READ_ONLY = {"log", "get_targets", "overview"}


def admin_command(h, name: str) -> None:
    command = COMMANDS.get(name)
    if command is None:
        h.send_error(404, "unknown command")
        return
    try:
        body = read_body(h)
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)
        return
    key_id = h._admin_signed(body)
    if key_id is None:
        send_json(h, 403, FORBIDDEN)
        return
    try:
        obj = parse_json(body)
        if obj.get("cmd") != name or not isinstance(obj.get("counter"), int):
            raise Refused(400, {"error": "bad_command"})
        with h.store.tx() as db:
            if name not in READ_ONLY and not Store.advance_counter(db, key_id, obj["counter"]):
                raise Refused(409, {"error": "stale_counter"})
            result = command(h, db, obj)
            if name not in READ_ONLY:
                Store.log(db, f"admin_{name}", obj.get("media"), admin=key_id)
        send_json(h, 200, {"ok": True, **result})
    except Refused as refusal:
        send_json(h, refusal.code, refusal.body)


def _file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


#: Предел файла выпуска. Образ системы -- около 1,6 ГиБ, с запасом.
UPLOAD_LIMIT = 8 << 30


def admin_upload(h, rest: str) -> None:
    """PUT /admin/v3/upload/<дистрибутив>/<номер>/<файл>.

    Подписывается строка `poa-upload <дистрибутив> <номер> <файл> <sha256> <размер>`.
    Сервер считает хеш на лету и кладёт файл на место, только если он сошёлся.
    Счётчик не нужен: содержимое адресовано хешем, повтор кладёт то же самое.
    """
    parts = rest.split("/")
    if len(parts) != 3 or not NAME_RE.match(parts[0]) or not parts[1].isdigit() \
            or not NAME_RE.match(parts[2]):
        send_json(h, 400, {"error": "bad_path"})
        return
    raw_len = (h.headers.get("Content-Length") or "").strip()
    digest = (h.headers.get("X-POA-SHA256") or "").strip()
    if not raw_len.isascii() or not raw_len.isdigit() or not 0 < int(raw_len) <= UPLOAD_LIMIT \
            or not HEX64_RE.match(digest):
        send_json(h, 400, {"error": "bad_upload"})
        return
    size = int(raw_len)
    message = f"poa-upload {parts[0]} {parts[1]} {parts[2]} {digest} {size}".encode("ascii")
    if h._admin_signed(message) is None:
        send_json(h, 403, FORBIDDEN)
        return
    directory = h.state_dir / "releases" / parts[0] / parts[1]
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f".{parts[2]}.part"
    hasher = hashlib.sha256()
    remaining = size
    with tmp.open("wb") as out:
        while remaining:
            chunk = h.rfile.read(min(1 << 20, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            out.write(chunk)
            remaining -= len(chunk)
    if remaining or hasher.hexdigest() != digest:
        tmp.unlink(missing_ok=True)
        send_json(h, 409, {"error": "digest_mismatch"})
        return
    os.replace(tmp, directory / parts[2])
    send_json(h, 200, {"ok": True})
