#!/usr/bin/env python3
"""Очередь событий ИБ на носителе (LIVE-2.0, этап Э20).

Правила (`docs/THREAT-MODEL.md`, приложение В):

* очередь лежит в разделе журналов (`/var/log/poa-events`, только root): до
  разблокировки LUKS ничего не пишется;
* событие -- код каталога, номер `seq`, время, `boot_id`, короткие параметры;
* очередь **не перезаписывается**, пока сервер не подтвердил приём: удаляется
  только подтверждённое (`ack`);
* при заполнении информационные события («И») перестают записываться, важные
  пишутся в резерв (`RESERVE`).

Командная строка для служб и скриптов носителя::

    poa-event E01 slot=A fails=0
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time

QUEUE_DIR = os.environ.get("POA_EVENTS_DIR", "/var/log/poa-events")
QUEUE = "queue.jsonl"
SEQ = "seq"
#: Предел очереди. Выгрузка идёт при каждом входе, так что очередь копится
#: только между сеансами -- предел нужен против лавины (сценарий F3).
LIMIT_BYTES = 4 << 20
#: Резерв сверх предела для важных событий.
RESERVE = 1 << 20
#: Информационные коды -- первыми перестают писаться при заполнении.
INFO_CODES = {"E01", "E02", "E05", "B01", "L02", "N01", "N02", "N03", "N06", "N09",
              "C02", "C06", "V01", "V02", "T01", "T05", "T06", "T10", "D04", "U01",
              "U02", "U03", "U07"}


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""


class Queue:
    def __init__(self, directory: str = QUEUE_DIR) -> None:
        self.directory = directory
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, QUEUE)
        self.lock_path = os.path.join(directory, ".lock")

    def _locked(self):
        handle = open(self.lock_path, "a")
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    def _next_seq(self) -> int:
        path = os.path.join(self.directory, SEQ)
        try:
            value = int(open(path, encoding="ascii").read().strip() or 0) + 1
        except (OSError, ValueError):
            value = 1
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="ascii") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return value

    def append(self, code: str, params: dict | None = None) -> int | None:
        """Номер записанного события или None, если очередь полна для этого уровня."""
        with self._locked():
            size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            limit = LIMIT_BYTES if code in INFO_CODES else LIMIT_BYTES + RESERVE
            if size >= limit:
                self._note_overflow(size)
                return None
            seq = self._next_seq()
            record = {"seq": seq, "code": code, "t": int(time.time()), "boot": _boot_id(),
                      "p": params or {}}
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return seq

    def _note_overflow(self, size: int) -> None:
        marker = os.path.join(self.directory, "overflow")
        with open(marker, "a", encoding="ascii") as handle:
            handle.write(f"{int(time.time())} {size}\n")

    def pending(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def ack(self, acked: int) -> None:
        """Удаляет подтверждённое сервером (seq <= acked). Остальное не трогается."""
        with self._locked():
            keep = [e for e in self.pending() if e["seq"] > acked]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                for event in keep:
                    handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)


def main(argv: list[str]) -> int:
    if not argv or len(argv[0]) != 3:
        print("использование: poa-event КОД [ключ=значение ...]", file=sys.stderr)
        return 2
    params = dict(arg.split("=", 1) for arg in argv[1:] if "=" in arg)
    Queue().append(argv[0], params)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
