#!/usr/bin/env python3
"""poa-accessd: служба доступа на носителе (LIVE-2.0, этап Э8).

Работает от root и одна владеет ключом носителя, звеном и ключом VPN. Окно
«Подключение» (пользователь киоска) говорит с ней через сокет
`/run/poa/access.sock` строками JSON; служба проверяет, что на том конце
именно пользователь киоска (`SO_PEERCRED`), -- выход из киоска не даёт ни
ключей, ни права поднимать VPN.

Почему VPN поднимает служба, а не окно. Служба и так root: ей не нужно
никакого правила polkit, чтобы запустить `poa-vpn@<сеанс>.service`, а окну
не нужно никаких прав вовсе. Правило polkit из плана упразднено этим.

Команды:

* `status` -- активирован ли носитель, состояние VPN, срок доступа, цели;
* `activate {code}` -- активация; ответ -- строка otpauth для QR (показывается
  один раз, семя на носителе не хранится: TOTP -- фактор телефона);
* `login {totp}` -- вход, запись звена, сертификат, подъём VPN; ответ -- цели;
* `check` -- доступность целей по TCP (не отвечает / отклонила / доступна);
* `disconnect` -- опустить VPN.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poa_client  # noqa: E402
import poa_firewall  # noqa: E402
import poa_trust  # noqa: E402

CONFIG = os.environ.get("POA_ACCESS_CONFIG", "/etc/poa/access.json")
SOCKET = os.environ.get("POA_ACCESS_SOCKET", "/run/poa/access.sock")
VPN_RUN = "/run/poa/vpn"
#: Принятый описатель сервера -- в постоянном состоянии (state/poa).
DESCRIPTOR = "/var/lib/poa/descriptor.json"
#: Поправлять часы, если расходятся с сервером больше чем на столько секунд:
#: OpenVPN-клиент проверяет сроки сертификата шлюза.
CLOCK_SKEW = 300


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as handle:
        return json.load(handle)


class Service:
    def __init__(self, config: dict, runner=subprocess.run, clock=time.time,
                 firewall: "poa_firewall.Firewall | None" = None) -> None:
        self.config = config
        self.run = runner
        self.firewall = firewall
        self.clock = clock
        self.state = poa_client.State(config["state"])
        self.lock = threading.Lock()
        self.grant: dict | None = None
        self.session_id = ""

    # -- клиент -----------------------------------------------------------
    def server(self) -> dict:
        """Где сервер и как его узнать: из принятого описателя, иначе из access.json."""
        descriptor = self.descriptor()
        if descriptor:
            return {"endpoint": descriptor["endpoints"][0], "tls_spki": descriptor["tls_spki"],
                    "server_sign_keys": descriptor["server_sign_keys"]}
        return {k: self.config.get(k) for k in ("endpoint", "tls_spki", "server_sign_keys")}

    def descriptor(self) -> dict | None:
        try:
            with open(self.config.get("descriptor", DESCRIPTOR), encoding="utf-8") as handle:
                return json.load(handle)["descriptor"]
        except (OSError, ValueError, KeyError):
            return None

    def refresh_descriptor(self) -> bool:
        """Забирает описатель у сервера; принимает подлинный и более новый (Э18)."""
        root = self.config.get("root_public")
        if not root:
            return False
        try:
            status, data = self.client().endpoint.request("GET", "/v3/descriptor")
            if status != 200:
                return False
            document = json.loads(data)
            accepted = poa_trust.newer_descriptor(root, document, self.descriptor())
        except (poa_client.AccessError, poa_trust.TrustError, ValueError):
            return False
        if accepted is None:
            return False
        poa_client.atomic_write(self.config.get("descriptor", DESCRIPTOR),
                                poa_client.canonical(document))
        return True

    def client(self) -> poa_client.Client:
        import base64

        server = self.server()
        keys = tuple(base64.b64decode(k) for k in server["server_sign_keys"])
        endpoint = poa_client.Endpoint(server["endpoint"], tuple(server.get("tls_spki") or ()))
        return poa_client.Client(endpoint, self.config["media"],
                                 poa_client.FileSigner(self.config["device_key"]), keys)

    # -- команды ----------------------------------------------------------
    def status(self) -> dict:
        data = self.state.load()
        return {
            "activated": bool(data.get("activated")),
            "vpn": self.vpn_state(),
            "not_after": (self.grant or {}).get("not_after"),
            "targets": (self.grant or {}).get("targets", []),
            "authorized": os.path.exists("/run/poa/authorized"),
        }

    def activate(self, code: str) -> dict:
        grant = self.client().activate(code, self._info())
        data = self.state.load()
        # Семя TOTP на носителе не хранится: TOTP -- фактор телефона, а копия
        # носителя с семенем перестала бы требовать телефон.
        self.state.save({**data, "link": grant["link"], "activated": False})
        secret = grant["totp"]["secret"]
        label = self.config.get("label", self.config["media"])
        uri = (f"otpauth://totp/POA:{label}?secret={secret}&issuer=POA"
               f"&digits={grant['totp']['digits']}&period={grant['totp']['period']}")
        return {"otpauth": uri}

    def login(self, totp_code: str) -> dict:
        key_pem, vpn_request = self._vpn_key()
        grant = self.client().login(self.state, totp_code, self._status(), vpn_request)
        data = self.state.load()
        if not data.get("activated"):
            self.state.save({**data, "activated": True})
        self._fix_clock(grant.get("server_time"))
        self.grant = grant
        if self.firewall is not None:
            # Стадия «выдан доступ»: открываются шлюзы, потом -- цели в туннеле.
            self.firewall.allow_gateways(grant["vpn"].get("remotes", []))
            self.firewall.allow_targets(grant.get("targets", []))
        self._vpn_up(grant["vpn"], key_pem)
        self.flush_events()
        self.refresh_descriptor()
        return {"targets": grant.get("targets", []), "not_after": grant.get("not_after")}

    def flush_events(self) -> None:
        """Журнал событий -- на сервер при каждом входе (Э20). Удаляется только
        подтверждённое; сбой выгрузки входу не мешает -- очередь подождёт."""
        try:
            import poa_events

            queue = poa_events.Queue(self.config.get("events_dir", poa_events.QUEUE_DIR))
            pending = queue.pending()
            if pending:
                queue.ack(self.client().send_events(1, pending))
        except Exception:  # noqa: BLE001 -- журнал не должен ронять вход
            pass

    def check(self) -> dict:
        result = {}
        for target in (self.grant or {}).get("targets", []):
            result[target["id"]] = probe(target["host"], int(target["port"]))
        return {"reachability": result}

    def disconnect(self) -> dict:
        if self.firewall is not None:
            self.firewall.close_session()
        if self.session_id:
            self.run(["systemctl", "stop", f"poa-vpn@{self.session_id}.service"], check=False)
            shutil.rmtree(os.path.join(VPN_RUN, self.session_id), ignore_errors=True)
            self.session_id = ""
        return {}

    # -- внутреннее -------------------------------------------------------
    def _info(self) -> dict:
        return {"version": self.config.get("version", ""), "virt": detect_virt(self.run)}

    def _status(self) -> dict:
        release = self.config.get("release", {})
        return {"virt": detect_virt(self.run), "serial": release.get("serial", 0),
                "distribution": release.get("distribution", ""),
                "version": self.config.get("version", ""), "medium": "flash"}

    def _vpn_key(self) -> tuple[bytes, dict]:
        """Ключ VPN рождается здесь и живёт только в RAM (/run -- tmpfs)."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
        return pem, {"provider": "openvpn", "public_key": public.decode("ascii")}

    def _fix_clock(self, server_time) -> None:
        if isinstance(server_time, int) and abs(self.clock() - server_time) > CLOCK_SKEW:
            self.run(["date", "-u", "-s", f"@{server_time}"], check=False)

    def _vpn_up(self, vpn: dict, key_pem: bytes) -> None:
        if vpn.get("provider") != "openvpn":
            raise poa_client.AccessError(0, "provider_not_supported")
        self.disconnect()
        self.session_id = os.urandom(8).hex()
        directory = os.path.join(VPN_RUN, self.session_id)
        os.makedirs(directory, mode=0o700)
        with open(os.path.join(directory, "client.conf"), "w", encoding="ascii") as handle:
            handle.write(openvpn_config(vpn, key_pem.decode("ascii"),
                                        vpn.get("gateway_name") or self.config.get("gateway_name", "")))
        os.chmod(os.path.join(directory, "client.conf"), 0o600)
        self.run(["systemctl", "start", f"poa-vpn@{self.session_id}.service"], check=True)

    def vpn_state(self) -> str:
        if not self.session_id:
            return "down"
        result = self.run(["systemctl", "is-active", f"poa-vpn@{self.session_id}.service"],
                          check=False, capture_output=True, text=True)
        return "up" if result.stdout.strip() == "active" else "down"


