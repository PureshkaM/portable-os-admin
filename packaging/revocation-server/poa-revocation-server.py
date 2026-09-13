#!/usr/bin/env python3
"""Ресурс отзыва: отвечает носителям и принимает команды администраторов.

Протокол описан в `docs/REVOCATION-SERVER.md`; здесь его исполнение, короткое
настолько, чтобы его можно было прочитать целиком.

Две стороны, и обе подписаны:

**Носитель спрашивает.** `GET /state?media=<id>&nonce=<hex>` -- в ответ
сообщение `poa-state <id> <nonce> <состояние>` и подпись ключом отзыва.
Случайное число носителя подписывается вместе с ответом, поэтому вчерашний
ответ сегодня не подойдёт.

**Администратор распоряжается.** `POST /admin/media/<id>` -- тело
`poa-media <id> <номер> <состояние>`, подпись ключом полномочий, который не
покидает контейнера администратора. Сервер знает только открытую часть: он
может проверить команду, но не выдать себя за администратора.

Почему база, а не общий список. Администраторов может быть несколько, и
публикация списка целиком означала бы, что любой из них переписывает чужие
носители -- в том числе снимает отзыв, поставленный другим. Здесь у каждого
носителя своя запись и свой владелец: **менять запись может только тот ключ,
которым носитель был создан**. Неизвестный идентификатор -- это выпуск нового
носителя, запись заводится, и владельцем становится предъявивший ключ.

Следствие, которое надо знать заранее: администратор, потерявший свой
контейнер, теряет и управление своими носителями. Правит их тогда владелец
сервера -- руками в базе, у него для этого есть машина.

Счётчик у каждой записи свой и строго возрастает: однажды подписанную команду
про этот носитель нельзя прислать повторно, чтобы «оживить» отозванный.
"""
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import http.server
import pathlib
import re
import sqlite3
import sys
import urllib.parse

from cryptography.hazmat.primitives import serialization

#: Идентификатор носителя: восемь шестнадцатеричных знаков, как их выдаёт
#: программа. Проверяется до всего остального -- в подписанное сообщение
#: попадает то, что прислали, и мусор туда попадать не должен.
MEDIA_RE = re.compile(r"\A[0-9a-f]{8}\Z")
#: Случайное число запроса носителя.
NONCE_RE = re.compile(r"\A[0-9a-f]{8,64}\Z")
#: Состояния, которые вообще бывают. Всё прочее -- отказ, а не «ну ладно».
STATES = ("ok", "revoked")


def fingerprint(public_key) -> str:
    """Короткое имя ключа администратора: по нему запись знает своего владельца.

    Полный ключ в базе не нужен -- он и так есть у сервера в списке допущенных,
    -- а отпечаток читаем глазами и одинаков у обеих сторон.
    """
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return hashlib.sha256(raw).hexdigest()[:16]


