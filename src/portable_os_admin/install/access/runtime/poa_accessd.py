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

После MFA окно получает каталог. Команда select принимает только идентификатор
места. Служба открывает подтверждённое направление до истечения допуска
и запускает клиент от имени пользователя киоска. Менять цель и продлевать
правила может только служба.
"""
from __future__ import annotations

import base64
import collections
import re
import http.client
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
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poa_client  # noqa: E402
import poa_firewall  # noqa: E402
import poa_launch  # noqa: E402
import poa_trust  # noqa: E402

CONFIG = os.environ.get("POA_ACCESS_CONFIG", "/etc/poa/access.json")
SOCKET = os.environ.get("POA_ACCESS_SOCKET", "/run/poa/access.sock")
#: Под /run/openvpn: политика SELinux РЕД ОС даёт openvpn читать только
#: openvpn_var_run_t, а /run/poa -- var_run_t (AVC denied, живая загрузка
#: 02.10.2026). Метку ставит restorecon после записи.
VPN_RUN = "/run/openvpn/poa"
#: Принятый описатель сервера -- в постоянном состоянии (state/poa).
DESCRIPTOR = "/var/lib/poa/descriptor.json"
TLS_PIN = "/var/lib/poa/tls-pin"
#: Якоря после замены корня (Э18) -- постоянное состояние.
ROOTS = "/var/lib/poa/roots.json"
#: Обновление образа (Э9): запрос службе и её прогресс.
UPDATE_REQUEST = "/run/poa/update-request.json"
UPDATE_STATE = "/run/poa/update.json"
#: Последний подлинный список отзыва рабочих ключей (Э18).
REVOKED = "/var/lib/poa/revoked.json"
#: Признак «проверка отзыва ответила» (её ставит poa-revocation-check).
AUTHORIZED = "/run/poa/authorized"
#: Проба портала (Э14): ответ сервера на `GET /v3/probe` по HTTP.
PROBE_BODY = b"poa-probe 1\n"
#: Окно портала -- отдельный пользователь, отдельный юнит, ограниченное время:
#: пока оно живо, этому пользователю открыт веб (правило в poa_firewall).
PORTAL_UNIT = "poa-portal"
#: Проверка отзыва (её таймер -- раз в 5 мин); запускается сразу, как только
#: сеть открылась, -- иначе после портала человек ждал бы до пяти минут.
REVOCATION_UNIT = "poa-revocation.service"
PORTAL_SECONDS = 600
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
        self.catalog: dict | None = None
        self.entry: dict | None = None
        self.grant: dict | None = None
        self.generation = 0
        self.next_renew = 0.0
        self.tunnel_ready = False
        self.work_process = None
        self.work_result = ""
        self.last_error = ""
        self.session_id = ""
        #: Последний результат пробы портала: state -- open/portal/offline.
        self.portal: dict = {"state": "unknown", "url": ""}

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

    def anchors(self) -> list[dict]:
        """Якоря -- корни организации: с записи носителя (`roots` в access.json,
        хранилище LUKS), а после замены корня -- из постоянного состояния."""
        try:
            with open(self.config.get("roots_state", ROOTS), encoding="utf-8") as handle:
                return json.load(handle)["roots"]
        except (OSError, ValueError, KeyError):
            return list(self.config.get("roots") or [])

    def _apply_root_updates(self, updates) -> list[dict]:
        """Замены корня, приехавшие с описателем. Каждую подписывает другой
        корень; принятые ложатся в состояние -- и переживают перезагрузку."""
        anchors = self.anchors()
        changed = False
        for update in updates if isinstance(updates, list) else []:
            try:
                anchors = poa_trust.apply_root_replacement(anchors, update)
                changed = True
            except (poa_trust.TrustError, KeyError, TypeError):
                continue
        if changed:
            poa_client.atomic_write(self.config.get("roots_state", ROOTS),
                                    poa_client.canonical({"roots": anchors}))
        return anchors

    def _revocation(self, anchors, incoming) -> dict | None:
        """Самый свежий подлинный список отзыва: сохранённый или пришедший.
        Запомненный не «забывается», если следующий описатель придёт без него."""
        path = self.config.get("revoked_state", REVOKED)
        try:
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            stored = None
        best = stored
        if isinstance(incoming, dict):
            try:
                body = poa_trust.check_revocation_list(anchors, incoming)
                if stored is None or body["serial"] > stored["list"]["serial"]:
                    best = incoming
                    poa_client.atomic_write(path, poa_client.canonical(incoming))
            except (poa_trust.TrustError, KeyError, TypeError):
                pass
        return best

    def refresh_descriptor(self) -> bool:
        """Забирает описатель у сервера; принимает подлинный и более новый (Э18)."""
        if not self.anchors():
            return False
        try:
            status, data = self.client().endpoint.request("GET", "/v3/descriptor")
            if status != 200:
                return False
            document = json.loads(data)
            anchors = self._apply_root_updates(document.get("root_updates"))
            revoked = self._revocation(anchors, document.get("revoked"))
            accepted = poa_trust.newer_descriptor(anchors, document, self.descriptor(), revoked)
        except (poa_client.AccessError, poa_trust.TrustError, ValueError):
            return False
        if accepted is None:
            return False
        poa_client.atomic_write(self.config.get("descriptor", DESCRIPTOR),
                                poa_client.canonical(document))
        # Проверка отзыва (curl) узнаёт сервер по тем же отпечаткам.
        pins = ";".join("sha256//" + base64.b64encode(bytes.fromhex(h)).decode("ascii")
                        for h in accepted.get("tls_spki", []))
        if pins:
            poa_client.atomic_write(self.config.get("tls_pin", TLS_PIN), (pins + "\n").encode(), 0o644)
        return True

    def client(self) -> poa_client.Client:
        import base64

        server = self.server()
        keys = tuple(base64.b64decode(k) for k in server["server_sign_keys"])
        endpoint = poa_client.Endpoint(server["endpoint"], tuple(server.get("tls_spki") or ()))
        return poa_client.Client(endpoint, self.config["media"],
                                 poa_client.FileSigner(self.config["device_key"]), keys)

    # -- команды ----------------------------------------------------------
    def _revoked(self) -> bool:
        return any(os.path.exists(path) for path in (
            "/run/poa/media-revoked", "/var/lib/poa/media-revoked"))

    def _may_work(self) -> None:
        if self._revoked():
            raise poa_client.AccessError(403, "blocked")

    def status(self) -> dict:
        self.enforce()
        data = self.state.load()
        return {
            "activated": bool(data.get("activated")),
            "authenticated": self.catalog is not None,
            "vpn": self.vpn_state(),
            "active": self.entry["id"] if self.entry else None,
            "generation": self.generation,
            "working": self.work_process is not None,
            "result": self.work_result,
            "reason": self.last_error,
            "not_after": (self.grant or {}).get("not_after"),
            "targets": (self.catalog or {}).get("targets", []),
            "authorized": not self._revoked() and os.path.exists(self.config.get("authorized", AUTHORIZED)),
            "update": self.update_state(),
            "portal": self.portal.get("state", "unknown"),
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
        self._invalidate("")
        self._may_work()
        try:
            catalog = self.client().login(self.state, totp_code, self._status())
        except poa_client.AccessError as exc:
            if exc.code == 426:
                self.start_update((exc.body or {}).get("release"))
            raise
        self._may_work()
        self.start_update(self.state.load().get("release"))
        data = self.state.load()
        if not data.get("activated"):
            self.state.save({**data, "activated": True})
        self._fix_clock(catalog.get("server_time"))
        self.catalog = catalog
        self.generation = 0
        self.flush_events()
        self.refresh_descriptor()
        return {"targets": catalog["targets"]}

    def refresh_catalog(self) -> dict:
        self._may_work()
        if not self.catalog:
            raise poa_client.AccessError(401, "session_expired")
        try:
            result = self.client().catalog(self.catalog["session"])
        except poa_client.AccessError as exc:
            if 400 <= exc.code < 500:
                self._invalidate(exc.reason)
            raise
        self.catalog["targets"] = result["targets"]
        return {"targets": result["targets"]}

    def select(self, target_id: str) -> dict:
        self.enforce()
        self._may_work()
        if not self.catalog:
            raise poa_client.AccessError(401, "session_expired")
        if self.entry and not self.disconnect()["authenticated"]:
            raise poa_client.AccessError(401, "session_expired")
        entry = next((e for e in self.catalog["targets"] if e["id"] == target_id), None)
        if entry is None:
            raise poa_client.AccessError(403, "unknown_target")
        self.generation += 1
        key_pem, request = self._vpn_key()
        try:
            token = self.firewall.begin_request() if self.firewall else None
            grant = self.client().select(self.catalog["session"], entry, self.generation, request)
            self._may_work()
            # Параметры из подписанного ответа проверяются до открытия сети.
            poa_launch.client_command(grant["target"])
            self.session_id = os.urandom(6).hex()
            config = openvpn_config(grant["vpn"], key_pem.decode("ascii"),
                                    grant["vpn"].get("gateway_name") or self.config.get("gateway_name", ""),
                                    self.interface)
            self.entry, self.grant = entry, grant
            self.catalog["targets"] = grant["targets"]
            self._apply_grant(grant, token)
            self._vpn_up(config)
            self._may_work()
            if poa_client.boottime() >= grant["_deadline"]:
                raise poa_client.AccessError(401, "session_expired")
        except poa_client.AccessError as exc:
            if exc.reason == "assignment_changed" and not self.grant:
                self.refresh_catalog()
            else:
                self._invalidate(exc.reason)
            raise
        except Exception:
            self._invalidate("selection_failed")
            raise
        self.work_result = self.last_error = ""
        return {"target": {k: grant["target"].get(k, "") for k in ("id", "name", "type", "user", "domain")},
                "generation": self.generation}

    @property
    def interface(self) -> str:
        return "tun" + self.session_id

    def _apply_grant(self, answer: dict, token: int | None) -> None:
        self._may_work()
        if self.firewall:
            self.firewall.open_session(self.grant["vpn"]["remotes"], self.grant["target"],
                                       self.interface, answer["_deadline"], token)
        self.grant.update(_deadline=answer["_deadline"], not_after=answer["not_after"])
        self.next_renew = poa_client.boottime() + 60

    def enforce(self) -> None:
        if self.catalog and self._revoked():
            self._invalidate("blocked")
        elif self.grant and poa_client.boottime() >= self.grant["_deadline"]:
            self._invalidate("session_expired")
        elif self.grant:
            up = self.vpn_state() == "up"
            if self.tunnel_ready and not up:
                self._invalidate("tunnel_lost")
            else:
                self.tunnel_ready = up

    def maintain(self) -> None:
        self.enforce()
        if not self.grant or not self.work_process or poa_client.boottime() < self.next_renew:
            return
        try:
            token = self.firewall.begin_request() if self.firewall else None
            answer = self.client().renew(self.catalog["session"], self.entry, self.generation)
            if poa_client.boottime() >= self.grant["_deadline"]:
                raise poa_client.AccessError(401, "session_expired")
            self._apply_grant(answer, token)
            self.catalog["targets"] = answer["targets"]
        except poa_client.AccessError as exc:
            if exc.code >= 500 or exc.reason.startswith("network"):
                self.next_renew = poa_client.boottime() + 5
            else:
                self._invalidate(exc.reason)
        except (OSError, http.client.HTTPException):
            self.next_renew = poa_client.boottime() + 5
        except Exception:
            self._invalidate("selection_failed")

    def watch_session(self) -> None:
        while True:
            with self.lock:
                self.maintain()
            time.sleep(1)

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

    def start_work(self, generation, display: str, password: str) -> dict:
        self.enforce()
        self._may_work()
        if (type(generation) is not int or not isinstance(password, str)
                or len(password) > 4096 or any(c in password for c in ("\n", "\r", "\0"))):
            raise poa_client.AccessError(400, "bad_request")
        if (not self.grant or generation != self.generation or self.work_process
                or self.vpn_state() != "up"):
            raise poa_client.AccessError(409, "selection_changed")
        account = pwd.getpwnam(self.config["kiosk_user"])
        if not account.pw_uid or not re.fullmatch(r":[0-9]+(\.[0-9]+)?", display):
            raise poa_client.AccessError(403, "forbidden")
        target = self.grant["target"]
        argv, needs_password = poa_launch.client_command(target)
        unit = f"poa-work-{self.session_id}"
        command = ["systemd-run", "--quiet", "--collect", "--wait", "--pipe",
                   "--service-type=exec", f"--unit={unit}", f"--uid={account.pw_uid}",
                   "-p", "BindsTo=poa-accessd.service", "-p", "After=poa-accessd.service",
                   "-p", "NoNewPrivileges=yes", f"--setenv=DISPLAY={display}",
                   f"--setenv=HOME={account.pw_dir}", f"--setenv=XAUTHORITY={account.pw_dir}/.Xauthority",
                   "--", *argv]
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        self.work_process = proc

        def finish():
            tail = collections.deque(maxlen=50)
            try:
                with proc.stdin:
                    if needs_password:
                        proc.stdin.write(password.encode("utf-8") + b"\n")
            except OSError:
                pass
            with proc.stdout:
                for raw in proc.stdout:
                    tail.append(raw.decode("utf-8", "replace").rstrip())
            result = proc.wait()
            with self.lock:
                if self.work_process is proc:
                    self.work_process = None
                    self.work_result = poa_launch.session_error(target["type"], result, list(tail))
                    self.disconnect()

        threading.Thread(target=finish, daemon=True).start()
        return {"started": True}

    def _close_local(self) -> None:
        try:
            if self.firewall:
                self.firewall.close_session()
        finally:
            self.entry = self.grant = None
            self.tunnel_ready = False
            if self.session_id:
                if self.work_process:
                    self.run(["systemctl", "stop", f"poa-work-{self.session_id}.service"], check=False)
                self.work_process = None
                self.run(["systemctl", "stop", f"poa-vpn@{self.session_id}.service"], check=False)
                shutil.rmtree(os.path.join(VPN_RUN, self.session_id), ignore_errors=True)
                self.session_id = ""

    def _invalidate(self, reason: str) -> None:
        self.catalog = None
        self.last_error = reason
        self._close_local()

    def disconnect(self) -> dict:
        active = self.entry is not None
        self._close_local()
        if active and self.catalog:
            try:
                result = self.client().release(self.catalog["session"], self.generation)
                self.catalog["targets"] = result["targets"]
            except (poa_client.AccessError, OSError, http.client.HTTPException):
                # Без подтверждения выхода состояние сеанса на сервере неизвестно.
                self.catalog = None
                self.last_error = "session_expired"
        return {"authenticated": self.catalog is not None}

    # -- внутреннее -------------------------------------------------------
    def _info(self) -> dict:
        return {"version": self.config.get("version", ""), "virt": detect_virt(self.run)}

    def _status(self) -> dict:
        # Номер работающего выпуска -- из командной строки ядра (его ставит
        # GRUB из slot.cfg слота): по нему сервер решает про обновление и 426.
        import poa_update

        try:
            serial = int(poa_update.cmdline_value("poa.serial") or 0)
        except (OSError, ValueError):
            serial = 0
        return {"virt": detect_virt(self.run), "serial": serial,
                "distribution": self.config.get("release", {}).get("distribution", ""),
                "version": self.config.get("version", ""), "medium": "flash"}

    # -- обновление образа (Э9) -------------------------------------------
    # -- портал (Э14) ------------------------------------------------------
    def probe_url(self) -> str:
        """Проба -- по HTTP на порт 80 того же сервера (сервер слушает там
        `/v3/`); сервер, заданный как http://, -- по его же адресу и порту."""
        parts = urllib.parse.urlsplit(self.server()["endpoint"])
        netloc = parts.netloc if parts.scheme == "http" else (parts.hostname or "")
        return f"http://{netloc}/v3/probe"

    def open_portal(self, display: str) -> dict:
        """Окно страницы входа сети -- под пользователем poa-portal.

        `systemd-run`: свой юнит (не запустить второе окно), предел времени
        (веб для этого пользователя не остаётся открытым), свой /tmp. Ссылка
        -- из перенаправления, которое вернула проба, иначе сама проба:
        портал перехватит и её.
        """
        if self.portal.get("state") != "portal":
            return {"error": "no_portal"}
        if not display.startswith(":") or not display[1:].replace(".", "").isdigit():
            return {"error": "bad_display"}
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "poa_portal.py")
        argv = ["systemd-run", f"--unit={PORTAL_UNIT}", "--collect", "--quiet",
                f"--uid={poa_firewall.PORTAL_USER}", f"--gid={poa_firewall.PORTAL_USER}",
                "-p", f"RuntimeMaxSec={PORTAL_SECONDS}", "-p", "PrivateTmp=yes",
                f"--setenv=DISPLAY={display}", "--setenv=HOME=/tmp",
                "/usr/bin/python3", script, self.portal.get("url") or self.probe_url(), self.probe_url()]
        result = self.run(argv, check=False)
        return {"ok": getattr(result, "returncode", 0) == 0}

    def watch_portal(self, stop=None, sleep=time.sleep) -> None:
        """Проба портала, пока сервер не ответил: раз в 5 с при портале,
        раз в 10 с без сети; после ответа сервера -- раз в 30 с."""
        while not (stop and stop.is_set()):
            if os.path.exists(self.config.get("authorized", AUTHORIZED)):
                self.portal = {"state": "open", "url": ""}
                sleep(30)
                continue
            before = self.portal.get("state")
            try:
                self.portal = portal_probe(self.probe_url())
            except Exception:  # noqa: BLE001 - проба не должна ронять службу
                self.portal = {"state": "offline", "url": ""}
            if self.portal["state"] == "open" and before != "open":
                # Сеть только что открылась (портал пройден, Wi-Fi подключён) --
                # проверку отзыва не ждём по таймеру (живая проверка 03.10.2026).
                self.run(["systemctl", "start", "--no-block", REVOCATION_UNIT], check=False)
            sleep(5 if self.portal["state"] == "portal" else 10)

    def start_update(self, info: dict | None) -> None:
        """Назначенный сервером выпуск новее работающего -- запустить загрузку
        в неактивный слот (служба poa-image-update, под systemd-inhibit)."""
        if not info:
            return
        try:
            if int(info.get("serial", 0)) <= self._status()["serial"]:
                return
        except (TypeError, ValueError):
            return
        os.makedirs(os.path.dirname(UPDATE_REQUEST), exist_ok=True)
        poa_client.atomic_write(self.config.get("update_request", UPDATE_REQUEST),
                                json.dumps(info).encode(), 0o600)
        self.run(["systemctl", "start", "--no-block", "poa-image-update.service"], check=False)

    def update_state(self) -> dict:
        try:
            with open(self.config.get("update_state", UPDATE_STATE), encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

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

    def _vpn_up(self, config: str) -> None:
        directory = os.path.join(VPN_RUN, self.session_id)
        os.makedirs(directory, mode=0o700)
        os.chmod(VPN_RUN, 0o700)
        with open(os.path.join(directory, "client.conf"), "w", encoding="ascii") as handle:
            handle.write(config)
        os.chmod(os.path.join(directory, "client.conf"), 0o600)
        if shutil.which("restorecon"):
            self.run(["restorecon", os.path.dirname(VPN_RUN)], check=False)
            self.run(["restorecon", "-R", VPN_RUN], check=False)
        self.run(["systemctl", "start", f"poa-vpn@{self.session_id}.service"], check=True)

    def vpn_state(self) -> str:
        if not self.session_id:
            return "down"
        result = self.run(["systemctl", "is-active", f"poa-vpn@{self.session_id}.service"],
                          check=False, capture_output=True, text=True)
        if result.stdout.strip() != "active":
            return "down"
        # Юнит активен сразу после запуска openvpn, задолго до туннеля; окно,
        # поверив этому, проверяло цели в пустоту («не отвечает», живая
        # загрузка 02.10.2026). Поднят -- когда путь к цели лежит через tun.
        addresses = poa_firewall.resolve4(self.grant["target"]["host"])
        if not addresses:
            return "connecting"
        route = self.run(["ip", "-o", "route", "get", addresses[0]],
                         check=False, capture_output=True, text=True)
        fields = (route.stdout or "").split()
        if "dev" in fields:
            index = fields.index("dev") + 1
            if index < len(fields) and fields[index] == self.interface:
                return "up"
        return "connecting"


def openvpn_config(vpn: dict, key_pem: str, gateway_name: str, interface: str) -> str:
    """Конфигурация клиента: всё внутри файла, ключ -- только в /run."""
    name = r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,252}"
    if (vpn.get("provider") != "openvpn" or not re.fullmatch(name, gateway_name)
            or not re.fullmatch(r"tun[0-9a-f]{12}", interface)):
        raise ValueError("Не задан проверяемый профиль шлюза")
    lines = []
    for r in vpn["remotes"]:
        if (not re.fullmatch(name, r["host"]) or r["proto"] not in ("udp", "tcp")
                or type(r["port"]) is not int or not 1 <= r["port"] <= 65535):
            raise ValueError("Некорректный адрес шлюза")
        lines.append(f"remote {r['host']} {r['port']} {'udp' if r['proto'] == 'udp' else 'tcp-client'}")
    if not lines:
        raise ValueError("Не задан адрес шлюза")
    for field, label in (("ca", "CERTIFICATE"), ("cert", "CERTIFICATE"),
                         ("tls_crypt_v2", "OpenVPN tls-crypt-v2 client key")):
        if field == "tls_crypt_v2" and not vpn.get(field):
            continue
        if not re.fullmatch(f"-----BEGIN {label}-----\n[A-Za-z0-9+/=\n]+-----END {label}-----\n?", vpn[field]):
            raise ValueError("Повреждены реквизиты шлюза")
    remotes = "\n".join(lines)
    verify = f"verify-x509-name {gateway_name} name\n"
    tc2 = f"<tls-crypt-v2>\n{vpn['tls_crypt_v2'].rstrip()}\n</tls-crypt-v2>\n" if vpn.get("tls_crypt_v2") else ""
    return (
        f"client\ndev {interface}\ndev-type tun\n" + remotes + "\n"
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


def portal_probe(url: str, timeout: float = 5.0) -> dict:
    """Перехватывает ли сеть веб: {"state": open|portal|offline, "url": куда}.

    HTTP и без следования перенаправлениям: портал отвечает за сервер своим
    перенаправлением или своей страницей. Ответ сервера сверяется побайтно --
    иначе страница портала с кодом 200 сошла бы за «сеть открыта».
    """
    parts = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        connection.request("GET", parts.path or "/", headers={"Cache-Control": "no-cache"})
        response = connection.getresponse()
        body = response.read(4096)
    except (OSError, http.client.HTTPException):
        return {"state": "offline", "url": ""}
    finally:
        connection.close()
    if response.status == 200 and body == PROBE_BODY:
        return {"state": "open", "url": ""}
    location = response.getheader("Location") or ""
    if 300 <= response.status < 400 and location.startswith(("http://", "https://")):
        return {"state": "portal", "url": location}
    return {"state": "portal", "url": url}


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
                elif command == "catalog":
                    answer = service.refresh_catalog()
                elif command == "select":
                    answer = service.select(request.get("id"))
                elif command == "start":
                    answer = service.start_work(request.get("generation"), request.get("display", ""),
                                                request.get("password", ""))
                elif command == "disconnect":
                    answer = service.disconnect()
                elif command == "portal":
                    answer = service.open_portal(str(request.get("display", "")))
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

    parts = urllib.parse.urlsplit(host)
    name = parts.hostname or host
    port = parts.port or (80 if parts.scheme == "http" else 443)
    while not (stop and stop.is_set()):
        resolved = firewall.allow_server(name, port)
        time.sleep(300 if resolved else 30)


def main() -> int:
    config = load_config()
    uid = pwd.getpwnam(config["kiosk_user"]).pw_uid
    if not uid:
        raise ValueError("Пользователь киоска не может быть root")
    firewall = poa_firewall.Firewall()
    threading.Thread(target=keep_server_open, args=(firewall, config["endpoint"]), daemon=True).start()
    service = Service(config, firewall=firewall)
    firewall.close_session()
    firewall.bind_client(uid)
    threading.Thread(target=service.watch_session, daemon=True).start()
    threading.Thread(target=service.watch_portal, daemon=True).start()
    serve(service, allowed_uid=uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
