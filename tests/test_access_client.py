"""Клиент программы администратора к серверу доступа v3 -- против живого сервера."""
from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import secrets

import pytest

from portable_os_admin.admin import access_client, revocation_client

_spec = importlib.util.spec_from_file_location(
    "access_server_harness", pathlib.Path(__file__).with_name("test_access_server.py"))
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    clock = harness.Clock()
    monkeypatch.setattr(harness.totp, "time", clock)
    server = harness._Server(tmp_path, clock)
    seed = secrets.token_bytes(32)
    key = revocation_client.signing_key(seed)
    server.admin, server.admin_id = key, revocation_client.key_id(seed)
    server.handler.admins = {server.admin_id: key.public_key()}
    client = access_client.AccessClient(f"http://127.0.0.1:{server.port}", seed)
    yield server, client
    server.close()


def test_activation_code_reaches_the_server_only_as_a_hash(setup):
    server, client = setup
    device = harness.Device(server)
    server.register(device.media, device.key)
    code = client.issue_activation(device.media)
    assert len(code) == 11 and code[5] == "-"
    with server.handler.store.tx() as db:
        stored = [r["code_hash"] for r in db.execute("SELECT code_hash FROM activations")]
    assert stored == [hashlib.sha256(code.replace("-", "").encode()).hexdigest()]
    status, _ = device.activate(code)
    assert status == 200


def test_commands_in_quick_succession_get_increasing_numbers(setup):
    server, client = setup
    for name in ("a", "b", "c"):
        client.group(name)
    client.distribution("d1", "debian", "amd64", 2)
    client.assign("a", "d1")
    client.allow_vm("b", True)


def test_release_is_uploaded_and_published(setup, tmp_path):
    server, client = setup
    client.distribution("d1", "redos", "x86_64", 2)
    files = []
    for name, data in (("release.json", b"{}"), ("system.squashfs", secrets.token_bytes(3000))):
        path = tmp_path / name
        path.write_bytes(data)
        files.append(str(path))
    client.publish_release("d1", 7, files)
    status, raw, _ = server.request("GET", "/v3/release/d1/7/release.json")
    assert (status, raw) == (200, b"{}")


def test_refusal_is_explained(setup):
    server, client = setup
    with pytest.raises(access_client.AccessServerError, match="нет"):
        client.unblock("0a1b2c3d-99999999")
    stranger = access_client.AccessClient(client.url, secrets.token_bytes(32))
    with pytest.raises(access_client.AccessServerError, match="не допустил"):
        stranger.group("x")