def load_admins(directory: pathlib.Path) -> dict[str, object]:
    """Допущенные администраторы: их открытые ключи кладёт владелец сервера.

    Список, а не «кто угодно с ключом»: иначе посторонний мог бы заранее занять
    чужие идентификаторы носителей и не дать выпустить их по-настоящему.
    """
    admins: dict[str, object] = {}
    if directory is None:
        return admins
    for path in sorted(directory.glob("*.pem")):
        try:
            key = serialization.load_pem_public_key(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - причина в журнал, файл пропускаем
            print(f"ключ {path.name} не читается: {exc}", file=sys.stderr, flush=True)
            continue
        admins[fingerprint(key)] = key
        print(f"допущен администратор {fingerprint(key)} ({path.name})", flush=True)
    return admins


class Database:
    """Записи о носителях. Одна таблица -- больше здесь хранить нечего."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS media ("
                " media_id TEXT PRIMARY KEY,"
                " owner TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " counter INTEGER NOT NULL,"
                " updated_at TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        # Соединение на операцию, а не одно на всю жизнь службы: запросов
        # единицы в час, а так база остаётся согласованной даже если служба
        # умрёт посреди работы.
        return sqlite3.connect(self.path, timeout=10)

    def state_of(self, media_id: str) -> str:
        """Состояние носителя. Неизвестный -- «ok».

        Неизвестность не повод уничтожать данные: носитель мог быть выпущен
        другим администратором, чья запись сюда ещё не доехала.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT state FROM media WHERE media_id = ?", (media_id,)
            ).fetchone()
        return row[0] if row else "ok"

    def record_of(self, media_id: str) -> tuple[str, str, int] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT owner, state, counter FROM media WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        return (row[0], row[1], row[2]) if row else None

    def put(self, media_id: str, owner: str, state: str, counter: int) -> None:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                "INSERT INTO media (media_id, owner, state, counter, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(media_id) DO UPDATE SET"
                " state = excluded.state, counter = excluded.counter,"
                " updated_at = excluded.updated_at",
                (media_id, owner, state, counter, now),
            )


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "poa-revocation/2"
    key = None       # ключ подписи ответов носителям
    admins: dict = {}  # допущенные администраторы по отпечатку
    db = None

    # -- носитель спрашивает -------------------------------------------------
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

        state = self.db.state_of(media)
        message = f"poa-state {media} {nonce} {state}"
        signature = base64.b64encode(self.key.sign(message.encode("ascii")))
        body = message.encode("ascii") + b"\n" + signature + b"\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=ascii")
        self.send_header("Content-Length", str(len(body)))
        # Ответ на конкретное случайное число: кэшировать его нельзя ни на
        # секунду, иначе посредник отдаст носителю чужой ответ.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- администратор распоряжается ----------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        prefix = "/admin/media/"
        if not parsed.path.startswith(prefix):
            self.send_error(404, "unknown endpoint")
            return
        media = parsed.path[len(prefix):]
        if not MEDIA_RE.match(media):
            self.send_error(400, "bad media id")
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 4096:
            self.send_error(400, "bad body size")
            return
        body = self.rfile.read(length)

        key_id = (self.headers.get("X-POA-Key-Id") or "").strip()
        admin = self.admins.get(key_id)
        if admin is None:
            print(f"отклонено: ключ {key_id or '(нет)'} не допущен", flush=True)
            self.send_error(403, "unknown administrator")
            return
        try:
            signature = base64.b64decode(self.headers.get("X-POA-Signature") or "",
                                         validate=True)
            admin.verify(signature, body)
        except Exception:
            # Одинаковый ответ на «подписи нет» и «подпись чужая»: разница
            # ничего не даёт тому, кто имеет право, и подсказывает тому, кто
            # подбирает.
            print(f"отклонено: подпись {key_id} не сходится", flush=True)
            self.send_error(403, "bad signature")
            return

        parts = body.decode("utf-8", "replace").strip().split()
        if len(parts) != 4 or parts[0] != "poa-media" or parts[1] != media:
            self.send_error(400, "bad command")
            return
        if not parts[2].isdigit() or parts[3] not in STATES:
            self.send_error(400, "bad counter or state")
            return
        counter, state = int(parts[2]), parts[3]

        record = self.db.record_of(media)
        if record is None:
            # Неизвестный носитель -- это его выпуск. Владельцем становится
            # тот, кто его создал, и дальше распоряжаться им может только он.
            self.db.put(media, key_id, state, counter)
            print(f"заведён носитель {media}: владелец {key_id}, состояние {state}",
                  flush=True)
            self.send_response(201)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        owner, _, previous = record
        if owner != key_id:
            # Администраторов несколько, и чужие носители -- не его дело:
            # иначе один снимал бы отзыв, поставленный другим.
            print(f"отклонено: {key_id} не владелец носителя {media} (владелец {owner})",
                  flush=True)
            self.send_error(403, "not the owner")
            return
        if counter <= previous:
            print(f"отклонено: номер {counter} не больше прежнего {previous}", flush=True)
            self.send_error(409, "stale counter")
            return

        self.db.put(media, owner, state, counter)
        print(f"носитель {media}: состояние {state} (номер {counter})", flush=True)
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
                        help="закрытый ключ отзыва в PEM: им подписываются ответы носителям")
    parser.add_argument("--db", required=True, type=pathlib.Path,
                        help="файл базы с записями о носителях")
    parser.add_argument("--admins", type=pathlib.Path, default=None,
                        help="каталог с открытыми ключами допущенных администраторов "
                             "(*.pem); без него приём команд выключен")
    parser.add_argument("--host", default="127.0.0.1",
                        help="адрес; по умолчанию только локальный -- наружу "
                             "смотрит обратный прокси с TLS")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    Handler.key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    Handler.db = Database(args.db)
    Handler.admins = load_admins(args.admins)

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ресурс отзыва слушает {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
