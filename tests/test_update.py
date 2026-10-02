"""Э9: обновление образа в неактивный слот -- проверка до записи, запись атомарно."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
import shutil
import sys
import time

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
sys.path.insert(0, str(RUNTIME))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


trust = _load("poa_trust")
gost = sys.modules["poa_gost"]
update = _load("poa_update")

FILES = {"rootfs.squashfs": b"image" * 1000, "rootfs.verity": b"tree" * 10,
         "vmlinuz": b"kernel" * 100, "initrd.img": b"initrd" * 100}
KEYS = {"rootfs.squashfs": "image", "rootfs.verity": "verity", "vmlinuz": "kernel", "initrd.img": "initrd"}


class Server:
    """Отдаёт файлы выпуска так, как Endpoint.download их получает; при
    `cut` первая попытка обрывается на трети -- как медленная флешка за nginx."""

    def __init__(self, root: pathlib.Path, cut: bool = False):
        self.root = root
        self.cut = cut
        self.calls = 0

    def download(self, path, dest, progress=None):
        self.calls += 1
        name = path.rsplit("/", 1)[-1]
        data = (self.root / name).read_bytes()
        dest = pathlib.Path(dest)
        have = dest.stat().st_size if dest.exists() else 0
        piece = data[have:]
        if self.cut and have == 0 and name != "release.json":
            piece = piece[: len(piece) // 3]
        with open(dest, "ab") as handle:
            handle.write(piece)
        if progress:
            progress(dest.stat().st_size)
        return dest.stat().st_size


def _payload(serial, family="debian", files=FILES):
    body = {"kind": "poa-release", "format": 1, "serial": serial, "name": "x", "family": family,
            "arch": "amd64", "layout": 3, "verity_root_hash": "ab" * 32}
    for name, data in files.items():
        body[KEYS[name]] = {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    return body


@pytest.fixture
def world(tmp_path):
    root = trust.GostSigner(gost.new_private_key(gost.TC26_256_A), "main")
    working = ed25519.Ed25519PrivateKey.generate()
    cert = trust.certify(root, trust.raw_public(working), time.time() + 3600, 1)
    boot, images, server_dir = tmp_path / "boot", tmp_path / "images", tmp_path / "srv"
    (boot / "slot-a").mkdir(parents=True)
    (images / "slot-a").mkdir(parents=True)
    server_dir.mkdir()
    (boot / "slot-a" / "release.json").write_text(json.dumps({"release": _payload(10)}))
    for name, data in FILES.items():
        (server_dir / name).write_bytes(data)
    calls = []

    def publish(payload):
        (server_dir / "release.json").write_text(json.dumps(trust.sign_document(working, cert, "release", payload)))

    updater = update.Updater(Server(server_dir), [root.entry], "grub-editenv",
                             run=lambda argv, check=False: calls.append(argv),
                             images=str(images), boot=str(boot), state=str(tmp_path / "update.json"),
                             cmdline="BOOT_IMAGE=/slot-a/vmlinuz poa.slot=a poa.serial=10")
    updater.sleep = lambda seconds: None
    return updater, publish, boot, images, server_dir, calls, (root, working)


INFO = {"distribution": "debian-kiosk", "serial": 11}


def test_a_newer_signed_release_lands_in_the_other_slot_and_is_offered_to_grub(world):
    updater, publish, boot, images, _srv, calls, _keys = world
    publish(_payload(11))
    assert updater.apply(INFO) == "b"
    assert (images / "slot-b" / "rootfs.squashfs").read_bytes() == FILES["rootfs.squashfs"]
    assert (boot / "slot-b" / "vmlinuz").exists() and (boot / "slot-b" / "initrd.img").exists()
    assert "set poa_serial=11" in (boot / "slot-b" / "slot.cfg").read_text()
    assert ["grub-editenv", str(boot / "efi/EFI/poa/grubenv"), "set", "poa_try=b", "poa_tries=3"] in calls
    assert json.loads((pathlib.Path(updater.state)).read_text())["state"] == "ready"


def test_a_corrupted_file_leaves_grub_untouched(world):
    updater, publish, boot, _images, srv, calls, _keys = world
    publish(_payload(11))
    # Та же длина, другое содержимое: подмену ловит именно sha256.
    (srv / "vmlinuz").write_bytes(b"X" * len(FILES["vmlinuz"]))
    with pytest.raises(update.UpdateError, match="sha256"):
        updater.apply(INFO)
    assert not (boot / "slot-b" / "vmlinuz").exists()
    assert not any("poa_try=b" in " ".join(c) for c in calls)
    assert json.loads(pathlib.Path(updater.state).read_text())["state"] == "failed"


def test_a_release_signed_by_a_stranger_is_refused_before_any_download(world):
    updater, _publish, boot, images, srv, calls, _keys = world
    stranger = trust.GostSigner(gost.new_private_key(gost.TC26_256_A), "main")
    working = ed25519.Ed25519PrivateKey.generate()
    cert = trust.certify(stranger, trust.raw_public(working), time.time() + 3600, 1)
    (srv / "release.json").write_text(json.dumps(trust.sign_document(working, cert, "release", _payload(11))))
    with pytest.raises(update.UpdateError, match="подписи"):
        updater.apply(INFO)
    assert not list((images / "slot-b").glob("*")) if (images / "slot-b").exists() else True


def test_another_family_or_an_old_number_is_refused(world):
    updater, publish, *_ = world
    publish(_payload(11, family="redos"))
    with pytest.raises(update.UpdateError, match="несовместим"):
        updater.apply(INFO)
    assert updater.apply({"distribution": "x", "serial": 9}) == ""   # не новее -- ничего не делать


def test_the_window_text_follows_the_update():
    launch = _load("poa_launch")
    assert "Не выключайте" in launch.update_text({"state": "downloading", "done": 50, "total": 100})
    assert "50%" in launch.update_text({"state": "downloading", "done": 50, "total": 100})
    assert "следующем включении" in launch.update_text({"state": "ready"})
    assert launch.update_text({}) == ""


def test_a_broken_download_is_resumed_from_where_it_stopped(world):
    updater, publish, boot, images, srv, calls, _keys = world
    updater.endpoint = Server(srv, cut=True)
    publish(_payload(11))
    assert updater.apply(INFO) == "b"
    assert (images / "slot-b" / "rootfs.squashfs").read_bytes() == FILES["rootfs.squashfs"]
    # release.json + по две попытки на каждый из четырёх файлов
    assert updater.endpoint.calls == 1 + 2 * 4


def test_many_small_pieces_are_not_counted_as_failures(world, monkeypatch):
    """Обрыв с продвижением -- не отказ: 100 кусков при пределе в 30 попыток."""
    updater, publish, _boot, images, srv, _calls, _keys = world

    class Trickle(Server):
        def download(self, path, dest, progress=None):
            self.calls += 1
            data = (self.root / path.rsplit("/", 1)[-1]).read_bytes()
            dest = pathlib.Path(dest)
            have = dest.stat().st_size if dest.exists() else 0
            step = len(data) if path.endswith("release.json") else max(1, len(data) // 100)
            with open(dest, "ab") as handle:
                handle.write(data[have:have + step])
            return dest.stat().st_size

    monkeypatch.setattr(update, "ATTEMPTS", 30)
    updater.endpoint = Trickle(srv)
    publish(_payload(11))
    assert updater.apply(INFO) == "b"
    assert (images / "slot-b" / "rootfs.squashfs").read_bytes() == FILES["rootfs.squashfs"]


def test_the_running_slot_is_checked_against_its_signed_release(world):
    """Э5: подмена ядра или slot.cfg (хеш образа) на флешке видна после загрузки."""
    updater, publish, boot, _images, srv, _calls, (root, _working) = world
    publish(_payload(11))
    updater.apply(INFO)
    good = f"BOOT_IMAGE=/slot-b/vmlinuz poa.slot=b poa.roothash={'ab' * 32} poa.serial=11"
    assert update.check_running_slot([root.entry], str(boot), good) == []
    bad_hash = good.replace("ab" * 32, "cd" * 32)
    assert any("корневым хешем" in p for p in update.check_running_slot([root.entry], str(boot), bad_hash))
    (boot / "slot-b" / "vmlinuz").write_bytes(b"evil")
    assert any("vmlinuz" in p for p in update.check_running_slot([root.entry], str(boot), good))
    assert update.check_running_slot([], str(boot), good) == []   # нет корня -- нечем сверять

