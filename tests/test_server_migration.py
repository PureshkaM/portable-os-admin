"""Э17: перенос базы сервера доступа из sqlite в PostgreSQL.

Нужна PostgreSQL: `POA_TEST_PG=postgresql://роль:пароль@хост/postgres`
(роль с CREATEDB). Без неё тесты пропускаются -- на маке её нет.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import secrets

import pytest

_spec = importlib.util.spec_from_file_location(
    "access_server_harness_m", pathlib.Path(__file__).with_name("test_access_server.py"))
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

from poa_access import migrate  # noqa: E402  (путь добавил сервер)
from poa_access.store import Store  # noqa: E402

PG = os.environ.get("POA_TEST_PG")
pytestmark = pytest.mark.skipif(not PG, reason="нужна PostgreSQL (POA_TEST_PG)")


@pytest.fixture
def target():
    import psycopg2

    name = f"poa_m_{secrets.token_hex(6)}"
    conn = psycopg2.connect(PG)
    conn.autocommit = True
    conn.cursor().execute(f"CREATE DATABASE {name}")
    conn.close()
    yield PG.rsplit("/", 1)[0] + f"/{name}"
    conn = psycopg2.connect(PG)
    conn.autocommit = True
    conn.cursor().execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    conn.close()


def test_a_working_sqlite_base_moves_to_postgresql_and_the_server_goes_on(tmp_path, monkeypatch, target):
    monkeypatch.delenv("POA_TEST_PG")          # источник -- sqlite, как у живого сервера
    clock = harness.Clock()
    monkeypatch.setattr(harness.totp, "time", clock)
    old = harness._Server(tmp_path, clock)
    device = harness.Device(old)
    old.register(device.media, device.key)
    assert device.activate(old.issue_code(device.media))[0] == 200
    old.admin_cmd("group", id="dept", parent="root")
    old.admin_cmd("media_group", media=device.media, group="dept")
    status, body = device.post("/v3/events", {"catalog": 1, "events": [
        {"seq": 1, "code": "E01", "t": 1, "p": {}}, {"seq": 2, "code": "D01", "t": 2}]})
    assert (status, body) == (200, {"acked": 2})
    before = old.admin_cmd("log", since=0)[1]
    old.close()

    counts = migrate.migrate(str(old.db_path), target)
    assert counts["media"] == 1 and counts["device_events"] == 2 and counts["groups"] == 2
    # Повторный перенос ничего не удваивает.
    migrate.migrate(str(old.db_path), target)

    store = Store(target)
    with store.tx() as db:
        assert Store.media(db, device.media)["group_id"] == "dept"
        events = db.execute("SELECT id, seq FROM device_events ORDER BY id").fetchall()
        assert [r["seq"] for r in events] == [1, 2]
        last = db.execute("SELECT MAX(id) FROM server_log").fetchone()[0]
        Store.log(db, "after_migration")
        assert db.execute("SELECT MAX(id) FROM server_log").fetchone()[0] == last + 1
    assert [e["code"] for e in before["device"]] == ["E01", "D01"]
