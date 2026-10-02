"""Сервер доступа v3: живой, в процессе, на 127.0.0.1 со случайным портом.

Проверяется договор `docs/ACCESS-PROTOCOL.md` и прежде всего правила звена --
таблица раздела 6.3 целиком: обычный вход, потерянное подтверждение, сбой
записи на носителе, клон в обоих порядках. Часы TOTP подменяются, чтобы
несколько входов подряд не упирались в защиту от повтора кода.
"""
from __future__ import annotations

import hashlib
import http.client
import http.server
import importlib.util
import json
import pathlib
import secrets
import threading
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1] / "packaging" / "revocation-server"


def _load_server():
    spec = importlib.util.spec_from_file_location("poa_access_server_v3",
                                                  SERVER_DIR / "poa-revocation-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srv = _load_server()
from poa_access import ca, canon, secrets_store, totp, v3  # noqa: E402  (путь добавил сервер)
from poa_access.store import Store  # noqa: E402

MEDIA = "0a1b2c3d-11223344"


class Clock:
    """Часы TOTP под управлением теста."""

    def __init__(self) -> None:
        self.now = 1_790_000_000.0

    def time(self) -> float:
        return self.now


class _Server:
    def __init__(self, root: pathlib.Path, clock: Clock) -> None:
        self.root = root
        self.sign_key = ed25519.Ed25519PrivateKey.generate()
        self.admin = ed25519.Ed25519PrivateKey.generate()
        self.admin_id = srv.fingerprint(self.admin.public_key())
        ca_key_pem, ca_cert_pem = ca.new_ca("test")
        self.ca_cert_pem = ca_cert_pem
        secrets_ = secrets_store.ServerSecrets(
            sign_key=self.sign_key,
            ca_key=serialization.load_pem_private_key(ca_key_pem, password=None),
            ca_cert_pem=ca_cert_pem,
            data_key=secrets.token_bytes(32),
        )
        db_path = root / "media.sqlite"
        handler = type("TestHandler", (srv.Handler,), {
            "key": self.sign_key,
            "db": srv.Database(db_path),
            "store": Store(db_path),
            "secrets": secrets_,
            "state_dir": root,
            "admins": {self.admin_id: self.admin.public_key()},
            "admins_dir": root / "admins",
            "log_message": lambda *args: None,
        })
        self.handler = handler
        self.counter = 0
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.clock = clock

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method, path, headers=None, body=b""):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read(), response
        finally:
            conn.close()

    # -- администратор ------------------------------------------------------
    def admin_cmd(self, name: str, **params):
        self.counter += 1
        body = canon.canonical({"cmd": name, "counter": self.counter, **params})
        status, raw, _ = self.request("POST", f"/admin/v3/{name}", {
            "X-POA-Key-Id": self.admin_id,
            "X-POA-Signature": canon.b64(self.admin.sign(body)),
            "Content-Length": str(len(body)),
        }, body)
        return status, json.loads(raw) if raw else {}

    def register(self, media: str, device) -> None:
        raw = device.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw)
        body = f"poa-media {media} 1 ok {canon.b64(raw)}".encode("ascii")
        status, _, _ = self.request("POST", f"/admin/media/{media}", {
            "X-POA-Key-Id": self.admin_id,
            "X-POA-Signature": canon.b64(self.admin.sign(body)),
            "Content-Length": str(len(body)),
        }, body)
        assert status == 201

    def issue_code(self, media: str) -> str:
        code = secrets.token_hex(5).upper()
        status, _ = self.admin_cmd("activation", media=media,
                                   code_hash=hashlib.sha256(code.encode()).hexdigest(),
                                   expires=int(time.time()) + 3600)
        assert status == 200
        return code


