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


# -- выпуски и группы через access_actions (Э7) -----------------------------------------
def _container(client):
    from types import SimpleNamespace

    events = []
    return SimpleNamespace(
        data=SimpleNamespace(revocation_server_url=client.url),
        admin_authority_secret=client.seed,
        record_event=lambda *args: events.append(args), events=events)


def _dist(tmp_path, serial, family="debian", signed=True):
    import json

    directory = tmp_path / f"dist-{serial}-{family}-{'s' if signed else 'u'}"
    directory.mkdir()
    for name in ("rootfs.squashfs", "rootfs.verity", "vmlinuz", "initrd.img"):
        (directory / name).write_bytes(name.encode() * 10)
    release = {"serial": serial, "family": family, "arch": "amd64", "layout": 3}
    document = {"release": release, **({"signer": {"cert": {}}} if signed else {})}
    (directory / "release.json").write_text(json.dumps(document))
    return str(directory)


def test_a_distribution_is_published_as_a_release_line(setup, tmp_path):
    from portable_os_admin.admin import access_actions

    _server, client = setup
    container = _container(client)
    with pytest.raises(access_actions.AccessActionError, match="не подписан"):
        access_actions.publish_distribution(container, _dist(tmp_path, 5, signed=False), "kiosk")
    steps = []
    assert access_actions.publish_distribution(
        container, _dist(tmp_path, 5), "kiosk", lambda i, n, f: steps.append((i, n))) == 5
    assert steps[-1] == (4, 5)
    access_actions.set_min_serial(container, "kiosk", 5)
    line = access_actions.overview(container)["distributions"][0]
    assert (line["id"], line["family"], line["releases"], line["min_serial"]) == \
        ("kiosk", "debian", [5], 5)
    with pytest.raises(access_actions.AccessActionError, match="не новее"):
        access_actions.publish_distribution(container, _dist(tmp_path, 4), "kiosk")
    with pytest.raises(access_actions.AccessActionError, match="не подходит"):
        access_actions.publish_distribution(container, _dist(tmp_path, 6, family="redos"), "kiosk")
    # Публикация следующего выпуска не сбрасывает требование «не ниже».
    access_actions.publish_distribution(container, _dist(tmp_path, 6), "kiosk")
    line = access_actions.overview(container)["distributions"][0]
    assert line["releases"] == [6, 5] and line["min_serial"] == 5


def test_groups_assignment_and_vm_policy_round_trip(setup):
    from portable_os_admin.admin import access_actions

    server, client = setup
    device = harness.Device(server)
    server.register(device.media, device.key)
    container = _container(client)
    client.distribution("kiosk", "debian", "amd64", 3)
    access_actions.create_group(container, "dept", "root")
    access_actions.assign(container, "root", "kiosk")
    access_actions.set_vm_policy(container, "dept", True)
    access_actions.move_media(container, device.media, "dept")
    data = access_actions.overview(container)
    dept = next(g for g in data["groups"] if g["id"] == "dept")
    assert (dept["effective"], dept["allow_vm"]) == ("kiosk", True)
    assert {"media_id": device.media, "group": "dept"} in data["media"]
    access_actions.set_vm_policy(container, "dept", None)
    assert next(g for g in access_actions.overview(container)["groups"]
                if g["id"] == "dept")["allow_vm"] is None
    assert len(container.events) == 5


def test_target_pins_are_normalized_or_refused(tmp_path):
    """Отпечаток в том виде, в каком его ждёт носитель: PEM, вставленный в
    однострочное поле, собирается заново; .pub теряет комментарий."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from portable_os_admin.admin import access_actions

    hexa = "37b00521e24d56161219debeca8e4c7b481af979951090c67834319ceafa4204"
    colon = ":".join(hexa[i:i + 2] for i in range(0, 64, 2)).upper()
    assert access_actions.normalize_pin("rdp", colon) == f"sha256:{hexa}"
    with pytest.raises(access_actions.AccessActionError):
        access_actions.normalize_pin("rdp", "sha256:abc")

    assert access_actions.normalize_pin("ssh", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 root@debian\n") == \
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5"
    with pytest.raises(access_actions.AccessActionError):
        access_actions.normalize_pin("ssh", "просто текст")

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "10.99.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    flattened = pem.replace("\n", " ")
    restored = access_actions.normalize_pin("vnc", flattened)
    assert x509.load_pem_x509_certificate(restored.encode()) == cert
    with pytest.raises(access_actions.AccessActionError, match="не разбирается"):
        access_actions.normalize_pin("vnc", pem.replace(pem.splitlines()[2], "A" * 64))


def test_command_numbers_grow_across_client_objects(monkeypatch):
    """Два клиента в одну миллисекунду -- разные номера: access_actions
    создаёт клиента на каждое действие."""
    monkeypatch.setattr(access_client.time, "time_ns", lambda: 5_000_000_000_000)
    first = access_client.AccessClient("http://x", b"s" * 32)._counter()
    second = access_client.AccessClient("http://x", b"s" * 32)._counter()
    assert second > first
