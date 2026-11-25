"""Э14: сетевой экран носителя -- правила и стадии."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
sys.path.insert(0, str(RUNTIME))
_spec = importlib.util.spec_from_file_location("poa_firewall", RUNTIME / "poa_firewall.py")
fw = importlib.util.module_from_spec(_spec)
sys.modules["poa_firewall"] = fw
_spec.loader.exec_module(fw)


class Recorder:
    def __init__(self):
        self.scripts = []

    def __call__(self, argv, input="", text=True, check=True):
        self.scripts.append(input)


def test_everything_is_closed_by_default():
    assert "type filter hook output priority 0; policy drop;" in fw.RULESET
    assert "type filter hook input priority 0; policy drop;" in fw.RULESET
    assert "type filter hook forward priority 0; policy drop;" in fw.RULESET


def test_targets_are_reachable_only_through_the_tunnel():
    assert 'oifname . ip daddr . tcp dport @target4 accept' in fw.RULESET
    assert 'iifname . ip saddr . tcp sport @target4 accept' in fw.RULESET
    assert "ct state established,related accept" not in fw.RULESET


def test_stages_fill_and_empty_the_sets(monkeypatch):
    monkeypatch.setattr(fw, "resolve4", lambda host: {"vpn.example": ["198.51.100.1"],
                                                      "10.0.0.5": ["10.0.0.5"]}.get(host, []))
    rec = Recorder()
    firewall = fw.Firewall(runner=rec)
    firewall.open_session([{"host": "vpn.example", "port": 1194, "proto": "udp"}],
                          {"host": "10.0.0.5", "port": 3389}, "tun123", fw.boottime() + 60, 123)
    assert "198.51.100.1 . udp . 1194 timeout " in rec.scripts[0]
    assert '"tun123" . 10.0.0.5 . 3389 timeout ' in rec.scripts[0]
    assert "offset 123 @requests jump work_in" in rec.scripts[0]
    firewall.close_session()
    assert rec.scripts[1].splitlines() == ["flush set inet poa gateway4", "flush set inet poa target4",
                                           "flush set inet poa requests"]
    firewall.quarantine()
    assert "flush chain inet poa work_in" in rec.scripts[2]
    assert "flush chain inet poa work_out" in rec.scripts[2]


@pytest.mark.skipif(shutil.which("nft") is None, reason="нет nft")
def test_the_ruleset_is_accepted_by_nft(tmp_path):
    """Синтаксис проверяется самим nft (-c: без применения). Нужен пользователь
    poa-portal -- на носителе его заводит sysusers.d."""
    text = fw.RULESET.replace(f'meta skuid "{fw.PORTAL_USER}"', "meta skuid 0")
    result = subprocess.run(["nft", "-c", "-f", "-"], input=text, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_server_is_open_on_its_own_port_and_80_for_the_probe(monkeypatch):
    """Сервер не обязательно на 443 (стенд -- 8443): экран берёт порт из адреса."""
    rec = Recorder()
    fw.Firewall(runner=rec).allow_server("10.0.2.2", 8443)
    assert "10.0.2.2 . 80, 10.0.2.2 . 8443" in rec.scripts[-1]
    assert "ip daddr . tcp dport @server4" in fw.RULESET