class Device:
    """Носитель: ключ, звено, семя TOTP -- то, что лежит в его хранилище."""

    def __init__(self, server: _Server, media: str = MEDIA, key=None) -> None:
        self.server = server
        self.media = media
        self.key = key or ed25519.Ed25519PrivateKey.generate()
        self.link = None
        self.secret = None

    def copy(self) -> "Device":
        clone = Device(self.server, self.media, self.key)
        clone.link, clone.secret = self.link, self.secret
        return clone

    def nonce(self) -> str:
        body = canon.canonical({"media": self.media})
        status, raw, _ = self.server.request("POST", "/v3/nonce",
                                             {"Content-Length": str(len(body))}, body)
        assert status == 200
        return json.loads(raw)["nonce"]

    def post(self, path: str, obj: dict, nonce: str | None = None):
        nonce = nonce or self.nonce()
        body = canon.canonical(obj)
        message = canon.device_message("POST", path, self.media, nonce, body)
        status, raw, _ = self.server.request("POST", path, {
            "X-POA-Media": self.media, "X-POA-Nonce": nonce,
            "X-POA-Signature": canon.b64(self.key.sign(message)),
            "Content-Length": str(len(body)),
        }, body)
        return status, (json.loads(raw) if raw else {})

    def code(self) -> str:
        return totp.code_at(self.secret, int(self.server.clock.now // totp.PERIOD))

    def activate(self, code: str):
        status, body = self.post("/v3/activate", {"code": code, "info": {"virt": "none"}})
        if status == 200:
            grant = body["grant"]
            self.server.sign_key.public_key().verify(canon.unb64(body["signature"]),
                                                     canon.canonical(grant))
            self.secret, self.link = grant["totp"]["secret"], grant["link"]
        return status, body

    def session(self, link: str | None = None, virt: str = "none", serial: int = 1):
        self.server.clock.now += totp.PERIOD
        return self.post("/v3/session", {"totp": self.code(), "link": link or self.link,
                                         "status": {"virt": virt, "serial": serial}})

    def confirm(self, link: str):
        vpn_key = ec.generate_private_key(ec.SECP256R1())
        pem = vpn_key.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)
        return self.post("/v3/session/confirm", {"link": link, "vpn": {
            "provider": "openvpn", "public_key": pem.decode("ascii")}})

    def login(self):
        status, body = self.session()
        assert status == 200, body
        proposal = body["proposal"]
        self.link = proposal          # tmp + fsync + rename на носителе
        status, body = self.confirm(proposal)
        assert status == 200, body
        return body


@pytest.fixture
def server(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(totp, "time", clock)
    running = _Server(tmp_path, clock)
    yield running
    running.close()


@pytest.fixture
def device(server):
    dev = Device(server)
    server.register(dev.media, dev.key)
    code = server.issue_code(dev.media)
    status, _ = dev.activate(code)
    assert status == 200
    return dev


# -- обычный путь ---------------------------------------------------------------
def test_login_issues_a_signed_day_long_certificate_for_this_medium(server, device):
    server.admin_cmd("targets", media=MEDIA, targets=[{
        "id": "ws-1", "name": "РМ", "type": "rdp", "host": "10.0.0.5", "port": 3389,
        "user": "ivanov", "domain": "CORP", "pin": "sha256:" + "a" * 64}])
    body = device.login()
    grant = body["grant"]
    server.sign_key.public_key().verify(canon.unb64(body["signature"]), canon.canonical(grant))
    cert = x509.load_pem_x509_certificate(grant["vpn"]["cert"].encode())
    assert cert.subject.rfc4514_string() == f"CN={MEDIA}"
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert 24 * 3600 <= lifetime.total_seconds() <= 24 * 3600 + 600
    assert grant["targets"][0]["host"] == "10.0.0.5"


def test_nothing_is_granted_before_the_new_link_is_confirmed(server, device):
    """Ответ на /v3/session не содержит доступа -- только предложенное звено."""
    status, body = device.session()
    assert status == 200
    assert set(body) == {"proposal", "release", "server_time"}


# -- правила звена --------------------------------------------------------------
def test_a_copy_used_after_the_owner_logged_in_is_a_clone(server, device):
    copy = device.copy()
    device.login()
    status, body = copy.session()
    assert (status, body) == (409, {"error": "clone"})
    status, _ = device.session()
    assert status == 423, "носитель после клона должен быть заблокирован"


def test_a_copy_that_logs_in_first_is_caught_when_the_owner_returns(server, device):
    copy = device.copy()
    grant = copy.login()["grant"]
    status, body = device.session()
    assert (status, body) == (409, {"error": "clone"})
    # Сертификат, выданный копии, отозван и виден шлюзу в CRL.
    serial = x509.load_pem_x509_certificate(grant["vpn"]["cert"].encode()).serial_number
    status, raw, _ = server.request("GET", "/v3/crl")
    crl = x509.load_pem_x509_crl(raw)
    assert crl.get_revoked_certificate_by_serial_number(serial) is not None


def test_failed_write_on_the_medium_lets_the_old_link_in_again(server, device):
    """Носитель не успел записать N+1 -- предъявляет N, сервер принимает."""
    status, _ = device.session()          # предложено N+1, но не записано
    assert status == 200
    device.login()                         # снова с N
    status, _ = device.session()
    assert status == 200


def test_lost_confirmation_counts_the_written_link_as_confirmed(server, device):
    """Запись удалась, подтверждение не дошло -- носитель предъявит N+1."""
    status, body = device.session()
    device.link = body["proposal"]        # записано, confirm потерян
    device.login()
    status, _ = device.session()
    assert status == 200


def test_an_unknown_link_is_refused_without_blocking(server, device):
    status, body = device.session(link=canon.b64(secrets.token_bytes(32)))
    assert (status, body) == (403, {"error": "forbidden"})
    device.login()


def test_confirm_with_anything_but_the_proposal_is_refused(server, device):
    device.session()
    status, _ = device.confirm(device.link)
    assert status == 403


# -- TOTP и nonce -----------------------------------------------------------------
def test_wrong_totp_is_refused_and_ten_in_a_row_block_the_medium(server, device):
    for _ in range(v3.TOTP_FAIL_LIMIT):
        status, body = device.post("/v3/session", {"totp": "000000", "link": device.link,
                                                   "status": {}})
        if status == 423:
            break
        assert status in (401, 403)
    status, _ = device.session()
    assert status == 423


def test_the_same_totp_code_does_not_pass_twice(server, device):
    device.login()
    code = device.code()                 # шаг, уже принятый при входе
    status, _ = device.post("/v3/session", {"totp": code, "link": device.link, "status": {}})
    assert status == 401


def test_a_nonce_works_once(server, device):
    nonce = device.nonce()
    device.server.clock.now += totp.PERIOD
    first, _ = device.post("/v3/session", {"totp": device.code(), "link": device.link,
                                           "status": {}}, nonce=nonce)
    second, _ = device.post("/v3/session", {"totp": device.code(), "link": device.link,
                                            "status": {}}, nonce=nonce)
    assert (first, second) == (200, 403)


def test_a_foreign_signature_is_refused(server, device):
    stranger = Device(server, MEDIA, ed25519.Ed25519PrivateKey.generate())
    stranger.secret, stranger.link = device.secret, device.link
    status, body = stranger.session()
    assert (status, body) == (403, {"error": "forbidden"})


# -- активация ----------------------------------------------------------------------
def test_activation_twice_after_first_login_is_reported(server, device):
    device.login()
    code = server.issue_code(MEDIA)
    status, _ = device.activate(code)
    assert status == 200
    device.login()
    status, body = device.activate(code)
    assert (status, body) == (409, {"error": "already_activated"})


# -- политика и выпуски -----------------------------------------------------------------
def test_virtual_machine_is_refused_unless_the_group_allows_it(server, device):
    status, body = device.session(virt="kvm")
    assert (status, body) == (403, {"error": "virtualization"})
    assert server.admin_cmd("policy", group="root", allow_vm=True)[0] == 200
    status, _ = device.session(virt="kvm")
    assert status == 200


def test_old_release_gets_426_with_the_release_to_take(server, device):
    data = b"image"
    digest = hashlib.sha256(data).hexdigest()
    message = f"poa-upload d1 5 system.squashfs {digest} {len(data)}".encode()
    status, _, _ = server.request("PUT", "/admin/v3/upload/d1/5/system.squashfs", {
        "X-POA-Key-Id": server.admin_id, "X-POA-SHA256": digest,
        "X-POA-Signature": canon.b64(server.admin.sign(message)),
        "Content-Length": str(len(data))}, data)
    assert status == 200
    assert server.admin_cmd("distribution", id="d1", family="debian", arch="amd64",
                            layout=2, min_serial=5)[0] == 200
    assert server.admin_cmd("release", distribution="d1", serial=5,
                            files={"system.squashfs": digest})[0] == 200
    assert server.admin_cmd("assign", group="root", distribution="d1")[0] == 200
    status, body = device.session(serial=4)
    assert status == 426 and body["release"]["serial"] == 5
    status, raw, response = server.request("GET", "/v3/release/d1/5/system.squashfs",
                                           {"Range": "bytes=2-"})
    assert (status, raw) == (206, b"age")
    status, body = device.session(serial=5)
    assert status == 200 and body["release"]["serial"] == 5


def test_group_assignment_is_inherited_and_overridden(server, device):
    for name in ("d1", "d2"):
        server.admin_cmd("distribution", id=name, family="debian", arch="amd64", layout=2)
    server.admin_cmd("group", id="dept", parent="root")
    server.admin_cmd("group", id="team", parent="dept")
    server.admin_cmd("assign", group="dept", distribution="d1")
    server.admin_cmd("media_group", media=MEDIA, group="team")
    with server.handler.store.tx() as db:
        assert Store.effective(db, "team", "distribution") == "d1"
    server.admin_cmd("assign", group="team", distribution="d2")
    with server.handler.store.tx() as db:
        assert Store.effective(db, "team", "distribution") == "d2"
    assert server.admin_cmd("group", id="dept", parent="team")[0] == 409


# -- журнал и команды -----------------------------------------------------------------
def test_events_are_acknowledged_up_to_the_last_contiguous_seq(server, device):
    status, body = device.post("/v3/events", {"catalog": 1, "events": [
        {"seq": 1, "code": "E01", "t": 1, "p": {}}, {"seq": 2, "code": "D01", "t": 2}]})
    assert (status, body) == (200, {"acked": 2})
    status, body = device.post("/v3/events", {"catalog": 1, "events": [
        {"seq": 5, "code": "E02", "t": 5}]})
    assert (status, body) == (200, {"acked": 5})
    status, log = server.admin_cmd("log", since=0)
    assert any(r["kind"] == "events_gap" for r in log["server"])


def test_a_replayed_admin_command_is_refused(server):
    body = canon.canonical({"cmd": "policy", "counter": 1, "group": "root", "allow_vm": True})
    headers = {"X-POA-Key-Id": server.admin_id, "Content-Length": str(len(body)),
               "X-POA-Signature": canon.b64(server.admin.sign(body))}
    assert server.request("POST", "/admin/v3/policy", headers, body)[0] == 200
    assert server.request("POST", "/admin/v3/policy", headers, body)[0] == 409


def test_unsigned_admin_command_is_refused(server):
    body = canon.canonical({"cmd": "policy", "counter": 99, "group": "root", "allow_vm": True})
    status, _, _ = server.request("POST", "/admin/v3/policy", {
        "X-POA-Key-Id": server.admin_id, "Content-Length": str(len(body)),
        "X-POA-Signature": canon.b64(ed25519.Ed25519PrivateKey.generate().sign(body))}, body)
    assert status == 403


def test_rdp_security_is_nla_unless_the_target_says_tls(server, device):
    base = {"id": "ws9", "name": "x", "type": "rdp", "host": "10.0.0.9", "port": 3389,
            "user": "u", "domain": "", "pin": "sha256:" + "0" * 64}
    assert server.admin_cmd("targets", media=MEDIA, targets=[base])[0] == 200
    assert server.admin_cmd("get_targets", media=MEDIA)[1]["targets"][0]["security"] == "nla"
    assert server.admin_cmd("targets", media=MEDIA, targets=[{**base, "security": "tls"}])[0] == 200
    assert server.admin_cmd("get_targets", media=MEDIA)[1]["targets"][0]["security"] == "tls"
    assert server.admin_cmd("targets", media=MEDIA, targets=[{**base, "security": "rdp"}])[0] == 400
