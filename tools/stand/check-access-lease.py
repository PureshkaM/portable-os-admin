"""Проверка действующего TCP-потока с наблюдением на стороне рабочего места.

На Linux от root: unshare --net python3 tools/stand/check-access-lease.py
Все интерфейсы и правила создаются в двух временных сетевых пространствах.
"""
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[2] / "src/portable_os_admin/install/access/runtime"
sys.path.insert(0, str(RUNTIME))
import poa_firewall as fw  # noqa: E402


def run(*args):
    subprocess.run(args, check=True)


def echo(log):
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while True:
                data = self.request.recv(1024)
                if not data:
                    break
                with log.open("ab") as out:
                    out.write(data)
                self.request.sendall(data)

    with socketserver.ThreadingTCPServer(("0.0.0.0", 3389), Echo) as server:
        server.daemon_threads = True
        log.touch()
        server.serve_forever()


def check(peer, directory):
    own_ns = os.readlink("/proc/self/ns/net")
    for _ in range(100):
        if os.readlink(f"/proc/{peer.pid}/ns/net") != own_ns:
            break
        time.sleep(.02)
    else:
        raise RuntimeError("Не создано отдельное пространство рабочего места")
    ns = ["nsenter", "-t", str(peer.pid), "-n"]
    interface = "tun123abc"
    run("ip", "link", "add", interface, "type", "veth", "peer", "name", "desk0")
    run("ip", "link", "set", "desk0", "netns", str(peer.pid))
    run("ip", "addr", "add", "192.0.2.1/24", "dev", interface)
    run("ip", "link", "set", interface, "up")
    run(*ns, "ip", "addr", "add", "192.0.2.2/24", "dev", "desk0")
    run(*ns, "ip", "link", "set", "desk0", "up")
    log = directory / "received"
    server = subprocess.Popen([*ns, sys.executable, __file__, "--peer", str(log)])
    try:
        for _ in range(100):
            if log.exists():
                break
            time.sleep(.05)
        if not log.exists():
            raise RuntimeError("Рабочее место не готово")
        fw.RULESET = fw.RULESET.replace('meta skuid "poa-portal"', 'meta skuid 65533')
        chain = fw.Firewall()
        chain.load()
        chain.bind_client(65534)
        gw = [{"proto": "udp", "host": "198.51.100.5", "port": 1194}]
        desk = {"host": "192.0.2.2", "port": 3389}
        for scenario in ("close", "timeout", "quarantine"):
            token = chain.begin_request()
            ttl = 5 if scenario == "timeout" else 45
            chain.open_session(gw, desk, interface, fw.boottime() + ttl, token)
            os.seteuid(65534)
            try:
                connection = socket.create_connection((desk["host"], desk["port"]), timeout=2)
            finally:
                os.seteuid(0)
            with connection as sock:
                sock.sendall(b"selected")
                with sock.makefile("rb") as stream:
                    assert stream.read(8) == b"selected"
                received = log.stat().st_size
                if scenario == "close":
                    chain.close_session()
                elif scenario == "timeout":
                    time.sleep(ttl)
                else:
                    chain.quarantine()
                    chain.open_session(gw, desk, interface, fw.boottime() + ttl, chain.begin_request())
                try:
                    sock.sendall(b"closed")
                    assert not sock.recv(16)
                except (TimeoutError, ConnectionError, PermissionError):
                    pass
                assert log.stat().st_size == received, f"Рабочее место получило данные после {scenario}"
            print(scenario, "passed", flush=True)
    finally:
        server.terminate()
        server.wait()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--peer":
        echo(Path(sys.argv[2]))
    elif os.geteuid() or os.readlink("/proc/self/ns/net") == os.readlink("/proc/1/ns/net"):
        sys.exit("Запустите от root в отдельном пространстве сети через unshare --net")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            peer = subprocess.Popen(["unshare", "--net", "sleep", "120"])
            try:
                check(peer, Path(tmp))
            finally:
                peer.terminate()
                peer.wait()
