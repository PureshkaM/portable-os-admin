#!/usr/bin/env python3
"""Окно VNC: GtkVnc на весь экран, только VeNCrypt X509Plain.

Собственная аутентификация VNC (пароль до 8 знаков, DES) не допускается --
сценарий B3/V таблицы ИБ. Сервер обязан предложить VeNCrypt с подтипом
X509Plain; CA (или самоподписанный сертификат цели) приходит в профиле с
сервера доступа (`pin`, PEM), логин и пароль спрашиваются здесь же.
"""
from __future__ import annotations

import json
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkVnc", "2.0")
gi.require_version("GVnc", "1.0")
from gi.repository import Gdk, GLib, GVnc, Gtk, GtkVnc  # noqa: E402


#: Выход из сеанса: у полноэкранного окна нет ни меню, ни кнопки, а выход из
#: системы на цели (TigerVNC) сеанс VNC не закрывает. Сочетание, которое
#: на рабочем месте никому не нужно, -- перехватывается до GtkVnc.
EXIT_KEYS = "Ctrl+Alt+Shift+Q"
EXIT_HINT_SECONDS = 6


def is_exit_key(event) -> bool:
    mask = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SHIFT_MASK
    return (event.state & mask) == mask and Gdk.keyval_to_lower(event.keyval) == Gdk.KEY_q


#: Строка в stderr, по которой окно «Подключение» показывает причину отказа
#: (`poa_launch.session_error`).
ERROR_PREFIX = "POA-ERROR: "


def items(array) -> list:
    """Содержимое `GValueArray`, которым gtk-vnc передаёт типы защиты и
    нужные учётные данные. PyGObject в Debian 13 его не итерирует
    (`TypeError: 'ValueArray' object is not iterable` -- живая проверка
    03.10.2026: окно висело чёрным, ни тип, ни пароль не выбирались);
    `get_nth` есть во всех версиях."""
    try:
        return list(array)
    except TypeError:
        return [array.get_nth(i) for i in range(array.n_values)]


def ask_credentials(parent, user: str) -> tuple[str, str] | None:
    dialog = Gtk.Dialog(title="Вход", transient_for=parent, modal=True)
    dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Войти", Gtk.ResponseType.OK)
    box = dialog.get_content_area()
    login = Gtk.Entry(text=user)
    password = Gtk.Entry(visibility=False, activates_default=True)
    dialog.set_default_response(Gtk.ResponseType.OK)
    for label, entry in (("Пользователь", login), ("Пароль", password)):
        box.add(Gtk.Label(label=label, xalign=0))
        box.add(entry)
    dialog.show_all()
    ok = dialog.run() == Gtk.ResponseType.OK
    values = (login.get_text(), password.get_text())
    dialog.destroy()
    return values if ok else None


def main() -> int:
    target = json.loads(sys.argv[1])
    state = {"failed": False}
    window = Gtk.Window(title=target.get("name") or target["host"])
    window.fullscreen()
    display = GtkVnc.Display()
    overlay = Gtk.Overlay()
    overlay.add(display)
    hint = Gtk.Label(label=f"Выход из сеанса — {EXIT_KEYS}", margin=12)
    hint.set_halign(Gtk.Align.CENTER)
    hint.set_valign(Gtk.Align.START)
    hint.get_style_context().add_class("osd")
    overlay.add_overlay(hint)
    window.add(overlay)
    # Окно получает клавиши раньше GtkVnc: сочетание выхода на цель не уходит.
    window.connect("key-press-event",
                   lambda _w, event: (Gtk.main_quit(), True)[1] if is_exit_key(event) else False)
    display.connect("vnc-initialized",
                    lambda *_: GLib.timeout_add_seconds(EXIT_HINT_SECONDS, lambda: hint.hide()))
    connection = display.get_connection()

    # Тип и подтип выбирает сам GtkVnc.Display из своих предпочтений; наш
    # повторный set_auth_type будил его сопрограмму второй раз, и gtk-vnc
    # падал («Co-routine is yielding to no one», живая проверка 03.10.2026).
    # Поэтому здесь -- только проверка: не то, что нужно, -- отказ.
    verified = {"ca": False}

    def refuse(reason: str) -> None:
        print(f"{ERROR_PREFIX}{reason}", file=sys.stderr, flush=True)
        state["failed"] = True
        display.close()
        Gtk.main_quit()

    def choose_type(conn, types):
        if GVnc.ConnectionAuth.VENCRYPT not in items(types):
            refuse("Рабочее место не предлагает защищённый вход (VeNCrypt). Сообщите администратору.")

    def choose_subtype(conn, auth_type, subtypes):
        if GVnc.ConnectionAuthVencrypt.X509PLAIN not in items(subtypes):
            refuse("Рабочее место не предлагает вход по сертификату (X509Plain). Сообщите администратору.")

    def credential(disp, wanted):
        wanted = items(wanted)
        # gtk-vnc ждёт ВСЕ запрошенные значения; вместе с CA он просит имя
        # клиента (каталог клиентского сертификата). Для X509Plain клиентский
        # сертификат не нужен, но без имени соединение висело (03.10.2026).
        if GtkVnc.DisplayCredential.CLIENTNAME in wanted:
            disp.set_credential(GtkVnc.DisplayCredential.CLIENTNAME, "poa")
        if GtkVnc.DisplayCredential.CA_CERT_DATA in wanted:
            disp.set_credential(GtkVnc.DisplayCredential.CA_CERT_DATA, target["pin"])
            verified["ca"] = True
        if GtkVnc.DisplayCredential.PASSWORD not in wanted:
            return
        # Логин и пароль -- только в канал, проверенный нашим CA, и только
        # парой: пароль без логина -- это VNC-пароль (DES) внутри TLS, а
        # без запроса CA -- анонимный TLS, открытый «человеку посередине».
        if not verified["ca"] or GtkVnc.DisplayCredential.USERNAME not in wanted:
            refuse("Рабочее место просит пароль не тем способом, что задан администратором. "
                   "Подключение прервано.")
            return
        values = ask_credentials(window, target.get("user", ""))
        if values is None:
            display.close()
            Gtk.main_quit()
            return
        disp.set_credential(GtkVnc.DisplayCredential.USERNAME, values[0])
        disp.set_credential(GtkVnc.DisplayCredential.PASSWORD, values[1])

    def vnc_error(_disp, message):
        # «The certificate is not trusted» / «…hostname…» -- сертификат цели не
        # тот, что задал администратор (проверено на стенде 03.10.2026).
        text = (message or "").lower()
        if "certificate" in text:
            refuse("Сертификат рабочего места не совпадает с тем, что задал администратор. "
                   "Подключение прервано: это может быть подмена. Сообщите администратору.")
        elif "authentication" in text:
            refuse("Неверный пользователь или пароль.")
        else:
            refuse(f"Подключение не удалось: {message}")

    def auth_failure(_disp, message):
        refuse(f"Вход не выполнен: {message or 'неверный пользователь или пароль'}.")

    connection.connect("vnc-auth-choose-type", choose_type)
    connection.connect("vnc-auth-choose-subtype", choose_subtype)
    display.connect("vnc-auth-credential", credential)
    display.connect("vnc-auth-failure", auth_failure)
    display.connect("vnc-error", vnc_error)
    display.connect("vnc-disconnected", lambda *_: Gtk.main_quit())
    window.connect("destroy", lambda *_: Gtk.main_quit())
    display.open_host(target["host"], str(target["port"]))
    window.show_all()
    Gtk.main()
    return 2 if state["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
