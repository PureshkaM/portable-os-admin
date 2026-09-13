#!/usr/bin/env python3
"""Эталонный ресурс отзыва: отвечает носителям, не отозваны ли они.

Протокол описан в `docs/REVOCATION-SERVER.md`; здесь его исполнение, короткое
настолько, чтобы его можно было прочитать целиком и не гадать, что оно делает.

Что он делает. На `GET /state?media=<id>&nonce=<hex>` отвечает двумя строками:
сообщением `poa-state <id> <nonce> <состояние>` и его подписью в base64.
Подпись — Ed25519 закрытым ключом отзыва, который администратор выгружает из
контейнера («Обслуживание» → «Выгрузить для сервера…»).

Чего он не делает намеренно:

* **Не хранит ничего о пользователях.** Только список идентификаторов и их
  состояний — тот самый файл, что выгружает программа. Рабочих данных на
  носителях нет, они на рабочих компьютерах внутри организации.
* **Не решает, кого отзывать.** Решение принимает администратор в своей
  программе; сюда приезжает уже готовый список.
* **Не пытается быть умным про неизвестные носители.** Идентификатор, которого
  нет в списке, получает `ok`: неизвестность — не повод уничтожать данные.

Список перечитывается на каждом запросе, а не держится в памяти: отозвать
носитель должно быть можно, положив новый файл, без перезапуска службы и без
доступа к ней по SSH в тот момент, когда это срочно.
"""
from __future__ import annotations

import argparse
import base64
import http.server
import pathlib
import re
import sys
import urllib.parse

from cryptography.hazmat.primitives import serialization

#: Идентификатор носителя: восемь шестнадцатеричных знаков, как их выдаёт
#: программа. Проверяется до всего остального -- в подписанное сообщение
#: попадает то, что прислал носитель, и мусор туда попадать не должен.
MEDIA_RE = re.compile(r"\A[0-9a-f]{8}\Z")
#: Случайное число запроса. Тридцать два знака -- то, что шлёт носитель;
#: допускается и короче, но не пусто и не что попало.
NONCE_RE = re.compile(r"\A[0-9a-f]{8,64}\Z")


def load_states(path: pathlib.Path) -> dict[str, str]:
    """Читает файл состояний: по строке «<идентификатор> <состояние>».

    Ошибка чтения -- не повод отвечать «отозван» или падать: служба продолжает
    отвечать «ok». Обратное означало бы, что опечатка в файле или его временное
    отсутствие уничтожают носители по всей организации.
    """
    states: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"список состояний недоступен ({exc}); все носители считаются целыми",
              file=sys.stderr, flush=True)
        return states
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and MEDIA_RE.match(parts[0]):
            states[parts[0]] = "revoked" if parts[1] == "revoked" else "ok"
    return states


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "poa-revocation/1"
    key = None            # заполняется в main()
    states_path = None    # заполняется в main()

    def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/state":
            self.send_error(404, "unknown endpoint")
            return

        query = urllib.parse.parse_qs(parsed.query)
        media = (query.get("media") or [""])[0]
        nonce = (query.get("nonce") or [""])[0]
        if not MEDIA_RE.match(media) or not NONCE_RE.match(nonce):
            # Отказ, а не «ok»: подписывать то, что прислал кто попало, нельзя
            # -- подпись потом предъявят как наш ответ.
            self.send_error(400, "bad media or nonce")
            return

        state = load_states(self.states_path).get(media, "ok")
        message = f"poa-state {media} {nonce} {state}"
        signature = base64.b64encode(self.key.sign(message.encode("ascii")))
        body = message.encode("ascii") + b"\n" + signature + b"\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=ascii")
        self.send_header("Content-Length", str(len(body)))
        # Ответ на конкретное случайное число -- кэшировать его нельзя ни на
        # секунду, иначе посредник отдаст носителю чужой ответ.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # В journald, одной строкой на запрос: по этому журналу видно, какие
        # носители живы и когда отмечались, -- это и есть учёт парка.
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ресурс отзыва носителей")
    parser.add_argument("--key", required=True, type=pathlib.Path,
                        help="закрытый ключ отзыва в PEM (выгружает программа)")
    parser.add_argument("--states", required=True, type=pathlib.Path,
                        help="файл состояний: «<идентификатор> ok|revoked» по строке")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес; по умолчанию только локальный -- наружу "
                             "смотрит обратный прокси с TLS")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    Handler.key = key
    Handler.states_path = args.states

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ресурс отзыва слушает {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
