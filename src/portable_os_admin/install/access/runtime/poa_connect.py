#!/usr/bin/env python3
"""Окно «Подключение» (LIVE-2.0, этап Э8): от сети до сеанса на весь экран.

Сценарий согласован 01.10.2026:
1. нет сети -- окно ждёт; сеть выбирается значком в панели;
2. сервер ответил подписанным «ok» (`/run/poa/authorized`) -- появляется поле
   кода; при первом запуске -- активация (код от администратора, затем QR);
3. код TOTP -- служба получает доступ, поднимает VPN;
4. проверка рабочих мест; одно -- сразу пароль, несколько -- список;
5. сеанс на весь экран; после него -- «Подключиться снова» без нового кода,
   пока жив доступ.

Окно не знает ни ключей, ни протокола: всё через `poa-accessd` (`poa_launch`).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poa_launch  # noqa: E402
import poa_qr  # noqa: E402

POLL_SECONDS = 2


class ConnectWindow(Gtk.Window):
    def __init__(self) -> None:
        super().__init__(title="Подключение")
        self.set_default_size(560, 420)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.stack = Gtk.Stack()
        # Строка об обновлении системы (Э9) -- над любым экраном окна.
        self.update_label = Gtk.Label(xalign=0, wrap=True, margin=12)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(self.update_label, False, False, 0)
        outer.pack_start(self.stack, True, True, 0)
        self.add(outer)
        self.message = ""
        self.targets: list[dict] = []
        self.qr_modules = None
        self.connect("destroy", Gtk.main_quit)
        self._build()
        # Gtk.Stack не переключается на страницу, чьё содержимое ещё не
        # показано, а первый опрос идёт до show_all() окна: без этой строки
        # окно открывалось пустым (найдено тестом под Xvfb 01.10.2026).
        self.stack.show_all()
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll_update)
        self._poll()

    # -- экраны -------------------------------------------------------------
    def _page(self, name: str, *widgets) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin=32)
        for widget in widgets:
            box.pack_start(widget, False, False, 0)
        self.stack.add_named(box, name)

    def _label(self, text: str = "", big: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text, wrap=True, xalign=0)
        if big:
            label.set_markup(f"<big>{GLib.markup_escape_text(text)}</big>")
        return label

    def _build(self) -> None:
        self.wait_label = self._label("Подключитесь к сети значком в панели.", big=True)
        self._page("wait", self.wait_label)

        self.code_entry = Gtk.Entry(placeholder_text="XXXXX-XXXXX")
        self.code_entry.connect("activate", self._on_activate)
        activate_button = Gtk.Button(label="Активировать")
        activate_button.connect("clicked", self._on_activate)
        self.activate_error = self._label()
        self._page("activate", self._label("Код активации от администратора", big=True),
                   self.code_entry, activate_button, self.activate_error)

        self.qr_area = Gtk.DrawingArea()
        self.qr_area.set_size_request(260, 260)
        self.qr_area.connect("draw", self._draw_qr)
        qr_done = Gtk.Button(label="Отсканировал")
        qr_done.connect("clicked", lambda *_: self.stack.set_visible_child_name("totp"))
        self._page("qr", self._label("Отсканируйте код приложением-аутентификатором. "
                                     "Он показывается один раз.", big=True),
                   self.qr_area, qr_done)

        self.totp_entry = Gtk.Entry(placeholder_text="123456", max_length=6)
        self.totp_entry.connect("activate", self._on_login)
        self.totp_entry.connect("activate", self._on_login)
        login_button = Gtk.Button(label="Войти")
        login_button.connect("clicked", self._on_login)
        self.totp_error = self._label()
        self._page("totp", self._label("Код из приложения-аутентификатора", big=True),
                   self.totp_entry, login_button, self.totp_error)

        self.busy_label = self._label("Получаем доступ…", big=True)
        spinner = Gtk.Spinner(active=True)
        self._page("busy", self.busy_label, spinner)

        self.targets_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.targets_error = self._label()
        # Место могли включить позже, чем пользователь вошёл: перепроверка
        # без нового TOTP, пока жив доступ к VPN.
        recheck = Gtk.Button(label="Проверить снова")
        recheck.connect("clicked", lambda *_: self._show_targets())
        self._page("targets", self._label("Рабочее место", big=True), self.targets_box,
                   self.targets_error, recheck)

        again = Gtk.Button(label="Подключиться снова")
        again.connect("clicked", lambda *_: self._show_targets())
        self._page("again", self._label("Сеанс завершён.", big=True), again)

    # -- опрос службы ---------------------------------------------------------
    def _poll(self) -> bool:
        current = self.stack.get_visible_child_name()
        if current not in (None, "wait"):
            return True
        status = poa_launch.ask({"cmd": "status"}, timeout=5)
        if status.get("error"):
            self.wait_label.set_text(poa_launch.explain(status))
        elif not status.get("authorized"):
            self.wait_label.set_text("Подключитесь к сети значком в панели. "
                                     "Ждём ответа сервера…")
        elif status.get("vpn") == "up" and status.get("targets"):
            self.targets = status["targets"]
            self._show_targets()
        else:
            self.stack.set_visible_child_name("totp" if status.get("activated") else "activate")
        return True

    def _poll_update(self) -> bool:
        status = poa_launch.ask({"cmd": "status"}, timeout=5)
        text = poa_launch.update_text(status.get("update") or {})
        self.update_label.set_text(text)
        self.update_label.set_visible(bool(text))
        return True

    # -- действия ---------------------------------------------------------------
    def _background(self, request: dict, done) -> None:
        def work():
            answer = poa_launch.ask(request)
            GLib.idle_add(done, answer)
        threading.Thread(target=work, daemon=True).start()

    def _on_activate(self, *_):
        code = self.code_entry.get_text().strip()
        if not code:
            return
        self.stack.set_visible_child_name("busy")

        def done(answer):
            if answer.get("error"):
                self.activate_error.set_text(poa_launch.explain(answer))
                self.stack.set_visible_child_name("activate")
                return
            self.qr_modules = poa_qr.encode(answer["otpauth"])
            self.stack.set_visible_child_name("qr")
            self.qr_area.queue_draw()
        self._background({"cmd": "activate", "code": code}, done)

    def _on_login(self, *_):
        code = self.totp_entry.get_text().strip()
        if len(code) != 6:
            return
        self.totp_entry.set_text("")
        self.stack.set_visible_child_name("busy")

        def done(answer):
            if answer.get("error"):
                self.totp_error.set_text(poa_launch.explain(answer))
                self.stack.set_visible_child_name("totp")
                return
            self.targets = answer.get("targets", [])
            self._wait_vpn_then_targets()
        self._background({"cmd": "login", "totp": code}, done)

    def _wait_vpn_then_targets(self, deadline: float | None = None) -> None:
        deadline = deadline or time.time() + 60
        status = poa_launch.ask({"cmd": "status"}, timeout=5)
        if status.get("vpn") == "up":
            self._show_targets()
        elif time.time() > deadline:
            self.totp_error.set_text("Не удалось подключиться к сети компании.")
            self.stack.set_visible_child_name("totp")
        else:
            GLib.timeout_add_seconds(2, lambda: self._wait_vpn_then_targets(deadline) and False)

    def _show_targets(self) -> None:
        for child in self.targets_box.get_children():
            self.targets_box.remove(child)
        reach = poa_launch.ask({"cmd": "check"}, timeout=30).get("reachability", {})
        usable = []
        for target in self.targets:
            state = reach.get(target["id"], "silent")
            button = Gtk.Button(label=target.get("name") or target["host"])
            button.set_sensitive(state == "up")
            button.connect("clicked", lambda _b, t=target: self._open(t))
            self.targets_box.pack_start(button, False, False, 0)
            note = poa_launch.reachability_text(state)
            if note:
                self.targets_box.pack_start(self._label(note), False, False, 0)
            if state == "up":
                usable.append(target)
        self.targets_box.show_all()
        self.stack.set_visible_child_name("targets")
        if len(self.targets) == 1 and len(usable) == 1:
            self._open(usable[0])

    def _open(self, target: dict) -> None:
        argv, needs_password = poa_launch.client_command(target)
        password = None
        if needs_password:
            password = self._ask_password(target)
            if password is None:
                return
        self.hide()

        def run():
            process = subprocess.Popen(argv, stdin=subprocess.PIPE if password is not None else None)
            if password is not None:
                process.stdin.write(password.encode("utf-8") + b"\n")
                process.stdin.close()
            process.wait()
            GLib.idle_add(self._session_over)
        threading.Thread(target=run, daemon=True).start()

    def _session_over(self) -> None:
        self.show_all()
        self.stack.set_visible_child_name("again")

    def _ask_password(self, target: dict) -> str | None:
        dialog = Gtk.Dialog(title="Пароль", transient_for=self, modal=True)
        dialog.add_buttons("Отмена", Gtk.ResponseType.CANCEL, "Подключиться", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        who = target.get("user", "")
        if target.get("domain"):
            who = f"{target['domain']}\\{who}"
        box.add(self._label(f"Пароль рабочего места {who}".strip()))
        entry = Gtk.Entry(visibility=False, activates_default=True)
        box.add(entry)
        dialog.show_all()
        ok = dialog.run() == Gtk.ResponseType.OK
        value = entry.get_text()
        dialog.destroy()
        return value if ok and value else None

    def _draw_qr(self, area, cr) -> None:
        if not self.qr_modules:
            return
        size = len(self.qr_modules) + 8
        scale = min(area.get_allocated_width(), area.get_allocated_height()) / size
        cr.set_source_rgb(1, 1, 1)
        cr.paint()
        cr.set_source_rgb(0, 0, 0)
        for y, row in enumerate(self.qr_modules):
            for x, dark in enumerate(row):
                if dark:
                    cr.rectangle((x + 4) * scale, (y + 4) * scale, scale, scale)
        cr.fill()


def main() -> int:
    window = ConnectWindow()
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
