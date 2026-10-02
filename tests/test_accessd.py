"""poa-accessd: служба доступа на носителе -- против живого сервера, через сокет."""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import socket
import sys
import threading

import pytest
from cryptography.hazmat.primitives import serialization

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "src/portable_os_admin/install/access/runtime"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


harness = _load("access_server_harness3", ROOT / "tests" / "test_access_server.py")
sys.path.insert(0, str(RUNTIME))
accessd = _load("poa_accessd", RUNTIME / "poa_accessd.py")


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, check=False, capture_output=False, text=False):
        self.calls.append(argv)

        class R:
            returncode, stdout = 0, ("none\n" if argv[0] == "systemd-detect-virt" else "active\n")
        return R()


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = harness.Clock()
    monkeypatch.setattr(harness.totp, "time", clock)
    monkeypatch.setattr(accessd, "VPN_RUN", str(tmp_path / "vpn"))
    server = harness._Server(tmp_path / "srv", clock) if (tmp_path / "srv").mkdir() is None else None
    device = harness.Device(server)
    server.register(device.media, device.key)
    server.admin_cmd("vpn", remotes=[{"host": "gw.test", "port": 1194, "proto": "udp"}])
    server.admin_cmd("targets", media=device.media, targets=[{
        "id": "ws1", "name": "РМ", "type": "rdp", "host": "127.0.0.1", "port": server.port,
        "user": "u", "domain": "", "pin": "sha256:" + "0" * 64}])
    key = tmp_path / "device.key"
    key.write_bytes(device.key.private_bytes(serialization.Encoding.PEM,
                                             serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    raw = server.sign_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                    serialization.PublicFormat.Raw)
    config = {"endpoint": f"http://127.0.0.1:{server.port}", "media": device.media,
              "device_key": str(key), "server_sign_keys": [base64.b64encode(raw).decode()],
              "state": str(tmp_path / "access.json"), "gateway_name": "gw.test",
              "events_dir": str(tmp_path / "events")}
    runner = FakeRun()
    service = accessd.Service(config, runner=runner, clock=lambda: clock.now)
    sock = str(tmp_path / "access.sock")
    threading.Thread(target=accessd.serve, args=(service, sock), daemon=True).start()
    for _ in range(100):
        if pathlib.Path(sock).exists():
            break
        threading.Event().wait(0.02)
    yield server, service, sock, runner, tmp_path
    server.close()


def ask(sock: str, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.connect(sock)
        conn.sendall(json.dumps(request).encode() + b"\n")
        return json.loads(conn.makefile("rb").readline())


def test_activation_then_login_brings_the_vpn_up(world):
    server, service, sock, runner, tmp = world
    answer = ask(sock, {"cmd": "activate", "code": server.issue_code(service.config["media"])})
    secret = answer["otpauth"].split("secret=")[1].split("&")[0]
    assert "secret" not in json.loads(pathlib.Path(service.config["state"]).read_text()), \
        "семя TOTP не должно оставаться на носителе"
    server.clock.now += 30
    answer = ask(sock, {"cmd": "login", "totp": harness.totp.code_at(secret, int(server.clock.now // 30))})
    assert answer["targets"][0]["id"] == "ws1", answer
    conf = next((tmp / "vpn").glob("*/client.conf")).read_text()
    assert "<key>" in conf and "BEGIN PRIVATE KEY" in conf and "verify-x509-name gw.test name" in conf
    assert "remote gw.test 1194 udp" in conf and "tls-version-min 1.3" in conf
    assert any(c[:2] == ["systemctl", "start"] for c in runner.calls)
    status = ask(sock, {"cmd": "status"})
    assert status["activated"] is True and status["vpn"] == "up"
    assert ask(sock, {"cmd": "check"})["reachability"] == {"ws1": "up"}
    ask(sock, {"cmd": "disconnect"})
    assert not list((tmp / "vpn").glob("*/client.conf")), "ключ VPN должен исчезнуть с отключением"


def test_login_ships_the_event_queue_and_prunes_what_was_acknowledged(world):
    server, service, sock, runner, tmp = world
    import poa_events
    queue = poa_events.Queue(service.config["events_dir"])
    queue.append("D02", {"vid": "0781"})
    otp = ask(sock, {"cmd": "activate", "code": server.issue_code(service.config["media"])})["otpauth"]
    secret = otp.split("secret=")[1].split("&")[0]
    server.clock.now += 30
    ask(sock, {"cmd": "login", "totp": harness.totp.code_at(secret, int(server.clock.now // 30))})
    assert queue.pending() == []
    with server.handler.store.tx() as db:
        codes = [r["code"] for r in db.execute("SELECT code FROM device_events")]
    assert codes == ["D02"]


def test_server_refusal_reaches_the_window_as_a_reason(world):
    server, service, sock, runner, tmp = world
    ask(sock, {"cmd": "activate", "code": server.issue_code(service.config["media"])})
    assert ask(sock, {"cmd": "login", "totp": "000000"})["error"] == "totp"


def test_a_foreign_user_is_refused(world, monkeypatch):
    server, service, sock, runner, tmp = world
    monkeypatch.setattr(accessd, "peer_uid", lambda conn: 4242)
    left, right = socket.socketpair()
    threading.Thread(target=accessd.handle, args=(service, right, 1000), daemon=True).start()
    # Отказ уходит до чтения запроса -- клиенту остаётся только прочитать его.
    assert json.loads(left.makefile("rb").readline()) == {"error": "forbidden"}


def test_clock_is_corrected_from_the_server_when_far_off(world):
    server, service, sock, runner, tmp = world
    service.clock = lambda: 0
    service._fix_clock(1_790_000_000)
    assert ["date", "-u", "-s", "@1790000000"] in runner.calls


def test_probe_tells_refused_from_up():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    assert accessd.probe("127.0.0.1", port) == "up"
    listener.close()
    assert accessd.probe("127.0.0.1", port) == "refused"


def _descriptor(root, working, config, serial):
    trust = sys.modules.get("poa_trust") or _load("poa_trust", RUNTIME / "poa_trust.py")
    cert = trust.certify(root, trust.raw_public(working), 2**40, 1)
    payload = {"serial": serial, "endpoints": [config["endpoint"]], "tls_spki": [],
               "server_sign_keys": config["server_sign_keys"], "fallback": []}
    return trust.sign_document(working, cert, "descriptor", payload)


def test_descriptor_is_taken_only_if_signed_by_the_org_root_and_newer(world):
    from cryptography.hazmat.primitives.asymmetric import ed25519

    server, service, _sock, _runner, tmp_path = world
    trust = _load("poa_trust", RUNTIME / "poa_trust.py")
    root, working = ed25519.Ed25519PrivateKey.generate(), ed25519.Ed25519PrivateKey.generate()
    service.config["root_public"] = trust.b64(trust.raw_public(root))
    service.config["descriptor"] = str(tmp_path / "descriptor.json")
    assert service.refresh_descriptor() is False          # сервер ещё не публиковал

    server.admin_cmd("descriptor", document=_descriptor(root, working, service.config, 5))
    assert service.refresh_descriptor() is True
    assert service.descriptor()["serial"] == 5
    assert service.server()["endpoint"] == service.config["endpoint"]

    # Подлинный, но старый -- откат на перехваченный адрес -- не принимается.
    server.admin_cmd("descriptor", document=_descriptor(root, working, service.config, 4))
    assert service.refresh_descriptor() is False
    # Подписанный чужим корнем -- тоже.
    stranger = ed25519.Ed25519PrivateKey.generate()
    server.admin_cmd("descriptor", document=_descriptor(stranger, working, service.config, 9))
    assert service.refresh_descriptor() is False
    assert service.descriptor()["serial"] == 5


def test_without_org_root_descriptors_are_ignored(world):
    _server, service, *_ = world
    assert service.refresh_descriptor() is False
