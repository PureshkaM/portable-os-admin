"""Клиент сервера доступа на носителе (`runtime/poa_client.py`) против живого сервера."""
from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import ssl
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module      # dataclasses ищут свой модуль в sys.modules
    spec.loader.exec_module(module)
    return module


harness = _load("access_server_harness2", ROOT / "tests" / "test_access_server.py")
rt = _load("poa_client", ROOT / "src/portable_os_admin/install/access/runtime/poa_client.py")


@pytest.fixture
def world(tmp_path, monkeypatch):
    clock = harness.Clock()
    monkeypatch.setattr(harness.totp, "time", clock)
    server = harness._Server(tmp_path, clock)
    device = harness.Device(server)
    server.register(device.media, device.key)
    key_path = tmp_path / "device.key"
    key_path.write_bytes(device.key.private_bytes(serialization.Encoding.PEM,
                                                  serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    server_key = server.sign_key.public_key().public_bytes(serialization.Encoding.Raw,
                                                           serialization.PublicFormat.Raw)
    client = rt.Client(rt.Endpoint(f"http://127.0.0.1:{server.port}"), device.media,
                       rt.FileSigner(str(key_path)), (server_key,))
    state = rt.State(str(tmp_path / "access.json"))
    yield server, client, state
    server.close()


def _vpn():
    key = ec.generate_private_key(ec.SECP256R1())
    return {"provider": "openvpn", "public_key": key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()}


def _activate(server, client, state):
    grant = client.activate(server.issue_code(client.media), {"virt": "none"})
    state.save({"link": grant["link"], "totp": grant["totp"]})
    return grant["totp"]["secret"]


def test_activation_and_two_logins_keep_the_chain(world):
    server, client, state = world
    secret = _activate(server, client, state)
    for _ in range(2):
        server.clock.now += 30
        code = harness.totp.code_at(secret, int(server.clock.now // 30))
        grant = client.login(state, code, {"virt": "none", "serial": 1}, _vpn())
        assert "BEGIN CERTIFICATE" in grant["vpn"]["cert"]
    assert json.loads(pathlib.Path(state.path).read_text())["link"]


def test_a_grant_signed_by_another_key_is_rejected(world):
    server, client, state = world
    secret = _activate(server, client, state)
    client.server_keys = (b"\x00" * 32,)
    server.clock.now += 30
    with pytest.raises(rt.AccessError, match="grant_signature"):
        client.login(state, harness.totp.code_at(secret, int(server.clock.now // 30)),
                     {"virt": "none"}, _vpn())


def test_refusal_carries_the_server_reason(world):
    server, client, state = world
    _activate(server, client, state)
    with pytest.raises(rt.AccessError) as caught:
        client.login(state, "000000", {"virt": "none"}, _vpn())
    assert (caught.value.code, caught.value.reason) == (401, "totp")


def test_plain_http_is_refused_except_loopback_in_tests():
    with pytest.raises(rt.AccessError, match="insecure"):
        rt.Endpoint("http://access.example.org").request("GET", "/v3/crl")


# -- TLS с закреплением ----------------------------------------------------------
def _self_signed(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "access.test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "tls.crt", tmp_path / "tls.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    pin = rt.spki_sha256(cert.public_bytes(serialization.Encoding.DER))
    return str(cert_path), str(key_path), pin


def test_pinned_tls_passes_and_a_foreign_key_is_cut_off(world, tmp_path):
    server, client, state = world
    cert, key, pin = _self_signed(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.httpd.socket = context.wrap_socket(server.httpd.socket, server_side=True)
    url = f"https://127.0.0.1:{server.port}"
    status, _ = rt.Endpoint(url, (pin,)).request("GET", "/v3/crl")
    assert status == 200
    with pytest.raises(rt.PinMismatch):
        rt.Endpoint(url, ("0" * 64,)).request("GET", "/v3/crl")
