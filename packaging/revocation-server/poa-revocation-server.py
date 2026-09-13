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

    def do_POST(self) -> None:  # noqa: N802 - имя задано базовым классом
        """Приём нового списка состояний от приложения администратора.

        Так администратору не нужен доступ к серверу: он не ходит сюда по SSH и
        не кладёт файлы руками, а подписывает список ключом, который не покидает
        его контейнера. Сервер знает только открытую часть -- проверить подпись
        может, выдать себя за администратора нет.

        Тело запроса: первая строка `poa-states <номер>`, дальше сам список.
        Подпись всего тела -- в заголовке `X-POA-Signature`, base64.

        Номер строго возрастает и хранится рядом со списком. Без него старую,
        когда-то подписанную версию списка можно было бы прислать снова и
        «оживить» отозванный носитель -- та же защита от повтора, что и с
        случайным числом в ответах носителям, только с другой стороны.
        """
        if self.admin_key is None:
            self.send_error(403, "admin publishing is not configured")
            return
        if urllib.parse.urlparse(self.path).path != "/admin/states":
            self.send_error(404, "unknown endpoint")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            self.send_error(400, "bad body size")
            return
        body = self.rfile.read(length)

        try:
            signature = base64.b64decode(self.headers.get("X-POA-Signature") or "", validate=True)
            self.admin_key.verify(signature, body)
        except Exception:
            # Одинаковый ответ на «подписи нет» и «подпись чужая»: разница
            # ничего не даёт тому, кто имеет право, и подсказывает тому, кто
            # подбирает.
            print("отклонено: подпись администратора не сходится", flush=True)
            self.send_error(403, "bad signature")
            return

        text = body.decode("utf-8", "replace")
        first, _, rest = text.partition("\n")
        parts = first.split()
        if len(parts) != 2 or parts[0] != "poa-states" or not parts[1].isdigit():
            self.send_error(400, "bad header line")
            return
        counter = int(parts[1])

        previous = 0
        counter_path = self.states_path.with_suffix(".counter")
        try:
            previous = int(counter_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            previous = 0
        if counter <= previous:
            print(f"отклонено: номер {counter} не больше прежнего {previous}", flush=True)
            self.send_error(409, "stale counter")
            return

        # Замена целиком и атомарно: список, обновлённый наполовину, означал бы
        # носители в неизвестном состоянии.
        temporary = self.states_path.with_suffix(".new")
        temporary.write_text(rest, encoding="utf-8")
        temporary.replace(self.states_path)
        counter_path.write_text(f"{counter}\n", encoding="ascii")

        revoked = sum(1 for line in rest.splitlines() if line.endswith(" revoked"))
        print(f"список принят: номер {counter}, отозванных {revoked}", flush=True)
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
    parser.add_argument("--admin-pubkey", type=pathlib.Path, default=None,
                        help="открытый ключ полномочий администратора в PEM; без "
                             "него приём списка по сети выключен")
    args = parser.parse_args()

    key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    Handler.key = key
    Handler.states_path = args.states
    if args.admin_pubkey is not None:
        Handler.admin_key = serialization.load_pem_public_key(
            args.admin_pubkey.read_bytes()
        )

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ресурс отзыва слушает {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
