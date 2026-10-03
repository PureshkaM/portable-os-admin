#!/usr/bin/env python3
"""Окно страницы входа в сеть (LIVE-2.0, Э14): гостиница, аэропорт, кафе.

Запускает `poa-accessd` под пользователем `poa-portal` (только ему сетевой
экран открывает веб) в юните с пределом времени. Окно запертое:

* нет адресной строки -- ходить можно только туда, куда ведёт сама страница;
* только http и https: file://, data: и прочее не открываются;
* загрузки отменяются, выбор файлов не открывается;
* новые окна не создаются -- ссылка открывается в этом же;
* данные эфемерные: куки и кэш живут, пока живо окно.

Закрывается само, как только проба снова видит наш сервер: сеть открыта, и
окно «Подключение» продолжит без него.
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import GLib, Gtk, WebKit2  # noqa: E402

PROBE_BODY = b"poa-probe 1\n"
SCHEMES = ("http://", "https://")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def network_open(probe_url: str) -> bool:
    """Ответил ли наш сервер, а не портал (без перенаправлений, побайтно)."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(probe_url, timeout=4) as response:
            return response.status == 200 and response.read(4096) == PROBE_BODY
    except Exception:  # noqa: BLE001 - портал отвечает чем угодно, это не ошибка
        return False


def allowed(uri: str) -> bool:
    return uri.startswith(SCHEMES)


def main() -> int:
    start, probe_url = sys.argv[1], sys.argv[2]
    if not allowed(start):
        return 2
    context = WebKit2.WebContext.new_ephemeral()
    context.connect("download-started", lambda _ctx, download: download.cancel())
    view = WebKit2.WebView.new_with_context(context)
    settings = view.get_settings()
    settings.set_enable_developer_extras(False)
    settings.set_javascript_can_open_windows_automatically(False)

    def decide(_view, decision, kind):
        if kind in (WebKit2.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION):
            uri = decision.get_navigation_action().get_request().get_uri()
            if not allowed(uri):
                decision.ignore()
                return True
            if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION:
                decision.ignore()
                view.load_uri(uri)          # новое окно -- в этом же
                return True
        return False

    view.connect("decide-policy", decide)
    view.connect("create", lambda *_: None)
    view.connect("run-file-chooser", lambda *_: True)
    view.connect("context-menu", lambda *_: True)

    window = Gtk.Window(title="Вход в сеть")
    window.set_default_size(1024, 720)
    window.add(view)
    window.connect("destroy", lambda *_: Gtk.main_quit())

    def watch() -> None:
        # В потоке: проба ждёт до 4 с, и страница не должна замирать на это время.
        while not network_open(probe_url):
            time.sleep(4)
        GLib.idle_add(Gtk.main_quit)

    threading.Thread(target=watch, daemon=True).start()
    view.load_uri(start)
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
