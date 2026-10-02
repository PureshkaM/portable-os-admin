"""Логика окна «Подключение» без GTK: разговор со службой и запуск клиентов.

Вынесена отдельно, чтобы её можно было проверить без экрана. Окно
(`poa_connect.py`) только показывает то, что здесь решено.

Пароль рабочего места не попадает ни в `argv` (его видно в `ps`), ни на диск:
FreeRDP получает его через stdin (`/from-stdin`), окна VNC и SSH спрашивают его
сами внутри своего протокола.
"""
from __future__ import annotations

import json
import os
import shutil
import socket

SOCKET = os.environ.get("POA_ACCESS_SOCKET", "/run/poa/access.sock")
RUNTIME = os.path.dirname(os.path.abspath(__file__))


def ask(request: dict, path: str = SOCKET, timeout: float = 120.0) -> dict:
    """Один запрос к poa-accessd. Ошибка связи -- {"error": "service"}."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(timeout)
            conn.connect(path)
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            return json.loads(conn.makefile("rb").readline() or b"{}")
    except (OSError, ValueError):
        return {"error": "service"}


#: Что сказать человеку на отказ службы или сервера. Коротко: окно не место
#: для объяснения протокола, а позвонить администратору проще с ясной фразой.
MESSAGES = {
    "totp": "Неверный код. Дождитесь следующего кода в приложении и введите его.",
    "blocked": "Носитель заблокирован. Обратитесь к администратору.",
    "clone": "Обнаружена копия носителя. Носитель заблокирован — обратитесь к администратору.",
    "already_activated": "Носитель уже активирован. Если это были не вы — сообщите администратору.",
    "expired": "Код активации истёк. Попросите у администратора новый.",
    "update_required": "Требуется обновление системы. Оно загружается — не выключайте компьютер.",
    "virtualization": "Работа в виртуальной машине запрещена.",
    "forbidden": "Сервер отказал в доступе. Обратитесь к администратору.",
    "tls_pin": "Сервер не прошёл проверку подлинности. Возможна атака — не продолжайте и сообщите администратору.",
    "service": "Служба доступа не отвечает. Перезагрузите компьютер.",
}


def explain(answer: dict) -> str:
    reason = answer.get("error", "")
    if reason.startswith("network"):
        return "Нет связи с сервером. Проверьте подключение к сети."
    return MESSAGES.get(reason, "Не удалось выполнить запрос. Обратитесь к администратору.")


def reachability_text(state: str) -> str:
    return {"up": "", "refused": "Рабочее место отклонило подключение",
            "silent": "Рабочее место не отвечает (возможно, выключено)"}.get(state, "")


def freerdp_binary() -> str:
    for name in ("xfreerdp3", "xfreerdp", "sdl-freerdp3"):
        found = shutil.which(name)
        if found:
            return found
    return "xfreerdp"


def client_command(target: dict) -> tuple[list[str], bool]:
    """(argv, нужен ли пароль через stdin) для цели.

    RDP: на весь экран, NLA, сертификат цели закреплён отпечатком, диски,
    принтеры и USB не пробрасываются (их нет среди ключей -- в FreeRDP 3 они
    выключены по умолчанию), смарт-карта -- только если цель этого требует.
    """
    kind = target["type"]
    if kind == "rdp":
        argv = [freerdp_binary(), f"/v:{target['host']}:{target['port']}",
                f"/u:{target.get('user', '')}", "/f", "/sec:nla", "/from-stdin",
                "/dynamic-resolution", "-decorations"]
        if target.get("domain"):
            argv.append(f"/d:{target['domain']}")
        pin = target.get("pin", "")
        if pin.startswith("sha256:"):
            argv.append(f"/cert:fingerprint:sha256:{pin[len('sha256:'):]}")
        if target.get("smartcard"):
            argv.append("/smartcard")
        return argv, True
    if kind == "ssh":
        return ["python3", os.path.join(RUNTIME, "poa_ssh.py"), json.dumps(target)], False
    if kind == "vnc":
        return ["python3", os.path.join(RUNTIME, "poa_vnc.py"), json.dumps(target)], False
    raise ValueError(f"неизвестный тип цели: {kind}")
