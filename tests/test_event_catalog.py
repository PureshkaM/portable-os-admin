"""Каталог событий сервера и приложение В модели угроз -- одно и то же."""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging" / "revocation-server"))
from poa_access import catalog  # noqa: E402

_spec = importlib.util.spec_from_file_location("harness_cat", ROOT / "tests" / "test_access_server.py")
harness = importlib.util.module_from_spec(_spec)
sys.modules["harness_cat"] = harness
_spec.loader.exec_module(harness)


def test_catalog_matches_the_threat_model_appendix():
    doc = (ROOT / "docs" / "THREAT-MODEL.md").read_text(encoding="utf-8")
    part = doc[doc.index("### Носитель\n"):doc.index("### Журнал сервера")]
    documented = set(re.findall(r"^\| ([A-Z]\d{2}) \|", part, re.M))
    assert documented == set(catalog.EVENTS), "каталог разошёлся с моделью угроз"


def test_record_reads_as_media_colon_meaning():
    line = catalog.describe("0a1b2c3d-11223344", "C03", {"addr": "198.51.100.7"})
    assert line.startswith("0a1b2c3d-11223344: [К] TLS-отпечаток сервера не совпал")
    assert "addr=198.51.100.7" in line


def test_admin_log_shows_decoded_device_events(tmp_path, monkeypatch):
    clock = harness.Clock()
    monkeypatch.setattr(harness.totp, "time", clock)
    server = harness._Server(tmp_path, clock)
    try:
        device = harness.Device(server)
        server.register(device.media, device.key)
        device.post("/v3/events", {"catalog": 1, "events": [{"seq": 1, "code": "D02", "p": {"vid": "0781"}}]})
        status, log = server.admin_cmd("log", since=0)
        assert status == 200
        assert log["device"][0]["text"] == f"{device.media}: [В] Попытка подключить накопитель (vid=0781)"
    finally:
        server.close()
