"""Что из доступа (Э8) кладётся в образ и что -- на носитель.

**В образ** (при сборке, одинаково для всех носителей): код клиента в
`/usr/lib/poa-access`, юниты `poa-accessd` и `poa-vpn@`, автозапуск окна
«Подключение». Именно в образ, а не в слой носителя: слой носителя лежит над
образом и перекрывал бы его, и обновление образа (Э9) не обновило бы клиента.

**На носитель** (при записи): `/etc/poa/access.json` -- идентификатор, адрес
сервера, ключ подписи сервера, отпечаток TLS -- и ссылка автозапуска службы.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

from ...config.schema import InstallConfig

RUNTIME_SOURCE = Path(__file__).with_name("runtime")
RUNTIME_DIR = "usr/lib/poa-access"
CONFIG_PATH = "etc/poa/access.json"
STATE_PATH = "/var/lib/poa/access.json"
#: Отпечатки TLS сервера в формате curl -- для проверки отзыва (v2).
TLS_PIN_PATH = "etc/poa/tls-pin"
SERVICE_NAME = "poa-accessd.service"
SERVICE_PATH = f"etc/systemd/system/{SERVICE_NAME}"
VPN_UNIT_PATH = "etc/systemd/system/poa-vpn@.service"
UPDATE_UNIT_PATH = "etc/systemd/system/poa-image-update.service"
AUTOSTART_PATH = "etc/xdg/autostart/poa-connect.desktop"
RUNTIME_FILES = ("poa_client.py", "poa_accessd.py", "poa_launch.py", "poa_connect.py",
                 "poa_qr.py", "poa_ssh.py", "poa_vnc.py", "poa_events.py", "poa_firewall.py",
                 "poa_trust.py", "poa_gost.py", "poa_update.py")
FIREWALL_SERVICE_NAME = "poa-firewall.service"
FIREWALL_SERVICE_PATH = f"etc/systemd/system/{FIREWALL_SERVICE_NAME}"
SYSUSERS_PATH = "usr/lib/sysusers.d/poa-access.conf"
EVENTS_SERVICE_NAME = "poa-events.service"
EVENTS_SERVICE_PATH = f"etc/systemd/system/{EVENTS_SERVICE_NAME}"
EVENT_CLI_PATH = "usr/local/sbin/poa-event"


def is_enabled(config: InstallConfig) -> bool:
    """Доступ живёт там же, где отзыв: сервер доступа -- это сервер отзыва."""
    from ..revocation.policy import is_enabled as revocation_enabled

    return revocation_enabled(config)


def service_unit() -> str:
    return """[Unit]
Description=Служба доступа к рабочему месту (poa-accessd)
After=network.target
RequiresMountsFor=/var/lib/poa

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/lib/poa-access/poa_accessd.py
Restart=on-failure
RestartSec=2
# Ключ носителя, звено и ключ VPN -- только у этой службы.
UMask=0077
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"""


def update_unit() -> str:
    """Обновление образа в неактивный слот (Э9). systemd-inhibit -- чтобы
    выключение и сон подождали конца записи на носитель."""
    return """[Unit]
Description=Обновление системы в неактивный слот
After=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/systemd-inhibit --what=shutdown:sleep --who=poa --why="Записывается обновление системы" /usr/bin/python3 /usr/lib/poa-access/poa_update.py /etc/poa/access.json /run/poa/update-request.json
Nice=10
IOSchedulingClass=idle
"""


def vpn_unit() -> str:
    return """[Unit]
Description=VPN-сеанс носителя %i
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/sbin/openvpn --config /run/openvpn/poa/%i/client.conf
Restart=on-failure
RestartSec=5
"""


def autostart_entry() -> str:
    return """[Desktop Entry]
Type=Application
Name=Подключение
Exec=/usr/bin/python3 /usr/lib/poa-access/poa_connect.py
NoDisplay=true
X-GNOME-Autostart-enabled=true
"""


def events_unit() -> str:
    """Вход (E01) и выход (E02) -- после разблокировки, когда том журналов
    уже смонтирован; до разблокировки в журнал не пишется ничего (правило Э20)."""
    return """[Unit]
Description=Журнал событий носителя: вход и выход
RequiresMountsFor=/var/log
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c '/usr/local/sbin/poa-event E01 machine=$(sha256sum /sys/class/dmi/id/product_uuid 2>/dev/null | cut -c1-16) virt=$(systemd-detect-virt --vm 2>/dev/null || echo none)'
ExecStop=/usr/local/sbin/poa-event E02

[Install]
WantedBy=multi-user.target
"""


def firewall_unit() -> str:
    """Экран ставится раньше сети: до него носитель не говорит ни с кем."""
    return """[Unit]
Description=Сетевой экран носителя (по умолчанию закрыто всё)
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
After=systemd-sysusers.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/python3 -c "import sys; sys.path.insert(0, '/usr/lib/poa-access'); import poa_firewall; poa_firewall.Firewall().load()"

[Install]
WantedBy=sysinit.target
"""


def image_files(config: InstallConfig) -> dict[str, tuple[str, int | None]]:
    if not is_enabled(config):
        return {}
    files: dict[str, tuple[str, int | None]] = {
        f"{RUNTIME_DIR}/{name}": ((RUNTIME_SOURCE / name).read_text(encoding="utf-8"), 0o755)
        for name in RUNTIME_FILES
    }
    files[SERVICE_PATH] = (service_unit(), 0o644)
    files[VPN_UNIT_PATH] = (vpn_unit(), 0o644)
    files[UPDATE_UNIT_PATH] = (update_unit(), 0o644)
    files[AUTOSTART_PATH] = (autostart_entry(), 0o644)
    files[EVENTS_SERVICE_PATH] = (events_unit(), 0o644)
    files[FIREWALL_SERVICE_PATH] = (firewall_unit(), 0o644)
    # Пользователь окна портала: экран пускает в веб только его (Э14).
    files[SYSUSERS_PATH] = ('u poa-portal - "Окно портала" /nonexistent\n', 0o644)
    files[EVENT_CLI_PATH] = ("#!/bin/sh\nexec /usr/bin/python3 /usr/lib/poa-access/poa_events.py \"$@\"\n", 0o755)
    return files


def curl_pins(spki_hex) -> str:
    """Отпечатки SPKI (hex SHA-256) в виде `--pinnedpubkey` curl."""
    return ";".join("sha256//" + base64.b64encode(bytes.fromhex(h)).decode("ascii")
                    for h in spki_hex)


def medium_config(config: InstallConfig, media_id: str, server_key_pem: str,
                  tls_spki: tuple[str, ...] = (), roots: list | None = None) -> str:
    """`/etc/poa/access.json` этого носителя."""
    from cryptography.hazmat.primitives import serialization

    key = serialization.load_pem_public_key(server_key_pem.encode("ascii"))
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return json.dumps({
        "endpoint": config.revocation.url.rstrip("/"),
        "tls_spki": list(tls_spki),
        "server_sign_keys": [base64.b64encode(raw).decode("ascii")],
        "media": media_id,
        "device_key": "/etc/poa/device.key",
        "state": STATE_PATH,
        "kiosk_user": config.account.username,
        "label": media_id,
        # Корни организации (Э18): по ним носитель проверяет описатели сервера.
        # Хранилище LUKS -- не образ: образ не может нести собственный якорь.
        "roots": list(roots or []),
        # Утилита переменных GRUB у семейства -- ею обновление (Э9) просит
        # загрузчик попробовать новый слот.
        "grub_editenv": "grub2-editenv" if config.target_os == "redos" else "grub-editenv",
    }, ensure_ascii=False, indent=2) + "\n"
