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

    RDP: на весь экран, NLA (или TLS для серверов без NLA -- xrdp), сертификат цели закреплён отпечатком, диски,
    принтеры и USB не пробрасываются (их нет среди ключей -- в FreeRDP 3 они
    выключены по умолчанию), смарт-карта -- только если цель этого требует.
    """
    kind = target["type"]
    if kind == "rdp":
        argv = [freerdp_binary(), f"/v:{target['host']}:{target['port']}",
                f"/u:{target.get('user', '')}", "/f",
                f"/sec:{'tls' if target.get('security') == 'tls' else 'nla'}", "/from-stdin",
                "/dynamic-resolution", "-decorations",
                # Домен -- всегда, даже пустой: иначе /from-stdin спрашивает
                # сначала домен, пароль уходит в него, а сам пароль получает
                # EOF (живая загрузка 02.10.2026: ERRCONNECT_CONNECT_CANCELLED).
                f"/d:{target.get('domain', '')}"]
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


#: Причины отказа FreeRDP по имени ошибки из его журнала. Имена стабильнее
#: кодов выхода; первые пять сняты с живого FreeRDP 3 против xrdp стенда
#: (03.10.2026), отказы входа Windows -- по исходникам FreeRDP, вживую не
#: проверены (Windows на стенде нет).
RDP_REASONS = (
    (("ERRCONNECT_TLS_CONNECT_FAILED", "Host key verification failed"),
     "Сертификат рабочего места не совпадает с тем, что задал администратор. "
     "Подключение прервано: это может быть подмена. Сообщите администратору."),
    (("ERRCONNECT_SECURITY_NEGO_CONNECT_FAILED",),
     "Рабочее место не поддерживает способ входа, заданный администратором (NLA или TLS)."),
    (("ERRCONNECT_DNS_NAME_NOT_FOUND",), "Адрес рабочего места не найден."),
    (("ERRCONNECT_CONNECT_FAILED", "ERRCONNECT_CONNECT_TRANSPORT_FAILED"),
     "Рабочее место не отвечает или оборвало соединение."),
    (("ERRCONNECT_LOGON_FAILURE", "ERRCONNECT_WRONG_PASSWORD", "ERRCONNECT_AUTHENTICATION_FAILED"),
     "Неверный пользователь или пароль."),
    (("ERRCONNECT_ACCOUNT_LOCKED_OUT",), "Учётная запись заблокирована."),
    (("ERRCONNECT_ACCOUNT_DISABLED", "ERRCONNECT_ACCOUNT_EXPIRED"),
     "Учётная запись отключена или срок её действия истёк."),
    (("ERRCONNECT_PASSWORD_EXPIRED", "ERRCONNECT_PASSWORD_MUST_CHANGE",
      "ERRCONNECT_PASSWORD_CERTAINLY_EXPIRED"),
     "Срок пароля истёк — его нужно сменить. Обратитесь к администратору."),
    (("ERRCONNECT_ACCESS_DENIED", "ERRCONNECT_LOGON_TYPE_NOT_GRANTED",
      "ERRCONNECT_INSUFFICIENT_PRIVILEGES"),
     "Этой учётной записи вход на рабочее место запрещён."),
)


def session_error(kind: str, returncode: int, output: list[str]) -> str:
    """Почему сеанс не состоялся -- словами для человека; "" если всё штатно.

    Только RDP: у FreeRDP коды выхода ниже 128 -- штатные концы сеанса
    (отключение, выход из системы, простой), от 128 -- отказы подключения.
    SSH и VNC идут через свои обёртки и сообщают о себе в своём окне.
    """
    if kind != "rdp" or returncode < 128:
        return ""
    text = "\n".join(output)
    for markers, reason in RDP_REASONS:
        if any(marker in text for marker in markers):
            return reason
    errors = [line.split(" - ", 1)[-1] for line in output if "[ERROR]" in line]
    detail = errors[-1][:200] if errors else f"код {returncode}"
    return f"Подключение не удалось: {detail}"


def update_text(state: dict) -> str:
    """Что сказать человеку об обновлении системы (Э9)."""
    kind = state.get("state")
    if kind in ("checking", "downloading"):
        total, done = int(state.get("total") or 0), int(state.get("done") or 0)
        share = f" ({done * 100 // total}%)" if total else ""
        return f"Загружается обновление системы{share}. Не выключайте компьютер."
    if kind == "ready":
        return "Обновление загружено и применится при следующем включении."
    if kind == "failed":
        return "Обновление не загрузилось; попробуем при следующем входе."
    return ""
