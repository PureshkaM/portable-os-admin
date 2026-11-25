"""Сетевой фильтр носителя. Срок рабочего допуска исполняет ядро Linux."""
from __future__ import annotations

import ipaddress
import re
import secrets
import socket
import subprocess
import time
from pathlib import Path

TABLE = "poa"
PORTAL_USER = "poa-portal"
MAX_LEASE = 300

# Только служебные запросы и портал помечаются для получения ответа.
# В обоих направлениях каждый рабочий пакет проверяется по живому набору.
RULESET = f"""table inet {TABLE} {{
    set server4 {{ type ipv4_addr . inet_service; }}
    set server6 {{ type ipv6_addr . inet_service; }}
    set dns4 {{ type ipv4_addr; }}
    set dns6 {{ type ipv6_addr; }}
    set client_uid {{ type uid; }}
    set gateway4 {{ type ipv4_addr . inet_proto . inet_service; flags timeout; }}
    set target4 {{ type ifname . ipv4_addr . inet_service; flags timeout; }}

    set requests {{ typeof numgen inc mod 1; flags timeout; }}
    chain lease_in {{ }}
    chain lease_out {{ }}
    chain work_in {{
        ct direction reply ct state established iifname . ip saddr . tcp sport @target4 accept
        ct direction reply ct state established ip saddr . meta l4proto . th sport @gateway4 accept
    }}
    chain work_out {{
        meta skuid @client_uid oifname . ip daddr . tcp dport @target4 accept
        meta skuid 0 ip daddr . meta l4proto . th dport @gateway4 accept
    }}
    chain input {{
        type filter hook input priority 0; policy drop;
        iif lo accept
        jump lease_in
        iifname "tun*" drop
        ct direction reply ct state established ct mark 0x504f41 accept
        udp sport 67 udp dport 68 accept
        udp sport 547 udp dport 546 accept
        icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-advert }} accept
    }}
    chain output {{
        type filter hook output priority 0; policy drop;
        oif lo accept
        jump lease_out
        oifname "tun*" drop
        udp sport 68 udp dport 67 accept
        udp sport 546 udp dport 547 accept
        icmpv6 type {{ nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit }} accept
        ip daddr @dns4 meta l4proto {{ tcp, udp }} th dport 53 ct mark set 0x504f41 accept
        ip6 daddr @dns6 meta l4proto {{ tcp, udp }} th dport 53 ct mark set 0x504f41 accept
        meta skuid 0 ip daddr . tcp dport @server4 ct mark set 0x504f41 accept
        meta skuid 0 ip6 daddr . tcp dport @server6 ct mark set 0x504f41 accept
        meta skuid "{PORTAL_USER}" tcp dport {{ 80, 443 }} ct mark set 0x504f41 accept
        counter comment "poa-drop"
    }}
    chain forward {{
        type filter hook forward priority 0; policy drop;
    }}
}}
"""


def boottime() -> float:
    # На носителе обязательно Linux. На хосте разработки часы подменяют тесты.
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


def resolve4(host: str) -> list[str]:
    try:
        return sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except OSError:
        return []


def resolver_addresses() -> list:
    addresses = set()
    for path in ("/etc/resolv.conf", "/run/systemd/resolve/resolv.conf"):
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                try:
                    addresses.add(ipaddress.ip_address(fields[1].split("%", 1)[0]))
                except ValueError:
                    continue
    return sorted(addresses, key=lambda ip: (ip.version, int(ip)))


class Firewall:
    def __init__(self, runner=subprocess.run) -> None:
        self.run = runner

    def _nft(self, script: str) -> None:
        self.run(["nft", "-f", "-"], input=script, text=True, check=True)

    def load(self) -> None:
        self._nft(f"table inet {TABLE}\ndelete table inet {TABLE}\n{RULESET}")

    @staticmethod
    def _replace(name: str, elements: list[str]) -> str:
        script = f"flush set inet {TABLE} {name}\n"
        if elements:
            script += f"add element inet {TABLE} {name} {{ {', '.join(elements)} }}\n"
        return script

    def bind_client(self, uid: int) -> None:
        if type(uid) is not int or uid <= 0:
            raise ValueError("Нужен непривилегированный пользователь киоска")
        self._nft(self._replace("client_uid", [str(uid)]))

    def allow_server(self, host: str, port: int = 443) -> list[str]:
        resolvers = resolver_addresses()
        self._nft("".join(self._replace(f"dns{v}", [str(ip) for ip in resolvers if ip.version == v])
                          for v in (4, 6)))
        try:
            ips = {ipaddress.ip_address(r[4][0]) for r in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except OSError:
            ips = set()
        addresses = sorted(ips, key=lambda ip: (ip.version, int(ip)))
        self._nft("".join(self._replace(f"server{v}", [
            f"{a} . {p}" for a in addresses if a.version == v for p in sorted({80, int(port)})
        ]) for v in (4, 6)))
        return [str(ip) for ip in addresses]

    def begin_request(self) -> int:
        # Таймер ядра стартует до обращения к серверу. Приостановка процесса
        # между проверкой ответа и nft не продлевает пятиминутное окно.
        token = secrets.randbelow(2**31 - 1) + 1
        self._nft(f"add element inet {TABLE} requests {{ {token} timeout {MAX_LEASE - 1}s }}")
        return token

    def open_session(self, remotes: list[dict], target: dict, interface: str,
                     deadline: float, token: int) -> None:
        if type(token) is not int or not 0 < token < 2**31:
            raise ValueError("Нужен идентификатор запроса допуска")
        if not re.fullmatch(r"tun[0-9a-f]{1,12}", interface):
            raise ValueError("Некорректный интерфейс VPN")
        gateways = set()
        for remote in remotes:
            proto = remote["proto"]
            if proto not in ("udp", "tcp"):
                raise ValueError("Неизвестный транспорт VPN")
            for address in resolve4(remote["host"]):
                gateways.add(f"{address} . {proto} . {int(remote['port'])}")
        targets = [f'"{interface}" . {a} . {int(target["port"])}' for a in resolve4(target["host"])]
        # DNS может занять время. Рассчитываем оставшийся срок после разрешения имён.
        # Секундный запас покрывает округление таймеров ядра.
        ttl = min(MAX_LEASE, int(deadline - boottime())) - 1
        if ttl <= 0 or not gateways or not targets:
            self.close_session()
            raise ValueError("Срок допуска истёк или адрес назначения не определён")
        chains = "".join(f"flush chain inet {TABLE} lease_{d}\n"
                         f"add rule inet {TABLE} lease_{d} numgen inc mod 1 offset {token} @requests jump work_{d}\n"
                         for d in ("in", "out"))
        self._nft(chains + self._replace("gateway4", [f"{e} timeout {ttl}s" for e in sorted(gateways)])
                  + self._replace("target4", [f"{e} timeout {ttl}s" for e in targets]))

    def close_session(self) -> None:
        self._nft(self._replace("gateway4", []) + self._replace("target4", []) + self._replace("requests", []))

    def quarantine(self) -> None:
        # Изменение наборов запросом в полёте уже не восстановит правила accept.
        self._nft(f"flush chain inet {TABLE} work_in\nflush chain inet {TABLE} work_out\n"
                  + self._replace("gateway4", []) + self._replace("target4", []))
