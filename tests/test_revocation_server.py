"""Сервер отзыва: живой, в процессе, на 127.0.0.1 со случайным портом.

Здесь сеть нужна по делу: проверяется не форма команд, а то, что сервер с
ними делает, -- включая многопоточность (гонка знакомства) и разбор заголовков
на уровне HTTP (кривой `Content-Length`). Подменный обработчик этого не покажет.

Подпись носителя делается `cryptography`, а на носителе -- `openssl pkeyutl
-rawin`: обе дают чистую Ed25519 над байтами строки, это сверено при написании
клиентской части (см. `docs/RELEASE-1.1.md`).
"""
from __future__ import annotations

import base64
import http.client
import http.server
import importlib.util
import os
import pathlib
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from portable_os_admin.admin import revocation_client

SERVER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "packaging" / "revocation-server" / "poa-revocation-server.py"
)
INFO = "version=1.1.0;machine=ab12cd34"


def _load_server():
    # Имя файла с дефисами -- обычным import его не взять.
    spec = importlib.util.spec_from_file_location("poa_revocation_server", SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


srv = _load_server()


def _raw_public(key) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )).decode("ascii")


class _Server:
    """Поднятый сервер и всё, что нужно тестам, чтобы с ним говорить."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.revocation_key = ed25519.Ed25519PrivateKey.generate()
        self.admin = ed25519.Ed25519PrivateKey.generate()
        self.stranger = ed25519.Ed25519PrivateKey.generate()
        self.admins_dir = root / "admins"
        self.db_path = root / "media.sqlite"
        admins = {
            srv.fingerprint(self.admin.public_key()): self.admin.public_key(),
            srv.fingerprint(self.stranger.public_key()): self.stranger.public_key(),
        }
        # Подкласс на тест: атрибуты обработчика классовые, и без этого один
        # тест оставлял бы свою базу и своих администраторов следующему.
        handler = type("TestHandler", (srv.Handler,), {
            "key": self.revocation_key,
            "db": srv.Database(self.db_path),
            "admins": admins,
            "admins_dir": self.admins_dir,
            "log_message": lambda *args: None,
        })
        self.handler = handler
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- запросы -----------------------------------------------------------
    def request(self, method: str, path: str, headers=None, body: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read(), response
        finally:
            conn.close()

    def publish(self, media: str, counter: int, state: str = "ok",
                device_key: str | None = None, admin=None) -> int:
        admin = admin or self.admin
        text = f"poa-media {media} {counter} {state}"
        if device_key is not None:
            text += f" {device_key}"
        body = text.encode("ascii")
        status, _, _ = self.request("POST", f"/admin/media/{media}", {
            "X-POA-Key-Id": srv.fingerprint(admin.public_key()),
            "X-POA-Signature": base64.b64encode(admin.sign(body)).decode("ascii"),
        }, body)
        return status

    def ask(self, media: str, device, nonce: str | None = None, info: str = INFO):
        nonce = nonce or secrets.token_hex(16)
        signature = device.sign(f"poa-ask-v3 {media} {nonce} {info}".encode("ascii"))
        return self.request("GET", "/v3/media-state", {
            "X-POA-Media": media,
            "X-POA-Nonce": nonce,
            "X-POA-Info": info,
            "X-POA-Signature": base64.b64encode(signature).decode("ascii"),
        }) + (nonce,)

    def query(self, media: str, admin=None, moment: int | None = None):
        admin = admin or self.admin
        moment = int(time.time()) if moment is None else moment
        signature = admin.sign(f"poa-query {media} {moment}".encode("ascii"))
        return self.request("GET", f"/admin/media/{media}", {
            "X-POA-Key-Id": srv.fingerprint(admin.public_key()),
            "X-POA-Time": str(moment),
            "X-POA-Signature": base64.b64encode(signature).decode("ascii"),
        })


@pytest.fixture
def server(tmp_path):
    running = _Server(tmp_path)
    yield running
    running.close()


@pytest.fixture
def device():
    return ed25519.Ed25519PrivateKey.generate()


# -- носитель спрашивает ------------------------------------------------------
def test_a_signed_ask_gets_a_signed_state_and_is_remembered(server, device):
    assert server.publish("cafe0001", 1, "ok", _raw_public(device)) == 201

    status, body, response, nonce = server.ask("cafe0001", device)

    assert status == 200
    assert response.getheader("Cache-Control") == "no-store"
    message, signature = body.decode("ascii").splitlines()
    assert message == f"poa-state-v3 cafe0001 {nonce} ok"
    server.revocation_key.public_key().verify(base64.b64decode(signature),
                                              message.encode("ascii"))

    status, body, _ = server.query("cafe0001")
    assert status == 200
    fields = body.decode("ascii").split()
    assert fields[:3] == ["poa-record", "cafe0001", "ok"]
    assert fields[3].endswith("+00:00")
    assert fields[4:] == ["1.1.0", "ab12cd34"]


@pytest.mark.parametrize("state", ["ok", "revoked"])
def test_legacy_media_never_receive_an_actionable_state(server, device, state):
    server.publish("cafe0001", 1, state, _raw_public(device))
    nonce = secrets.token_hex(16)
    signed = device.sign(f"poa-ask cafe0001 {nonce} {INFO}".encode("ascii"))
    headers = {
        "X-POA-Media": "cafe0001",
        "X-POA-Nonce": nonce,
        "X-POA-Info": INFO,
        "X-POA-Signature": base64.b64encode(signed).decode("ascii"),
    }
    for endpoint in ("/v2/state", "/v3/media-state"):
        status, body, _ = server.request("GET", endpoint, headers)
        assert status == 403
        assert b"poa-state" not in body


def test_medium_persists_revocation_without_touching_its_key(server, device, tmp_path, monkeypatch):
    """Два настоящих ключа, curl и сервер. Сеть сеанса и systemd заменены
    временными исполнителями: тест проверяет порядок закрытия без прав root."""
    from portable_os_admin.config.schema import InstallConfig
    from portable_os_admin.install import machine_binding, revocation

    if (not shutil.which("openssl") or not shutil.which("curl")
            or "OpenSSL 3" not in subprocess.check_output(["openssl", "version"], text=True)):
        pytest.skip("для выполнения скрипта нужны curl и openssl с Ed25519 -rawin")

    config = InstallConfig.create_default("test", "debian")
    config.revocation.url = server.url
    run_dir, state, programs = (tmp_path / name for name in ("run", "state", "bin"))
    programs.mkdir()
    # Независимый от оборудования и конкретной ОС отпечаток.
    monkeypatch.setattr(machine_binding, "fingerprint_functions",
                        lambda: "poa_fingerprint_id() { echo ab12cd34; }")
    server_key = tmp_path / "server.pub"
    server_key.write_bytes(server.revocation_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    key = tmp_path / "device.pem"
    private_pem = device.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption())
    key.write_bytes(private_pem)
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("test-boot")
    env = dict(os.environ, PATH=f"{programs}:{os.environ['PATH']}", CALLS=str(tmp_path / "calls"))
    for name, body in {
        "ip": "echo default", "sync": "exit 0", "notify-send": "exit 0", "ps": "exit 0",
        "quarantine": 'echo "firewall" >> "$CALLS"',
        "systemctl": 'echo "$*" >> "$CALLS"',
    }.items():
        exe = programs / name
        exe.write_text("#!/bin/sh\n" + body + "\n")
        exe.chmod(0o755)
    script = revocation.revocation_script(config, "cafe0001", "unused")
    for before, after in {
        "/run/poa": run_dir, "/var/lib/poa": state, "/etc/poa/revocation.pub": server_key,
        "/etc/poa/device.key": key, "/proc/sys/kernel/random/boot_id": boot_id,
        revocation.LOG_PATH: tmp_path / "check.log", "/usr/bin/python3": programs / "quarantine",
        "/etc/poa/tls-pin": tmp_path / "unused-pin",
    }.items():
        script = script.replace(before, str(after))
    def poll():
        subprocess.run(["sh"], input=script, env=env, text=True, check=True)

    assert server.publish("cafe0001", 1, "ok", _raw_public(device)) == 201
    poll()
    assert (run_dir / "authorized").read_text() == boot_id.read_text()
    assert server.publish("cafe0001", 2, "revoked") == 204
    poll()
    assert not (run_dir / "authorized").exists()
    assert (state / "media-revoked").is_dir()
    assert key.read_bytes() == private_pem
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[:2] == ["firewall", "stop poa-accessd.service"]
    assert "stop poa-work-*.service poa-vpn@*.service" in calls

    # Даже новое подписанное ok по ошибочно восстановленной БД не снимает
    # полученный запрет, в том числе после перезагрузки и потери /run.
    shutil.rmtree(run_dir)
    assert server.publish("cafe0001", 3, "ok") == 410
    assert server.handler.db.record_of("cafe0001")["state"] == "revoked"
    with sqlite3.connect(server.db_path) as db:
        db.execute("UPDATE media SET state = 'ok' WHERE media_id = 'cafe0001'")
    poll()
    assert not (run_dir / "authorized").exists()
    assert (run_dir / "media-revoked").exists()
    assert key.read_bytes() == private_pem


def test_state_is_current_when_the_contact_is_recorded(server, device, monkeypatch):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    remember = server.handler.db.remember_contact

    def revoke_then_remember(*args):
        assert server.publish("cafe0001", 2, "revoked") == 204
        return remember(*args)

    monkeypatch.setattr(server.handler.db, "remember_contact", revoke_then_remember)
    status, body, _, nonce = server.ask("cafe0001", device)
    assert status == 200
    assert body.decode("ascii").splitlines()[0] == f"poa-state-v3 cafe0001 {nonce} revoked"


def test_a_revoked_medium_is_told_so(server, device):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    server.publish("cafe0001", 2, "revoked")
    status, body, _, nonce = server.ask("cafe0001", device)
    assert status == 200
    assert body.decode("ascii").splitlines()[0] == f"poa-state-v3 cafe0001 {nonce} revoked"


def test_an_id_with_an_administrator_prefix_works_end_to_end(server, device):
    """С 1.1.0 ID носителя -- «приставка администратора-носитель»: восьми знаков
    на общий сервер мало. Носители, выпущенные до приставки, остаются в базе,
    поэтому сервер понимает оба вида."""
    prefixed = "a1b2c3d4-cafe0001"
    assert server.publish(prefixed, 1, "ok", _raw_public(device)) == 201

    status, body, _, nonce = server.ask(prefixed, device)
    assert status == 200
    assert body.decode("ascii").splitlines()[0] == f"poa-state-v3 {prefixed} {nonce} ok"

    status, body, _ = server.query(prefixed)
    assert status == 200
    assert body.decode("ascii").split()[:3] == ["poa-record", prefixed, "ok"]

    # Приставка не открывает дверь сама по себе: чужой носитель того же
    # администратора неизвестен серверу и получает тот же отказ, что и все.
    assert server.ask("a1b2c3d4-cafe0002", device)[0] == 403
    # Вид ID проверяется: лишние части и не тот набор знаков -- отказ.
    for bad in ("a1b2c3d4-cafe0001-cafe0002", "a1b2c3d4-CAFE0001", "a1b2c3d4-cafe001"):
        assert server.ask(bad, device)[0] == 403


def test_every_refusal_to_a_medium_looks_the_same(server, device):
    """Перебирающий не должен узнать, на каком шаге споткнулся."""
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    # Запись из 1.0.0, перенесённая без ключа.
    with server.handler.db.database.tx() as db:
        db.execute("INSERT INTO media (media_id, owner, state, counter, updated_at)"
                   " VALUES (?, ?, ?, ?, ?)", ("cafe0002", "legacy", "ok", 1, srv.utc_now_iso()))
    other = ed25519.Ed25519PrivateKey.generate()

    refusals = [
        server.request("GET", "/v3/media-state"),                          # нет заголовков
        server.ask("CAFE0001", device)[:3],                          # кривой идентификатор
        server.ask("cafe0001", device, nonce="00" * 8)[:3],          # короткое число
        server.ask("cafe0001", device, info="version=1 1;machine=ab12cd34")[:3],
        server.ask("cafe0001", device, info="version=1.1.0")[:3],    # нет машины
        server.ask("beef0001", device)[:3],                          # неизвестный носитель
        server.ask("cafe0002", device)[:3],                          # у записи нет ключа
        server.ask("cafe0001", other)[:3],                           # подпись чужая
        server.request("GET", "/v3/media-state", {                         # подпись не base64
            "X-POA-Media": "cafe0001", "X-POA-Nonce": "a" * 32,
            "X-POA-Info": INFO, "X-POA-Signature": "!!!",
        }),
        server.request("GET", "/state?media=cafe0001&nonce=" + "a" * 32),  # протокол 1.0.0
    ]
    _, _, _, nonce = server.ask("cafe0001", device)
    refusals.append(server.ask("cafe0001", device, nonce=nonce)[:3])  # повтор числа

    assert [status for status, _, _ in refusals] == [403] * len(refusals)
    assert {body for _, body, _ in refusals} == {srv.FORBIDDEN_BODY}


def test_a_replayed_nonce_does_not_move_the_last_contact(server, device):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    nonce = "0123456789abcdef0123456789abcdef"
    assert server.ask("cafe0001", device, nonce=nonce)[0] == 200
    first = server.handler.db.record_of("cafe0001")["last_seen"]
    assert server.ask("cafe0001", device, nonce=nonce,
                      info="version=9.9.9;machine=00000000")[0] == 403
    record = server.handler.db.record_of("cafe0001")
    assert (record["last_seen"], record["version"]) == (first, "1.1.0")


def test_nonces_are_forgotten_after_a_week(server, device):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    old = int(time.time()) - srv.NONCE_TTL_SECONDS - 60
    with sqlite3.connect(server.db_path) as db:
        db.execute("INSERT INTO nonces VALUES ('cafe0001', ?, ?)", ("f" * 32, old))
    assert server.ask("cafe0001", device)[0] == 200
    with sqlite3.connect(server.db_path) as db:
        left = db.execute("SELECT count(*) FROM nonces WHERE nonce = ?", ("f" * 32,)).fetchone()
    assert left == (0,)


# -- выпуск носителя ----------------------------------------------------------
def test_a_medium_is_not_created_without_its_key(server):
    assert server.publish("cafe0001", 1, "ok") == 400
    assert server.handler.db.record_of("cafe0001") is None


def test_a_broken_device_key_is_refused(server):
    assert server.publish("cafe0001", 1, "ok", base64.b64encode(b"x" * 31).decode()) == 400


def test_the_owner_updates_with_four_fields_and_the_key_stays(server, device):
    assert server.publish("cafe0001", 1, "ok", _raw_public(device)) == 201
    assert server.publish("cafe0001", 2, "ok") == 204
    assert server.ask("cafe0001", device)[0] == 200


def test_a_fifth_field_reissues_the_medium_with_a_new_key(server, device):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    new = ed25519.Ed25519PrivateKey.generate()
    assert server.publish("cafe0001", 2, "ok", _raw_public(new)) == 204
    assert server.ask("cafe0001", device)[0] == 403
    assert server.ask("cafe0001", new)[0] == 200


def test_owner_and_counter_rules_still_hold(server, device):
    server.publish("cafe0001", 5, "ok", _raw_public(device))
    assert server.publish("cafe0001", 6, "revoked", admin=server.stranger) == 403
    assert server.publish("cafe0001", 5, "revoked") == 409
    assert server.handler.db.record_of("cafe0001")["state"] == "ok"


# -- администратор спрашивает -------------------------------------------------
def test_the_owner_learns_a_medium_that_never_called(server, device):
    server.publish("cafe0001", 1, "revoked", _raw_public(device))
    status, body, _ = server.query("cafe0001")
    assert status == 200
    assert body == b"poa-record cafe0001 revoked - - -\n"


def test_nobody_but_the_owner_learns_about_a_medium(server, device):
    server.publish("cafe0001", 1, "ok", _raw_public(device))
    assert server.query("cafe0001", admin=server.stranger)[0] == 403
    assert server.query("cafe0001", moment=int(time.time()) - 301)[0] == 403
    assert server.query("cafe0001", moment=int(time.time()) + 301)[0] == 403
    unknown = ed25519.Ed25519PrivateKey.generate()
    assert server.query("cafe0001", admin=unknown)[0] == 403
    # Подпись под другим временем, чем в заголовке.
    moment = int(time.time())
    signature = server.admin.sign(f"poa-query cafe0001 {moment - 1}".encode("ascii"))
    status, _, _ = server.request("GET", "/admin/media/cafe0001", {
        "X-POA-Key-Id": srv.fingerprint(server.admin.public_key()),
        "X-POA-Time": str(moment),
        "X-POA-Signature": base64.b64encode(signature).decode("ascii"),
    })
    assert status == 403


def test_an_unpublished_medium_is_not_found(server):
    assert server.query("beef0001")[0] == 404


def test_the_application_client_speaks_the_same_protocol(server, device):
    """Клиент приложения против настоящего сервера: форма команды и разбор ответа
    проверены с обеих сторон, а не каждая против своих ожиданий."""
    seed = b"\x09" * 32
    public = serialization.load_pem_public_key(
        revocation_client.public_key_pem(seed).encode("ascii"))
    server.handler.admins[revocation_client.key_id(seed)] = public

    said = revocation_client.publish_state(server.url, seed, "cafe0003", "ok", 1,
                                           device_public_key_b64=_raw_public(device))
    assert "заведён" in said
    assert revocation_client.query_media(server.url, seed, "cafe0003") == \
        revocation_client.MediaStatus("ok")
    server.ask("cafe0003", device)
    status = revocation_client.query_media(server.url, seed, "cafe0003")
    assert (status.version, status.machine) == ("1.1.0", "ab12cd34")
    assert status.last_seen

    with pytest.raises(revocation_client.RevocationServerError) as error:
        revocation_client.publish_state(server.url, seed, "cafe0004", "ok", 1)
    assert "до 1.1.0" in str(error.value)


# -- мелочи -------------------------------------------------------------------
def test_one_enrollment_code_admits_exactly_one_administrator(server):
    """Два одновременных знакомства с одним кодом: проходит ровно одно."""
    for _ in range(5):
        code = secrets.token_hex(16)
        (server.root / "enrollment-code").write_text(code + "\n", encoding="ascii")
        barrier = threading.Barrier(2)
        results = []

        def enroll():
            pem = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            barrier.wait()
            results.append(server.request("POST", "/admin/enroll",
                                          {"X-POA-Enrollment": code}, pem)[0])

        threads = [threading.Thread(target=enroll) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == [201, 403]
        assert not (server.root / "enrollment-code").exists()


def test_a_bad_body_does_not_burn_the_code(server):
    (server.root / "enrollment-code").write_text("c0de\n", encoding="ascii")
    status, _, _ = server.request("POST", "/admin/enroll", {"X-POA-Enrollment": "c0de"},
                                  b"not a key")
    assert status == 400
    assert (server.root / "enrollment-code").exists()


@pytest.mark.parametrize("length", ["abc", "-5", "", "1e3", "²"])
@pytest.mark.parametrize("path", ["/admin/media/cafe0001", "/admin/enroll"])
def test_a_crooked_content_length_is_400_not_500(server, path, length):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.putrequest("POST", path)
        if length:
            conn.putheader("Content-Length", length.encode("utf-8"))
        conn.putheader("X-POA-Enrollment", "whatever")
        conn.endheaders()
        assert conn.getresponse().status == 400
    finally:
        conn.close()


def test_a_database_of_1_0_0_is_migrated_without_losing_records(tmp_path, device):
    path = tmp_path / "media.sqlite"
    admin = ed25519.Ed25519PrivateKey.generate()
    owner = srv.fingerprint(admin.public_key())
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE media (media_id TEXT PRIMARY KEY, owner TEXT NOT NULL,"
            " state TEXT NOT NULL, counter INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute("INSERT INTO media VALUES ('cafe0001', ?, 'revoked', 7, '2026-09-13')",
                   (owner,))

    database = srv.Database(path)
    srv.Database(path)  # повторный запуск службы -- без ошибок

    record = database.record_of("cafe0001")
    assert (record["owner"], record["state"], record["counter"]) == (owner, "revoked", 7)
    assert record["device_key"] is None and record["last_seen"] is None

    # Состояние отзыва сохраняется при миграции и дополнении записи ключом.
    assert database.put("cafe0001", owner, "ok", 8, _raw_public(device)) == 410
    assert database.put("cafe0001", owner, "revoked", 8, _raw_public(device)) == 204
    assert database.record_of("cafe0001")["device_key"] == _raw_public(device)
