#!/usr/bin/env python3
"""Ресурс отзыва: отвечает носителям и принимает команды администраторов.

Протокол описан в `docs/REVOCATION-SERVER.md` (договор -- раздел «Протокол
отзыва v2» в `docs/RELEASE-1.1.md`); здесь его исполнение, короткое настолько,
чтобы его можно было прочитать целиком.

Три стороны, и все подписаны:

**Носитель спрашивает.** `GET /v2/state`, всё в заголовках: идентификатор,
случайное число, сведения о себе и подпись строки `poa-ask <id> <nonce> <info>`
ключом носителя. В ответ -- `poa-state <id> <nonce> <состояние>` и подпись
ключом отзыва. Случайное число подписывается вместе с ответом, поэтому
вчерашний ответ сегодня не подойдёт; а подпись запроса не даёт постороннему
узнать состояние носителя и отметить его живым. Всё, что не прошло проверку,
получает 403 с одним и тем же телом.

**Администратор распоряжается.** `POST /admin/media/<id>` -- тело
`poa-media <id> <номер> <состояние> [<ключ носителя>]`, подпись ключом
полномочий, который не покидает контейнера администратора. Сервер знает только
открытую часть: он может проверить команду, но не выдать себя за
администратора.

**Администратор спрашивает.** `GET /admin/media/<id>` -- подписанная строка
`poa-query <id> <время>`, в ответ состояние и последняя связь носителя.

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
import binascii
import datetime
import hashlib
import http.server
import pathlib
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

#: Идентификатор носителя: восемь шестнадцатеричных знаков, как их выдаёт
#: программа. Проверяется до всего остального -- в подписанное сообщение
#: попадает то, что прислали, и мусор туда попадать не должен.
MEDIA_RE = re.compile(r"\A[0-9a-f]{8}\Z")
#: Случайное число запроса носителя: 16 байт в шестнадцатеричном виде.
NONCE_RE = re.compile(r"\A[0-9a-f]{32}\Z")
#: Сведения носителя о себе. Узкий алфавит не ради красоты: строка ложится в
#: базу и в ответ администратору, где пробел сломал бы разбор полей.
INFO_RE = re.compile(r"\Aversion=([0-9A-Za-z.+~-]{1,32});machine=([0-9a-f]{8})\Z")
#: Номер команды. Регулярное выражение, а не `str.isdigit`: та пропускает
#: «²» и прочие юникодные цифры, на которых `int()` падает -- то есть 500.
COUNTER_RE = re.compile(r"\A[0-9]{1,18}\Z")
#: Unix-время в запросе администратора.
TIME_RE = re.compile(r"\A[0-9]{1,12}\Z")
#: Длина тела запроса -- только цифры, иначе `int()` на мусоре даёт 500.
LENGTH_RE = re.compile(r"\A[0-9]{1,9}\Z")
#: Состояния, которые вообще бывают. Всё прочее -- отказ, а не «ну ладно».
STATES = ("ok", "revoked")

#: Сколько помнить случайные числа носителей. Повтор перехваченного запроса в
#: этот срок получает 403; без памяти переигранный запрос отмечал бы в учёте
#: живым носитель, который давно не включался.
NONCE_TTL_SECONDS = 7 * 24 * 3600
#: Окно времени для запроса администратора. Часам его машины доверять можно, в
#: отличие от переносного носителя, поэтому хватает времени, без памяти чисел.
QUERY_WINDOW_SECONDS = 300

#: Единственный ответ на любой отказ носителю. Одинаковый намеренно: перебирающий
#: не должен узнать, на каком шаге споткнулся -- нет такого носителя, нет
#: ключа или подпись не та.
FORBIDDEN_BODY = b"forbidden\n"


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


def device_key_from_b64(text: str):
    """Открытый ключ носителя из base64 сырых 32 байт; None, если это не он."""
    try:
        raw = base64.b64decode(text, validate=True)
        if len(raw) != 32:
            return None
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError):
        return None


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


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
    """Записи о носителях и память случайных чисел их запросов."""

    #: Столбцы, появившиеся в протоколе v2. Живые серверы 1.0.0 уже держат
    #: базу без них, и терять их записи нельзя: в них владельцы и номера
    #: команд, восстановить которые нечем. Поэтому столбцы дописываются
    #: `ALTER TABLE`, а не пересозданием таблицы.
    V2_COLUMNS = (
        ("device_key", "TEXT"),   # открытый ключ носителя, base64 сырых байт
        ("last_seen", "TEXT"),    # последняя успешная связь, UTC ISO
        ("version", "TEXT"),      # версия носителя из последнего запроса
        ("machine", "TEXT"),      # отпечаток машины из последнего запроса
    )

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
            present = {row[1] for row in db.execute("PRAGMA table_info(media)")}
            for name, kind in self.V2_COLUMNS:
                if name not in present:
                    db.execute(f"ALTER TABLE media ADD COLUMN {name} {kind}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS nonces ("
                " media_id TEXT NOT NULL,"
                " nonce TEXT NOT NULL,"
                " seen_at INTEGER NOT NULL,"
                " PRIMARY KEY (media_id, nonce))"
            )

    def _connect(self) -> sqlite3.Connection:
        # Соединение на операцию, а не одно на всю жизнь службы: запросов
        # единицы в минуту, а так база остаётся согласованной даже если служба
        # умрёт посреди работы.
        return sqlite3.connect(self.path, timeout=10)

    def record_of(self, media_id: str) -> dict | None:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT owner, state, counter, device_key, last_seen, version, machine"
                " FROM media WHERE media_id = ?",
                (media_id,),
            ).fetchone()
        return dict(row) if row else None

    def put(self, media_id: str, owner: str, state: str, counter: int,
            device_key: str | None) -> None:
        """Заводит или меняет запись. `device_key=None` -- ключ остаётся прежним."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO media (media_id, owner, state, counter, updated_at, device_key)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(media_id) DO UPDATE SET"
                " state = excluded.state, counter = excluded.counter,"
                " updated_at = excluded.updated_at,"
                " device_key = COALESCE(excluded.device_key, media.device_key)",
                (media_id, owner, state, counter, utc_now_iso(), device_key),
            )

    def remember_contact(self, media_id: str, nonce: str, version: str,
                         machine: str) -> bool:
        """Запоминает число запроса и связь носителя. False -- число уже было.

        Одной транзакцией: повтор не должен ни пройти, ни сдвинуть время
        последней связи. Заодно забываются числа старше срока -- чистка при
        каждом успешном запросе дёшева и не требует отдельного таймера.
        """
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM nonces WHERE seen_at < ?", (now - NONCE_TTL_SECONDS,))
            try:
                db.execute(
                    "INSERT INTO nonces (media_id, nonce, seen_at) VALUES (?, ?, ?)",
                    (media_id, nonce, now),
                )
            except sqlite3.IntegrityError:
                return False
            db.execute(
                "UPDATE media SET last_seen = ?, version = ?, machine = ? WHERE media_id = ?",
                (utc_now_iso(), version, machine, media_id),
            )
        return True


