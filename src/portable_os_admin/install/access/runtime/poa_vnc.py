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
from gi.repository import GVnc, Gtk, GtkVnc  # noqa: E402


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
    window = Gtk.Window(title=target.get("name") or target["host"])
    window.fullscreen()
    display = GtkVnc.Display()
    window.add(display)
    connection = display.get_connection()

    def choose_type(conn, types):
        if GVnc.ConnectionAuth.VENCRYPT in [t for t in types]:
            conn.set_auth_type(GVnc.ConnectionAuth.VENCRYPT)
        else:
            Gtk.main_quit()          # нет VeNCrypt -- не подключаемся вовсе

    def choose_subtype(conn, auth_type, subtypes):
        if GVnc.ConnectionAuthVencrypt.X509PLAIN in [s for s in subtypes]:
            conn.set_auth_subtype(GVnc.ConnectionAuthVencrypt.X509PLAIN)
        else:
            Gtk.main_quit()

    def credential(disp, wanted):
        for item in wanted:
            if item == GtkVnc.DisplayCredential.CA_CERT_DATA:
                disp.set_credential(item, target["pin"])
        if any(c in (GtkVnc.DisplayCredential.USERNAME, GtkVnc.DisplayCredential.PASSWORD)
               for c in wanted):
            values = ask_credentials(window, target.get("user", ""))
            if values is None:
                Gtk.main_quit()
                return
            disp.set_credential(GtkVnc.DisplayCredential.USERNAME, values[0])
            disp.set_credential(GtkVnc.DisplayCredential.PASSWORD, values[1])

    connection.connect("vnc-auth-choose-type", choose_type)
    connection.connect("vnc-auth-choose-subtype", choose_subtype)
    display.connect("vnc-auth-credential", credential)
    display.connect("vnc-disconnected", lambda *_: Gtk.main_quit())
    window.connect("destroy", lambda *_: Gtk.main_quit())
    display.open_host(target["host"], str(target["port"]))
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
