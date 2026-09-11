"""Журнал работы самого приложения: кто и когда открывал контейнер.

Зачем он отдельно от журнала внутри контейнера. Контейнер отвечает на вопрос
«что сделали с этим носителем» -- отозвали, стёрли, вернули в строй, -- и такие
записи обязаны ехать вместе с носителями, поэтому лежат внутри. Этот журнал
отвечает на другой вопрос: «кто и когда работал с самим контейнером». Класть
такие записи внутрь значило бы растить контейнер тем, что к носителям
отношения не имеет, -- а он копируется, передаётся и открывается целиком.

Что пишется. Ровно четыре вещи: время, что произошло, к какому файлу
контейнера и под какой учётной записью. Ни паролей, ни ключей, ни содержимого
-- журнал лежит рядом с настройками интерфейса открытым текстом, и всё, что в
него попадает, надо считать доступным тому, кто добрался до машины
администратора. Минимум -- это и было требование.

Чем это **не** является. Неотказуемостью: файл лежит на машине администратора,
и тот, у кого есть root, его правит. Здесь учёт для самого администратора и
разбора «когда это началось», а не доказательство для третьей стороны. Такое
доказательство потребовало бы подписи и внешнего хранилища, то есть отдельной
работы, которой пока нет.

Отказ записи проглатывается молча -- по той же причине, что и в
`preferences.save`: работа администратора не должна вставать из-за строки в
журнале. Обратная сторона честно названа выше: журнал, который можно потерять,
не является доказательством.
"""
from __future__ import annotations

import getpass
import os
from datetime import datetime, timezone
from pathlib import Path

#: Записи, которые этот журнал знает. Ключом, а не готовой фразой: формулировку
#: потом правят, а записанное остаётся.
CONTAINER_OPENED = "container_opened"
CONTAINER_CREATED = "container_created"
CONTAINER_PASSWORD_CHANGED = "container_password_changed"
RECOVERY_CODE_ISSUED = "recovery_code_issued"

#: Потолок размера. Журнал живёт годами и никем не чистится, а строка в нём
#: короткая: полмегабайта -- это тысячи заходов. При переполнении отбрасывается
#: старая половина, а не файл целиком: терять весь журнал из-за того, что он
#: вырос, -- худший из способов им управлять.
MAX_BYTES = 512 * 1024


def path() -> Path:
    """Рядом с настройками интерфейса, а не в контейнере и не на носителе."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "portable-os-admin" / "journal.log"


def record(action: str, container_path: "str | Path" = "", details: str = "") -> None:
    """Одна строка в журнал. Молча смиряется с невозможностью записать."""
    try:
        who = getpass.getuser()
    except Exception:  # noqa: BLE001 - без учётной записи запись всё равно полезна
        who = "?"
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Табуляцией, потому что это машиночитаемо и одновременно читается глазами;
    # переводы строк в полях заменяются, чтобы одна запись оставалась одной
    # строкой -- иначе журнал нельзя ни просмотреть, ни разобрать.
    fields = [at, who, action, str(container_path), details]
    line = "\t".join(field.replace("\t", " ").replace("\n", " ") for field in fields)

    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        _trim(target)
    except OSError:
        return


def _trim(target: Path) -> None:
    try:
        if target.stat().st_size <= MAX_BYTES:
            return
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
        keep = lines[len(lines) // 2 :]
        with open(target, "w", encoding="utf-8") as handle:
            handle.writelines(keep)
    except OSError:
        return


def read(limit: int = 200) -> "list[str]":
    """Последние записи, новые снизу. Пустой список, если журнала ещё нет."""
    try:
        with open(path(), encoding="utf-8", errors="replace") as handle:
            lines = [line.rstrip("\n") for line in handle if line.strip()]
    except OSError:
        return []
    return lines[-limit:]


__all__ = [
    "CONTAINER_CREATED",
    "CONTAINER_OPENED",
    "CONTAINER_PASSWORD_CHANGED",
    "RECOVERY_CODE_ISSUED",
    "MAX_BYTES",
    "path",
    "read",
    "record",
]
