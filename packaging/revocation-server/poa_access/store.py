"""Хранилище сервера доступа поверх той же базы, что и v2: sqlite или
PostgreSQL (`poa_access.db`, этап Э17).

Главное свойство -- **один шаг входа = одна транзакция с блокировкой**: nonce,
звено и `last_step` TOTP меняются вместе. Иначе два параллельных запроса с
одним звеном прошли бы оба, и цепочка раздвоилась бы -- ровно то, что звено
должно ловить. Как это даёт каждая из баз -- в `poa_access.db`.

Звенья хранятся только хешами: копия базы не даёт предъявить звено.
"""
from __future__ import annotations

import json
import time

from .db import Database

SCHEMA = (
    # Столбцы v3 у записи носителя v2.
    "ALTER TABLE media ADD COLUMN alg TEXT",
    "ALTER TABLE media ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE media ADD COLUMN group_id TEXT",
)
TABLES = (
    "CREATE TABLE IF NOT EXISTS access ("
    " media_id TEXT PRIMARY KEY, totp BLOB, last_step INTEGER NOT NULL DEFAULT 0,"
    " link_c TEXT, link_p TEXT, activated INTEGER NOT NULL DEFAULT 0,"
    " fails INTEGER NOT NULL DEFAULT 0, dst BLOB, events_acked INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS link_history ("
    " media_id TEXT NOT NULL, h TEXT NOT NULL, PRIMARY KEY (media_id, h))",
    "CREATE TABLE IF NOT EXISTS activations ("
    " code_hash TEXT PRIMARY KEY, media_id TEXT NOT NULL, expires INTEGER NOT NULL,"
    " used_by TEXT, issued_at INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS nonces3 ("
    " nonce TEXT PRIMARY KEY, media_id TEXT NOT NULL, expires INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS certs ("
    " serial TEXT PRIMARY KEY, media_id TEXT NOT NULL, not_after INTEGER NOT NULL,"
    " revoked_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS targets ("
    " media_id TEXT NOT NULL, target_id TEXT NOT NULL, spec TEXT NOT NULL,"
    " PRIMARY KEY (media_id, target_id))",
    "CREATE TABLE IF NOT EXISTS target_versions ("
    " media_id TEXT NOT NULL, target_id TEXT NOT NULL, version INTEGER NOT NULL,"
    " PRIMARY KEY (media_id, target_id))",
    "CREATE TABLE IF NOT EXISTS employee_sessions ("
    " media_id TEXT PRIMARY KEY, session_id TEXT, mfa_until INTEGER NOT NULL,"
    " media_counter INTEGER NOT NULL, status TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,"
    " target_id TEXT, revision TEXT, lease_until INTEGER)",
    "CREATE TABLE IF NOT EXISTS groups ("
    " id TEXT PRIMARY KEY, parent TEXT, distribution TEXT, allow_vm INTEGER)",
    "CREATE TABLE IF NOT EXISTS distributions ("
    " id TEXT PRIMARY KEY, family TEXT NOT NULL, arch TEXT NOT NULL,"
    " layout INTEGER NOT NULL, min_serial INTEGER NOT NULL DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS releases ("
    " distribution TEXT NOT NULL, serial INTEGER NOT NULL, files TEXT NOT NULL,"
    " published_at INTEGER NOT NULL, PRIMARY KEY (distribution, serial))",
    "CREATE TABLE IF NOT EXISTS device_events ("
    " media_id TEXT NOT NULL, seq INTEGER NOT NULL, code TEXT NOT NULL, t INTEGER,"
    " boot TEXT, params TEXT, received INTEGER NOT NULL, PRIMARY KEY (media_id, seq))",
    "CREATE TABLE IF NOT EXISTS server_log ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, t INTEGER NOT NULL, kind TEXT NOT NULL,"
    " media_id TEXT, detail TEXT)",
    "CREATE TABLE IF NOT EXISTS admin_counters ("
    " key_id TEXT PRIMARY KEY, counter INTEGER NOT NULL)",
    # Таблица v2 (числа запросов /v2/state): её заводит и служба v2, а здесь --
    # чтобы схему целиком создавал один класс (перенос в PostgreSQL, Э17).
    "CREATE TABLE IF NOT EXISTS nonces ("
    " media_id TEXT NOT NULL, nonce TEXT NOT NULL, seen_at INTEGER NOT NULL,"
    " PRIMARY KEY (media_id, nonce))",
)
#: Корень дерева групп. Его назначение -- дистрибутив по умолчанию.
ROOT_GROUP = "root"


