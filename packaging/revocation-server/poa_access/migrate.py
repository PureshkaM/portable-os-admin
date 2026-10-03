"""Перенос базы сервера из sqlite в PostgreSQL (Э17).

    python3 -m poa_access.migrate media.sqlite postgresql:///poa_access?host=/run/postgresql \\
        --role poa-revocation

Схема создаётся тем же `Store`, что и у работающей службы; строки
переносятся по общим столбцам, повторный запуск ничего не дублирует
(`ON CONFLICT DO NOTHING`). Порядок журнала и событий сохраняется: у sqlite
это rowid, у PostgreSQL -- столбец id, и счётчики после переноса
продолжаются с последнего номера. В конце -- сверка числа строк.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

from .store import Store

#: Порядок не важен для ключей (внешних ключей нет), важен для читаемости.
TABLES = ("media", "nonces", "access", "link_history", "activations", "nonces3", "certs",
          "targets", "groups", "distributions", "releases", "device_events", "server_log",
          "admin_counters")
#: Таблицы, чей порядок -- номер строки (sqlite rowid → PostgreSQL id).
ORDERED = {"device_events": "rowid", "server_log": "id"}


class MigrationError(Exception):
    pass


def migrate(sqlite_path: str, target: str, role: str | None = None) -> dict[str, int]:
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    present = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    store = Store(target, role)
    if not store.database.postgres:
        raise MigrationError("перенос -- только в PostgreSQL (postgresql://…)")
    counts: dict[str, int] = {}
    with store.tx() as db:
        for table in TABLES:
            if table not in present:
                continue
            wanted = db.columns(table)
            columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})") if r[1] in wanted]
            select = ", ".join(columns)
            if table in ORDERED:
                select = f"{ORDERED[table]} AS id, " + ", ".join(c for c in columns if c != "id")
                columns = ["id"] + [c for c in columns if c != "id"]
            rows = source.execute(f"SELECT {select} FROM {table}").fetchall()
            marks = ", ".join("?" for _ in columns)
            for row in rows:
                db.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"
                           " ON CONFLICT DO NOTHING", tuple(row))
            if table in ORDERED and db.postgres:
                db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                           f" GREATEST((SELECT MAX(id) FROM {table}), 1))")
            counts[table] = len(rows)
        for table, expected in counts.items():
            got = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if got < expected:
                raise MigrationError(f"{table}: перенесено {got} строк из {expected}")
    source.close()
    return counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Перенос базы сервера доступа в PostgreSQL")
    parser.add_argument("sqlite")
    parser.add_argument("target")
    parser.add_argument("--role", default=None, help="владелец таблиц (SET ROLE)")
    args = parser.parse_args(argv)
    counts = migrate(args.sqlite, args.target, args.role)
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
