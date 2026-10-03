"""База сервера доступа: sqlite (стенд, переход) или PostgreSQL (Э17).

Запросы пишутся один раз, в общем диалекте:

* параметры -- `?` (для PostgreSQL переводятся в `%s`);
* вставка без перезаписи -- `INSERT ... ON CONFLICT DO NOTHING`, с заменой --
  `ON CONFLICT(ключ) DO UPDATE SET ...`: это понимают оба (sqlite с 3.24);
* в DDL -- `INTEGER`, `BLOB`, `INTEGER PRIMARY KEY AUTOINCREMENT`; для
  PostgreSQL они становятся `BIGINT`, `BYTEA`, `BIGSERIAL PRIMARY KEY`.
  `BIGINT`, а не `INTEGER`: номера команд администратора -- миллисекунды
  (1,8·10¹²), в int4 они не помещаются.

Главное свойство переносится как есть: **одна транзакция -- одна блокировка
записи на всю базу**. В sqlite это `BEGIN IMMEDIATE`, в PostgreSQL --
`pg_advisory_xact_lock` первой же командой транзакции. Шаг входа (nonce,
звено, `last_step` TOTP) меняется целиком или никак, и два параллельных
запроса с одним звеном не проходят оба. Глобальная сериализация, а не
блокировка строки: запросов единицы в секунду, а ошибиться с набором
блокируемых строк дороже, чем потерять параллельность.
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3

#: Ключ advisory-блокировки сервера доступа (произвольное, но постоянное число).
LOCK_KEY = 0x504F41  # «POA»

_DDL = (
    (re.compile(r"\bINTEGER PRIMARY KEY AUTOINCREMENT\b"), "BIGSERIAL PRIMARY KEY"),
    (re.compile(r"\bINTEGER\b"), "BIGINT"),
    (re.compile(r"\bBLOB\b"), "BYTEA"),
)


class IntegrityError(Exception):
    """Нарушение ключа -- одинаково для обеих баз."""


class Row(dict):
    """Строка, доступная и по имени, и по номеру (как sqlite3.Row)."""

    def __init__(self, names, values) -> None:
        super().__init__(zip(names, values))
        self._values = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def keys(self):  # noqa: D401 - как у sqlite3.Row
        return list(super().keys())


class Cursor:
    def __init__(self, cursor, postgres: bool) -> None:
        self._cursor = cursor
        self._postgres = postgres

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def _row(self, raw):
        if raw is None:
            return None
        names = [d[0] for d in self._cursor.description]
        values = [bytes(v) if isinstance(v, memoryview) else v for v in raw]
        return Row(names, values)

    def fetchone(self):
        if self._cursor.description is None:
            return None
        return self._row(self._cursor.fetchone())

    def fetchall(self):
        if self._cursor.description is None:
            return []
        return [self._row(r) for r in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """Соединение внутри транзакции: `execute` в общем диалекте."""

    def __init__(self, raw, postgres: bool) -> None:
        self.raw = raw
        self.postgres = postgres

    #: Выражение «порядковый номер строки» для таблиц без своего id.
    @property
    def rowid(self) -> str:
        return "id" if self.postgres else "rowid"

    def execute(self, sql: str, params=()) -> Cursor:
        if self.postgres:
            import psycopg2

            sql = sql.replace("%", "%%").replace("?", "%s")
            cursor = self.raw.cursor()
            try:
                cursor.execute(sql, tuple(params))
            except psycopg2.IntegrityError as exc:
                raise IntegrityError(str(exc)) from exc
            return Cursor(cursor, True)
        try:
            return Cursor(self.raw.execute(sql, tuple(params)), False)
        except sqlite3.IntegrityError as exc:
            raise IntegrityError(str(exc)) from exc

    def columns(self, table: str) -> set[str]:
        if self.postgres:
            rows = self.execute("SELECT column_name FROM information_schema.columns"
                                " WHERE table_name = ?", (table,)).fetchall()
            return {r[0] for r in rows}
        return {r[1] for r in self.raw.execute(f"PRAGMA table_info({table})")}

    def ddl(self, statement: str) -> None:
        """CREATE/ALTER в диалекте sqlite -- переводится для PostgreSQL."""
        if self.postgres:
            for pattern, replacement in _DDL:
                statement = pattern.sub(replacement, statement)
        self.execute(statement)


class Database:
    """`postgresql://…` -- PostgreSQL; иначе -- путь к файлу sqlite."""

    def __init__(self, target, role: str | None = None) -> None:
        self.target = str(target)
        self.postgres = self.target.startswith(("postgresql://", "postgres://"))
        #: Роль, от имени которой работать после входа (`SET ROLE`): перенос
        #: данных входит как postgres, а таблицы должны принадлежать службе.
        self.role = role

    @staticmethod
    def _password() -> dict:
        """Пароль кластера -- из файла учётных данных systemd
        (`POA_DB_PASSWORD_FILE`), а не через PGPASSFILE: systemd выкладывает
        учётные данные с правами 0440 и ACL, а libpq молча игнорирует файл
        паролей с правом группы («no password supplied», стенд 03.10.2026).
        Один узел обходится без пароля (peer)."""
        path = os.environ.get("POA_DB_PASSWORD_FILE")
        if not path:
            return {}
        with open(path, encoding="utf-8") as handle:
            return {"password": handle.read().strip()}

    @contextlib.contextmanager
    def tx(self):
        """Транзакция с блокировкой записи с первой же команды."""
        if self.postgres:
            import psycopg2

            raw = psycopg2.connect(self.target, **self._password())
            try:
                conn = Connection(raw, True)
                if self.role:
                    from psycopg2 import sql

                    raw.cursor().execute(sql.SQL("SET ROLE {}").format(sql.Identifier(self.role)))
                conn.execute("SELECT pg_advisory_xact_lock(?)", (LOCK_KEY,))
                yield conn
                raw.commit()
            except BaseException:
                raw.rollback()
                raise
            finally:
                raw.close()
            return
        raw = sqlite3.connect(self.target, timeout=30, isolation_level=None)
        try:
            raw.execute("BEGIN IMMEDIATE")
            yield Connection(raw, False)
            raw.execute("COMMIT")
        except BaseException:
            raw.execute("ROLLBACK")
            raise
        finally:
            raw.close()