class Store:
    def __init__(self, path, role: str | None = None) -> None:
        self.path = path
        self.database = Database(path, role)
        with self.tx() as db:
            present = db.columns("media")
            if not present:
                # База без v2 (тесты v3 поднимают сервер с нуля, новая база
                # PostgreSQL): таблица v2 заводится здесь в той же форме, что у v2.
                db.ddl(
                    "CREATE TABLE media (media_id TEXT PRIMARY KEY, owner TEXT NOT NULL,"
                    " state TEXT NOT NULL, counter INTEGER NOT NULL, updated_at TEXT NOT NULL,"
                    " device_key TEXT, last_seen TEXT, version TEXT, machine TEXT)"
                )
                present = db.columns("media")
            for statement in SCHEMA:
                column = statement.split("ADD COLUMN ")[1].split()[0]
                if column not in present:
                    db.ddl(statement)
            for statement in TABLES:
                db.ddl(statement)
            if db.postgres:
                # У sqlite порядок событий -- rowid; у PostgreSQL его нет.
                db.execute("ALTER TABLE device_events ADD COLUMN IF NOT EXISTS id BIGSERIAL")
            db.execute("INSERT INTO groups (id, parent) VALUES (?, NULL) ON CONFLICT DO NOTHING",
                       (ROOT_GROUP,))

    def tx(self):
        """Транзакция с блокировкой записи с первой же команды."""
        return self.database.tx()

    # -- журнал сервера ------------------------------------------------------
    @staticmethod
    def log(db, kind: str, media: str | None = None, **detail) -> None:
        db.execute("INSERT INTO server_log (t, kind, media_id, detail) VALUES (?, ?, ?, ?)",
                   (int(time.time()), kind, media,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None))

    def server_log(self, since: int = 0, limit: int = 500) -> list[dict]:
        with self.tx() as db:
            rows = db.execute("SELECT * FROM server_log WHERE id > ? ORDER BY id LIMIT ?",
                              (since, limit)).fetchall()
        return [dict(r) for r in rows]

    # -- носитель -------------------------------------------------------------
    @staticmethod
    def media(db, media: str):
        return db.execute("SELECT * FROM media WHERE media_id = ?", (media,)).fetchone()

    @staticmethod
    def access(db, media: str):
        row = db.execute("SELECT * FROM access WHERE media_id = ?", (media,)).fetchone()
        if row is None:
            db.execute("INSERT INTO access (media_id) VALUES (?)", (media,))
            row = db.execute("SELECT * FROM access WHERE media_id = ?", (media,)).fetchone()
        return row

    # -- nonce ----------------------------------------------------------------
    def issue_nonce(self, media: str, nonce: str, ttl: int) -> None:
        now = int(time.time())
        with self.tx() as db:
            db.execute("DELETE FROM nonces3 WHERE expires < ?", (now,))
            db.execute("INSERT INTO nonces3 (nonce, media_id, expires) VALUES (?, ?, ?)",
                       (nonce, media, now + ttl))

    @staticmethod
    def consume_nonce(db, media: str, nonce: str) -> bool:
        """Гасит nonce внутри транзакции шага. False -- нет такого или чужой/истёк."""
        cur = db.execute("DELETE FROM nonces3 WHERE nonce = ? AND media_id = ? AND expires >= ?",
                         (nonce, media, int(time.time())))
        return cur.rowcount == 1

    # -- группы и выпуски ------------------------------------------------------
    @staticmethod
    def effective(db, group_id: str | None, column: str):
        """Ближайшее непустое значение `column` вверх по дереву групп."""
        seen = set()
        current = group_id or ROOT_GROUP
        while current and current not in seen:
            seen.add(current)
            row = db.execute(f"SELECT parent, {column} FROM groups WHERE id = ?",
                             (current,)).fetchone()
            if row is None:
                current = ROOT_GROUP if current != ROOT_GROUP else None
                continue
            if row[column] is not None:
                return row[column]
            current = row["parent"] or (ROOT_GROUP if current != ROOT_GROUP else None)
        return None

    @staticmethod
    def latest_release(db, distribution: str):
        return db.execute("SELECT * FROM releases WHERE distribution = ? ORDER BY serial DESC"
                          " LIMIT 1", (distribution,)).fetchone()

    # -- сертификаты ------------------------------------------------------------
    @staticmethod
    def revoke_certs_of(db, media: str) -> int:
        now = int(time.time())
        cur = db.execute("UPDATE certs SET revoked_at = ? WHERE media_id = ? AND revoked_at IS NULL"
                         " AND not_after > ?", (now, media, now))
        return cur.rowcount

    def revoked_certs(self) -> list[tuple[int, int]]:
        now = int(time.time())
        with self.tx() as db:
            rows = db.execute("SELECT serial, revoked_at FROM certs WHERE revoked_at IS NOT NULL"
                              " AND not_after > ?", (now,)).fetchall()
        return [(int(r["serial"]), int(r["revoked_at"])) for r in rows]

    # -- счётчики команд администратора -----------------------------------------
    @staticmethod
    def advance_counter(db, key_id: str, counter: int) -> bool:
        row = db.execute("SELECT counter FROM admin_counters WHERE key_id = ?", (key_id,)).fetchone()
        if row is not None and counter <= row["counter"]:
            return False
        db.execute("INSERT INTO admin_counters (key_id, counter) VALUES (?, ?)"
                   " ON CONFLICT(key_id) DO UPDATE SET counter = excluded.counter",
                   (key_id, counter))
        return True
