"""Общий initramfs выпуска и «initrd носителя» (LIVE-2.0, этап Э2).

Initramfs собирается один раз -- при сборке дистрибутива -- и одинаков у всех
носителей: иначе образ нельзя было бы заменить целиком (A/B, Э3b/Э4), не
пересобирая initramfs на каждой флешке. То, что своё у носителя, едет вторым
архивом, `poa-medium.img`: загрузчик передаёт ядру оба, ядро распаковывает их
по очереди в один корень (тот же механизм, которым грузят микрокод).

Скрипты initramfs поэтому не знают о носителе ничего: имя тома, UUID LUKS и
ID носителя они берут из `/poa/env`, который приходит во втором архиве.
Решение и его причины -- `docs/LIVE-2.0.md`, раздел «Э2».
"""
from __future__ import annotations

from dataclasses import dataclass

#: То же, что `backends.constants.RECOVERY_INITRAMFS_DIR` (сверяется тестом).
#: Не импортом: бэкенды сами берут отсюда SOURCE_ENV, и импорт по кругу
#: оставил бы один из модулей недостроенным.
RECOVERY_INITRAMFS_DIR = "/poa"

#: Имя второго архива на разделе загрузки, рядом с ядром.
ARCHIVE_NAME = "poa-medium.img"
ENV_PATH = f"{RECOVERY_INITRAMFS_DIR}/env"

#: Строка, с которой начинается каждый загрузочный скрипт, знающий о носителе.
#: `set -a` -- чтобы значения дошли и до дочерних скриптов (регистрация,
#: модуль восстановления запускаются отдельными процессами).
SOURCE_ENV = f"[ -r {ENV_PATH} ] && {{ set -a; . {ENV_PATH}; set +a; }}\n"


@dataclass(frozen=True)
class GenericLayout:
    """Раскладка «любого носителя» для сборки общего initramfs.

    Поля -- не значения, а ссылки на переменные из `/poa/env`: генераторы
    загрузочных скриптов подставляют их в текст как раньше подставляли
    настоящие имена, и скрипт узнаёт своё значение уже при загрузке.
    Годится только для текста загрузочных скриптов: файлы корня (fstab,
    crypttab, отзыв) по-прежнему пишутся при записи с настоящей раскладкой.
    """

    mapper_name: str = "${POA_MAPPER}"
    luks_uuid: str = "${POA_LUKS_UUID}"
    media_id: str = "${POA_MEDIA_ID}"
    generic: bool = True


GENERIC = GenericLayout()


def is_generic(layout) -> bool:
    return bool(getattr(layout, "generic", False))


def _newc_entry(name: str, mode: int, data: bytes, ino: int) -> bytes:
    header = (
        "070701"
        + "".join(f"{v:08X}" for v in (
            ino, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(name) + 1, 0))
    ).encode("ascii")
    out = header + name.encode("utf-8") + b"\0"
    out += b"\0" * (-len(out) % 4)
    out += data
    out += b"\0" * (-len(out) % 4)
    return out


def newc_archive(files: dict[str, tuple[bytes, int]]) -> bytes:
    """cpio newc без сжатия: каталоги, затем файлы, затем TRAILER.

    Свой писатель, а не `cpio`: формат -- полстраницы, а внешняя утилита
    была бы ещё одной зависимостью машины администратора. Владелец -- root,
    время -- ноль: архив одинаков при одинаковом содержимом.
    """
    dirs: set[str] = set()
    for path in files:
        parts = path.strip("/").split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    out = b""
    ino = 1
    for d in sorted(dirs):
        out += _newc_entry(d, 0o040755, b"", ino)
        ino += 1
    for path in sorted(files):
        data, mode = files[path]
        out += _newc_entry(path.strip("/"), 0o100000 | mode, data, ino)
        ino += 1
    out += _newc_entry("TRAILER!!!", 0, b"", 0)
    return out + b"\0" * (-len(out) % 512)


def medium_archive(env_text: str, recovery_salt: bytes, requests: str,
                   crypttab: tuple[str, str] | None = None) -> bytes:
    """Второй initrd носителя. `crypttab` -- (путь в initramfs, текст), если
    семейство открывает корень по таблице внутри initramfs (Debian)."""
    files = {
        ENV_PATH: (env_text.encode("utf-8"), 0o400),
        f"{RECOVERY_INITRAMFS_DIR}/recovery_salt.bin": (recovery_salt, 0o400),
        f"{RECOVERY_INITRAMFS_DIR}/requests": (requests.encode("utf-8"), 0o400),
    }
    if crypttab is not None:
        files[crypttab[0]] = (crypttab[1].encode("utf-8"), 0o600)
    return newc_archive(files)