def openvpn_config(vpn: dict, key_pem: str, gateway_name: str) -> str:
    """Конфигурация клиента: всё внутри файла, ключ -- только в /run."""
    remotes = "\n".join(
        f"remote {r['host']} {r['port']} {'udp' if r['proto'] == 'udp' else 'tcp-client'}"
        for r in vpn["remotes"])
    verify = f"verify-x509-name {gateway_name} name\n" if gateway_name else ""
    tc2 = f"<tls-crypt-v2>\n{vpn['tls_crypt_v2']}</tls-crypt-v2>\n" if vpn.get("tls_crypt_v2") else ""
    return (
        "client\ndev tun\n" + remotes + "\n"
        "remote-cert-tls server\n" + verify +
        "tls-version-min 1.3\n"
        "data-ciphers AES-256-GCM:CHACHA20-POLY1305\n"
        "nobind\npersist-tun\nverb 2\n"
        # Маршруты только к назначенным целям -- их и выдаёт шлюз; всё прочее
        # не уходит в туннель и закрыто сетевым экраном носителя (Э14).
        f"<ca>\n{vpn['ca']}</ca>\n<cert>\n{vpn['cert']}</cert>\n<key>\n{key_pem}</key>\n" + tc2
    )


def probe(host: str, port: int, timeout: float = 4.0) -> str:
    """«up», «refused» (машина ответила отказом) или «silent» (не отвечает)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "up"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "silent"


def detect_virt(runner=subprocess.run) -> str:
    """Признак виртуализации для политики сервера (сценарий V8)."""
    result = runner(["systemd-detect-virt", "--vm"], check=False, capture_output=True, text=True)
    name = (result.stdout or "").strip()
    return "none" if not name or name == "none" else name


def peer_uid(conn: socket.socket) -> int:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", creds)[1]


def serve(service: Service, path: str = SOCKET, allowed_uid: int | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    os.chmod(path, 0o666)       # права решает SO_PEERCRED, а не режим файла
    listener.listen(4)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(service, conn, allowed_uid), daemon=True).start()


def handle(service: Service, conn: socket.socket, allowed_uid: int | None) -> None:
    with conn:
        if allowed_uid is not None and peer_uid(conn) not in (allowed_uid, 0):
            conn.sendall(b'{"error":"forbidden"}\n')
            return
        raw = conn.makefile("rb").readline(1 << 16)
        try:
            request = json.loads(raw)
            command = request.get("cmd")
            with service.lock:
                if command == "status":
                    answer = service.status()
                elif command == "activate":
                    answer = service.activate(str(request.get("code", "")))
                elif command == "login":
                    answer = service.login(str(request.get("totp", "")))
                elif command == "check":
                    answer = service.check()
                elif command == "disconnect":
                    answer = service.disconnect()
                else:
                    answer = {"error": "unknown_command"}
        except poa_client.AccessError as exc:
            answer = {"error": exc.reason, "code": exc.code, "detail": exc.body}
        except Exception as exc:  # noqa: BLE001 -- окно должно получить ответ, а не обрыв
            answer = {"error": "internal", "detail": str(exc)[:200]}
        conn.sendall(json.dumps(answer, ensure_ascii=False).encode("utf-8") + b"\n")


def keep_server_open(firewall: "poa_firewall.Firewall", host: str, stop=None) -> None:
    """Адреса сервера в экране: пока не разрешились -- раз в 30 с, потом раз в 5 мин.

    Нужно и проверке отзыва v2 (`/v2/state`): без адресов сервера в наборе её
    запрос не выйдет за пределы носителя.
    """
    import urllib.parse

    name = urllib.parse.urlsplit(host).hostname or host
    while not (stop and stop.is_set()):
        resolved = firewall.allow_server(name)
        time.sleep(300 if resolved else 30)


def main() -> int:
    config = load_config()
    uid = pwd.getpwnam(config["kiosk_user"]).pw_uid if config.get("kiosk_user") else None
    firewall = poa_firewall.Firewall()
    threading.Thread(target=keep_server_open, args=(firewall, config["endpoint"]), daemon=True).start()
    serve(Service(config, firewall=firewall), allowed_uid=uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
