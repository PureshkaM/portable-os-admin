"""Окно «Подключение» и запуск клиентов (Э8)."""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import sys
import threading

import pytest

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
sys.path.insert(0, str(RUNTIME))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, RUNTIME / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


launch = _load("poa_launch")
RDP = {"id": "ws1", "type": "rdp", "host": "10.0.0.5", "port": 3389, "user": "ivanov",
       "domain": "CORP", "pin": "sha256:" + "ab" * 32, "smartcard": False}


def test_rdp_password_goes_through_stdin_not_argv():
    argv, needs_password = launch.client_command(RDP)
    assert needs_password and "/from-stdin" in argv
    assert not any("/p:" in a for a in argv)
    assert "/f" in argv and "/sec:nla" in argv and "/d:CORP" in argv
    assert f"/cert:fingerprint:sha256:{'ab' * 32}" in argv
    assert not any(a.startswith(("/drive", "/usb", "+drives")) for a in argv)
    assert "/smartcard" not in argv
    assert "/smartcard" in launch.client_command({**RDP, "smartcard": True})[0]


def test_ssh_and_vnc_open_our_own_windows():
    for kind in ("ssh", "vnc"):
        argv, needs_password = launch.client_command({**RDP, "type": kind, "port": 22})
        assert argv[1].endswith(f"poa_{kind}.py") and not needs_password


def test_ssh_window_disables_escapes_forwarding_and_pins_the_host_key():
    ssh = _load("poa_ssh") if _has_vte() else None
    if ssh is None:
        pytest.skip("нет VTE")
    argv = ssh.ssh_argv({**RDP, "type": "ssh", "port": 2222}, "/tmp/kh")
    joined = " ".join(argv)
    for option in ("EscapeChar=none", "ForwardAgent=no", "ForwardX11=no",
                   "PermitLocalCommand=no", "ClearAllForwardings=yes",
                   "StrictHostKeyChecking=yes", "UserKnownHostsFile=/tmp/kh"):
        assert option in joined
    line = ssh.known_hosts_line({**RDP, "port": 2222, "pin": "ssh-ed25519 AAAA"})
    assert line == "[10.0.0.5]:2222 ssh-ed25519 AAAA\n"


def test_messages_are_human():
    assert "Неверный код" in launch.explain({"error": "totp"})
    assert "Возможна атака" in launch.explain({"error": "tls_pin"})
    assert "Нет связи" in launch.explain({"error": "network: timed out"})
    assert launch.reachability_text("silent").startswith("Рабочее место не отвечает")


def _has_vte() -> bool:
    try:
        import gi
        gi.require_version("Vte", "2.91")
        from gi.repository import Vte  # noqa: F401
        return True
    except (ImportError, ValueError):
        return False


def _fake_service(path: str, answers: dict) -> None:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(8)

    def loop():
        while True:
            conn, _ = listener.accept()
            with conn:
                request = json.loads(conn.makefile("rb").readline())
                conn.sendall(json.dumps(answers.get(request["cmd"], {})).encode() + b"\n")
    threading.Thread(target=loop, daemon=True).start()


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="нужен экран (xvfb-run)")
@pytest.mark.parametrize("status,page", [
    ({"authorized": False}, "wait"),
    ({"authorized": True, "activated": False}, "activate"),
    ({"authorized": True, "activated": True}, "totp"),
])
def test_window_picks_the_page_from_the_service_state(tmp_path, monkeypatch, status, page):
    path = str(tmp_path / "s.sock")
    _fake_service(path, {"status": status})
    monkeypatch.setattr(launch, "SOCKET", path)
    monkeypatch.setattr(launch.ask, "__defaults__", (path, 120.0))
    connect = _load("poa_connect")
    window = connect.ConnectWindow()
    assert window.stack.get_visible_child_name() == page
    window.destroy()
