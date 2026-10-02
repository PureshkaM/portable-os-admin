"""Сетевой экран носителя (LIVE-2.0, этап Э14): по умолчанию закрыто всё.

Правила одни на всю жизнь сеанса, меняются только наборы адресов -- их
наполняет `poa-accessd` по стадиям:

| Стадия            | Что открыто                                           |
|-------------------|-------------------------------------------------------|
| всегда            | DHCP, DNS, сервер доступа (80 -- проба, порт адреса)  |
| портал            | веб только для пользователя окна портала (`poa-portal`)|
| выдан доступ      | шлюзы VPN (адрес, протокол, порт)                     |
| VPN поднят        | назначенные цели, и только через туннель              |

Входящее закрыто всегда, кроме ответов на установленные соединения и DHCP.
Наборы, а не перезапись правил: смена стадии атомарна и не рвёт соединений.
"""
from __future__ import annotations

import socket
import subprocess

TABLE = "poa"
PORTAL_USER = "poa-portal"

RULESET = f"""table inet {TABLE} {{
    set server4 {{ type ipv4_addr . inet_service; }}
    set server6 {{ type ipv6_addr . inet_service; }}
    set gateway4 {{ type ipv4_addr . inet_proto . inet_service; }}
    set target4 {{ type ipv4_addr . inet_service; }}

    chain input {{
        type filter hook input priority 0; policy drop;
        iif lo accept
        ct state established,related accept
        udp sport 67 udp dport 68 accept
        icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert }} accept
    }}

    chain output {{
        type filter hook output priority 0; policy drop;
        oif lo accept
        ct state established,related accept
        udp dport {{ 53, 67, 123 }} accept
        tcp dport 53 accept
        icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit }} accept
        ip daddr . tcp dport @server4 accept
        ip6 daddr . tcp dport @server6 accept
        ip daddr . meta l4proto . th dport @gateway4 accept
        oifname "tun*" ip daddr . th dport @target4 accept
        meta skuid "{PORTAL_USER}" tcp dport {{ 80, 443 }} accept
        counter comment "poa-drop"
    }}

    chain forward {{
        type filter hook forward priority 0; policy drop;
    }}
}}
"""


def resolve4(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


class Firewall:
    def __init__(self, runner=subprocess.run) -> None:
        self.run = runner

    def _nft(self, script: str) -> None:
        self.run(["nft", "-f", "-"], input=script, text=True, check=True)

    def load(self) -> None:
        """Ставит таблицу заново: прежнюю -- целиком прочь, одной транзакцией."""
        self._nft(f"table inet {TABLE}\ndelete table inet {TABLE}\n{RULESET}")

    def _replace(self, name: str, elements: list[str]) -> None:
        script = f"flush set inet {TABLE} {name}\n"
        if elements:
            script += f"add element inet {TABLE} {name} {{ {', '.join(elements)} }}\n"
        self._nft(script)

    def allow_server(self, host: str, port: int = 443) -> list[str]:
        """Сервер доступа: порт из его адреса и 80 для пробы портала."""
        addresses = resolve4(host)
        self._replace("server4", [f"{a} . {p}" for a in addresses for p in sorted({80, int(port)})])
        return addresses

    def allow_gateways(self, remotes: list[dict]) -> None:
        elements = []
        for remote in remotes:
            proto = "udp" if remote.get("proto") == "udp" else "tcp"
            for address in resolve4(remote["host"]):
                elements.append(f"{address} . {proto} . {int(remote['port'])}")
        self._replace("gateway4", elements)

    def allow_targets(self, targets: list[dict]) -> None:
        elements = []
        for target in targets:
            for address in resolve4(target["host"]):
                elements.append(f"{address} . {int(target['port'])}")
        self._replace("target4", elements)

    def close_session(self) -> None:
        """Конец сеанса: шлюзы и цели закрываются, сервер остаётся."""
        self._replace("gateway4", [])
        self._replace("target4", [])