class BadLength(Exception):
    """`Content-Length` не число или вне допустимого."""


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "poa-revocation/2"
    key = None       # ключ подписи ответов носителям
    admins: dict = {}  # допущенные администраторы по отпечатку
    admins_dir = None  # где они лежат; туда же пишется новый при знакомстве
    db = None
    #: «Прочитать код -- сверить -- сжечь» обязано быть одним шагом: сервер
    #: многопоточный, и два одновременных знакомства с одним кодом иначе
    #: прошли бы оба -- оба прочитали код раньше, чем первый его удалил.
    enroll_lock = threading.Lock()
    #: Та же гонка у команд про носитель: «прочитать запись -- сверить владельца
    #: и номер -- записать». Без блокировки два одновременных выпуска одного
    #: идентификатора разными администраторами оба видели бы «записи нет», и
    #: второй переписал бы состояние записи, владелец которой -- первый.
    media_lock = threading.Lock()

    # -- общее -------------------------------------------------------------
    def _read_body(self, limit: int = 4096) -> bytes:
        """Тело запроса. Кривой `Content-Length` -- BadLength, а не 500."""
        raw = (self.headers.get("Content-Length") or "").strip()
        if not LENGTH_RE.match(raw):
            raise BadLength
        length = int(raw)
        if length <= 0 or length > limit:
            raise BadLength
        return self.rfile.read(length)

    def _send(self, code: int, body: bytes = b"", content_type: str = "text/plain; charset=ascii",
              no_store: bool = False) -> None:
        self.send_response(code)
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _forbidden(self) -> None:
        self._send(403, FORBIDDEN_BODY, no_store=True)

    def _admin_signed(self, message: bytes) -> str | None:
        """Отпечаток администратора, если подпись `message` его. Иначе None."""
        key_id = (self.headers.get("X-POA-Key-Id") or "").strip()
        admin = self.admins.get(key_id)
        if admin is None:
            print(f"отклонено: ключ {key_id or '(нет)'} не допущен", flush=True)
            return None
        try:
            signature = base64.b64decode(self.headers.get("X-POA-Signature") or "",
                                         validate=True)
            admin.verify(signature, message)
        except Exception:
            # Одинаковый ответ на «подписи нет» и «подпись чужая»: разница
            # ничего не даёт тому, кто имеет право, и подсказывает тому, кто
            # подбирает.
            print(f"отклонено: подпись {key_id} не сходится", flush=True)
            return None
        return key_id

    # -- GET ---------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/pubkey":
            # Открытая часть ключа отзыва. Её забирает приложение при первой
            # настройке и кладёт на носители: сервер разворачивают раньше, чем
            # появляется приложение, поэтому ключ рождается здесь, а не там.
            pem = self.key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self._send(200, pem, content_type="application/x-pem-file")
            return
        if parsed.path == "/v2/state":
            self._device_asks()
            return
        if parsed.path == "/state":
            # Протокол 1.0.0: неподписанный запрос по адресу. Носителей 1.0.0 в
            # работе нет, а отвечать на него значило бы снова выдавать
            # состояние любому, кто знает идентификатор.
            self._forbidden()
            return
        prefix = "/admin/media/"
        if parsed.path.startswith(prefix):
            self._admin_asks(parsed.path[len(prefix):])
            return
        self.send_error(404, "unknown endpoint")

    def _device_asks(self) -> None:
        """Носитель спрашивает о себе. Любая неудача -- одинаковый 403."""
        media = (self.headers.get("X-POA-Media") or "").strip()
        nonce = (self.headers.get("X-POA-Nonce") or "").strip()
        info = (self.headers.get("X-POA-Info") or "").strip()
        signature_b64 = (self.headers.get("X-POA-Signature") or "").strip()
        matched = INFO_RE.match(info)
        if not MEDIA_RE.match(media) or not NONCE_RE.match(nonce) or not matched:
            self._forbidden()
            return

        record = self.db.record_of(media)
        # Неизвестный носитель -- 403, а не «ok», как было в 1.0.0: теперь
        # «ok» открывает сеанс, и выдавать его тому, кого сервер не знает,
        # нельзя. Запись, перенесённая из 1.0.0 без ключа, -- то же самое:
        # проверить подпись нечем.
        device_key = device_key_from_b64(record["device_key"] or "") if record else None
        if device_key is None:
            print(f"отклонено: носитель {media} неизвестен или без ключа", flush=True)
            self._forbidden()
            return
        try:
            signature = base64.b64decode(signature_b64, validate=True)
            device_key.verify(signature, f"poa-ask {media} {nonce} {info}".encode("ascii"))
        except Exception:
            print(f"отклонено: подпись запроса носителя {media} не сходится", flush=True)
            self._forbidden()
            return

        # Число запоминается только после проверки подписи: иначе посторонний
        # забивал бы таблицу мусором, а то и занимал бы числа настоящего носителя.
        version, machine = matched.group(1), matched.group(2)
        if not self.db.remember_contact(media, nonce, version, machine):
            print(f"отклонено: повтор числа носителя {media}", flush=True)
            self._forbidden()
            return

        state = record["state"]
        message = f"poa-state {media} {nonce} {state}"
        signature = base64.b64encode(self.key.sign(message.encode("ascii")))
        # Ответ на конкретное случайное число: кэшировать его нельзя ни на
        # секунду, иначе посредник отдаст носителю чужой ответ.
        self._send(200, message.encode("ascii") + b"\n" + signature + b"\n", no_store=True)

    def _admin_asks(self, media: str) -> None:
        """Администратор спрашивает о своём носителе: состояние и последняя связь."""
        if not MEDIA_RE.match(media):
            self.send_error(400, "bad media id")
            return
        moment = (self.headers.get("X-POA-Time") or "").strip()
        if not TIME_RE.match(moment) or abs(time.time() - int(moment)) > QUERY_WINDOW_SECONDS:
            # Окно времени вместо памяти чисел: запрос только читает, и повтор
            # в пределах пяти минут ничего не даёт, кроме того же ответа.
            print("отклонено: время запроса администратора вне окна", flush=True)
            self.send_error(403, "forbidden")
            return
        key_id = self._admin_signed(f"poa-query {media} {moment}".encode("ascii"))
        if key_id is None:
            self.send_error(403, "forbidden")
            return

        record = self.db.record_of(media)
        if record is None:
            # 404 допущенному администратору, а не 403: «не опубликован» и «не
            # ваш» -- разные поводы действовать. Существование чужого
            # идентификатора узнаёт только тот, кого владелец сервера допустил.
            self.send_error(404, "unknown media")
            return
        if record["owner"] != key_id:
            print(f"отклонено: {key_id} не владелец носителя {media}", flush=True)
            self.send_error(403, "forbidden")
            return

        fields = [record["last_seen"], record["version"], record["machine"]]
        tail = " ".join(value if value else "-" for value in fields)
        body = f"poa-record {media} {record['state']} {tail}\n".encode("ascii")
        self._send(200, body, no_store=True)

    # -- администратор распоряжается ----------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - имя задано базовым классом
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/admin/enroll":
            self._enroll()
            return
        prefix = "/admin/media/"
        if not parsed.path.startswith(prefix):
            self.send_error(404, "unknown endpoint")
            return
        media = parsed.path[len(prefix):]
        if not MEDIA_RE.match(media):
            self.send_error(400, "bad media id")
            return

        try:
            body = self._read_body()
        except BadLength:
            self.send_error(400, "bad body size")
            return

        key_id = self._admin_signed(body)
        if key_id is None:
            self.send_error(403, "forbidden")
            return

        parts = body.decode("utf-8", "replace").strip().split()
        if len(parts) not in (4, 5) or parts[0] != "poa-media" or parts[1] != media:
            self.send_error(400, "bad command")
            return
        if not COUNTER_RE.match(parts[2]) or parts[3] not in STATES:
            self.send_error(400, "bad counter or state")
            return
        counter, state = int(parts[2]), parts[3]
        device_key = None
        if len(parts) == 5:
            if device_key_from_b64(parts[4]) is None:
                self.send_error(400, "bad device key")
                return
            device_key = parts[4]

        with self.media_lock:
            record = self.db.record_of(media)
            if record is None:
                # Неизвестный носитель -- это его выпуск. Владельцем становится
                # тот, кто его создал, и дальше распоряжаться им может только он.
                # Без ключа носителя запись не заводится: такой носитель не
                # сможет подписать запрос и всё равно получал бы только 403.
                if device_key is None:
                    self.send_error(400, "device key required")
                    return
                self.db.put(media, key_id, state, counter, device_key)
                print(f"заведён носитель {media}: владелец {key_id}, состояние {state}",
                      flush=True)
                self._send(201)
                return

            owner, previous = record["owner"], record["counter"]
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

            # Пятое поле у существующей записи -- перевыпуск носителя с тем же
            # идентификатором: новый ключ, прежний идентификатор и владелец.
            self.db.put(media, owner, state, counter, device_key)
        replaced = ", ключ заменён" if device_key and device_key != record["device_key"] else ""
        print(f"носитель {media}: состояние {state} (номер {counter}){replaced}", flush=True)
        self._send(204)

    def _enroll(self) -> None:
        """Первое знакомство с администратором: он присылает свой открытый ключ.

        По одноразовому коду, который печатает сценарий установки. Иначе
        пришлось бы либо доверять любому, кто постучится первым, -- а сервер
        общедоступен, -- либо лезть на машину руками, чего мы и избегаем.

        Код сгорает после первого успешного знакомства. Второй администратор
        заводится новым кодом: его создаёт владелец сервера, у него для этого
        есть машина.
        """
        # Тело разбирается до кода: иначе кривое тело сжигало бы код впустую,
        # и администратору пришлось бы просить новый из-за опечатки.
        try:
            body = self._read_body()
        except BadLength:
            self.send_error(400, "bad body size")
            return
        try:
            key = serialization.load_pem_public_key(body)
            name = fingerprint(key)
        except Exception:
            self.send_error(400, "bad public key")
            return

        code_path = self.admins_dir.parent / "enrollment-code"
        given = (self.headers.get("X-POA-Enrollment") or "").strip()
        with self.enroll_lock:
            try:
                expected = code_path.read_text(encoding="ascii").strip()
            except OSError:
                expected = ""
            if not expected or not given or not secrets.compare_digest(given, expected):
                print("отклонено знакомство: код не сходится", flush=True)
                self.send_error(403, "bad enrollment code")
                return
            # Код сжигается до записи ключа. В обратном порядке сбой между
            # шагами оставил бы допущенного администратора и живой код --
            # второе знакомство по тому же коду.
            try:
                code_path.unlink()
            except OSError:
                print("знакомство отклонено: код не удаётся сжечь", flush=True)
                self.send_error(500, "cannot burn enrollment code")
                return

        self.admins_dir.mkdir(parents=True, exist_ok=True)
        (self.admins_dir / f"{name}.pem").write_bytes(body)
        self.admins[name] = key
        print(f"заведён администратор {name}", flush=True)
        self._send(201, f"{name}\n".encode("ascii"))

    def log_message(self, fmt: str, *args) -> None:
        # В journald, одной строкой на запрос. Учёт парка теперь в базе
        # (последняя связь каждого носителя), журнал -- для разбора отказов.
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
    Handler.admins_dir = args.admins

    if args.admins is not None and not Handler.admins:
        # Никого ещё нет -- значит сервер только что развернули, приложения не
        # существует и положить в него нечего. Заводим одноразовый код: по нему
        # первый администратор пришлёт свой открытый ключ сам.
        code_path = args.admins.parent / "enrollment-code"
        if not code_path.exists():
            code_path.parent.mkdir(parents=True, exist_ok=True)
            code_path.write_text(secrets.token_hex(16) + "\n", encoding="ascii")
            code_path.chmod(0o600)
        print(f"код знакомства: {code_path.read_text(encoding='ascii').strip()}", flush=True)

    server = http.server.ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"ресурс отзыва слушает {args.host}:{args.port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
