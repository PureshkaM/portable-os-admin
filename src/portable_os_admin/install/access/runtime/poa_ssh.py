#!/usr/bin/env python3
"""Окно SSH: терминал VTE, в котором запущен только `ssh` к одной цели.

Не терминал общего назначения: локальной оболочки нет -- процесс окна и есть
`ssh`, а когда он завершается, окно закрывается. Меню нет. Ключ хоста закреплён
по профилю с сервера (`pin` вида `ssh-ed25519 AAAA…`), чужой -- отказ ssh.
Пробросы, агент, X11 и локальные команды выключены, escape-символ тоже: через
`~C` из ssh можно открыть пробросы.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # noqa: E402


def ssh_argv(target: dict, known_hosts: str) -> list[str]:
    return [
        "/usr/bin/ssh", "-F", "/dev/null",
        "-o", "EscapeChar=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
        "-o", "PermitLocalCommand=no", "-o", "ClearAllForwardings=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
        "-o", "GlobalKnownHostsFile=/dev/null", "-o", "UpdateHostKeys=no",
        "-p", str(target["port"]), f"{target.get('user', '')}@{target['host']}",
    ]


def known_hosts_line(target: dict) -> str:
    host = target["host"] if int(target["port"]) == 22 else f"[{target['host']}]:{target['port']}"
    return f"{host} {target['pin']}\n"


def main() -> int:
    target = json.loads(sys.argv[1])
    runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    fd, known = tempfile.mkstemp(prefix="poa-known-", dir=runtime)
    with os.fdopen(fd, "w") as handle:
        handle.write(known_hosts_line(target))

    window = Gtk.Window(title=target.get("name") or target["host"])
    window.fullscreen()
    terminal = Vte.Terminal()
    window.add(terminal)
    terminal.connect("child-exited", lambda *_: Gtk.main_quit())
    window.connect("destroy", lambda *_: Gtk.main_quit())
    terminal.spawn_async(Vte.PtyFlags.DEFAULT, None, ssh_argv(target, known), [],
                         GLib.SpawnFlags.DEFAULT, None, None, -1, None, None)
    window.show_all()
    Gtk.main()
    os.unlink(known)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
