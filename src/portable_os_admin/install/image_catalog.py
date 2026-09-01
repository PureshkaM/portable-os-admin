"""Что образ знает о себе сам: локали, пояса, раскладки, приложения.

Зачем это отдельный модуль
--------------------------

До 01.09.2026 всё это администратор вписывал руками, а программа предлагала
списки из двух пунктов: две локали, восемь часовых поясов, две раскладки. Имя
приложения для киоска нужно было помнить наизусть -- калькулятор в MATE
зовётся `mate-calc`, и узнать это из интерфейса было неоткуда.

Между тем образ несёт всё это сам, и он же -- единственный источник, который
не соврёт: списки, написанные в коде, устаревают к следующему выпуску
дистрибутива, а метаданные лежат внутри того самого файла, из которого
собирается носитель.

Откуда что берётся
------------------

| Что | РЕД ОС | Debian |
|---|---|---|
| Локали | `/usr/lib/locale/*` в `filelists.xml.gz` | `/usr/share/i18n/SUPPORTED` в пакете `locales` |
| Приложения | `*.desktop` там же | **неоткуда**: на DVD нет `Contents` |
| Часовые пояса | `usr/share/zoneinfo/*` пакета `tzdata` | то же |
| Раскладки | `rules/base.lst` пакета `xkeyboard-config` | то же в `xkb-data` |

Чтение потоковое: `filelists.xml.gz` РЕД ОС 8 -- 3,7 МБ сжатых, и разворачивать
его в память целиком незачем.

Ничего из прочитанного не подставляется само. Это предложение, а не решение:
администратор выбирает из списка, который заведомо есть в его образе, вместо
того чтобы вспоминать имя и узнавать об ошибке через сорок минут сборки.
"""
from __future__ import annotations

import gzip
import os
import re
import subprocess
import tempfile

#: Сколько ждать распаковки одного пакета. Пакеты здесь маленькие -- tzdata и
#: xkeyboard-config, -- и если распаковка идёт дольше, то что-то не так с
#: образом, а не с размером.
UNPACK_TIMEOUT = 60

#: Зоны, которые не предлагаются: служебные каталоги tzdata, а не места.
_ZONE_NOISE = ("right/", "posix/", "Etc/", "SystemV/", "US/", "Canada/", "Mexico/", "Brazil/")


def _find(directory: str, pattern: str) -> "str | None":
    """Первый файл каталога, чьё имя подходит под шаблон."""
    if not os.path.isdir(directory):
        return None
    regex = re.compile(pattern)
    for name in sorted(os.listdir(directory)):
        if regex.search(name):
            return os.path.join(directory, name)
    return None


def _filelists_paths(mountpoint: str, directories: bool = False) -> "list[str]":
    """Все пути всех пакетов образа РЕД ОС, потоком.

    `filelists.xml.gz` -- единственное место, где видно, какие файлы принесёт
    каждый пакет, не распаковывая его. Читается построчно: развернутый он
    вырастает на порядок.

    `directories` переключает, что именно нужно. Для приложений -- файлы:
    `<file>` без типа. Для локалей -- наоборот каталоги: локаль в RPM это
    `<file type="dir">/usr/lib/locale/ru_RU</file>`, и, отфильтровав каталоги
    заодно с прочим шумом, список локалей получаешь пустым. Так и вышло при
    первом прогоне -- нашлась ровно одна запись, `locale-archive`.
    """
    path = _find(os.path.join(mountpoint, "repodata"), r"filelists\.xml\.gz$")
    if not path:
        return []
    pattern = re.compile(
        r'<file type="dir">([^<]+)</file>' if directories else r"<file>([^<]+)</file>"
    )
    marker = '<file type="dir">' if directories else "<file>"
    found: list[str] = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if marker not in line:
                continue
            match = pattern.search(line)
            if match:
                found.append(match.group(1))
    return found


def _unpack(package: str, into: str) -> bool:
    """Распаковать один пакет во временный каталог.

    Оба семейства -- своим способом, и ни один не требует прав: rpm2cpio и
    dpkg-deb работают с файлом, а не с системой.
    """
    try:
        if package.endswith(".rpm"):
            with open(package, "rb") as handle:
                cpio = subprocess.run(
                    ["rpm2cpio"], stdin=handle, capture_output=True, timeout=UNPACK_TIMEOUT
                )
            if cpio.returncode != 0:
                return False
            extract = subprocess.run(
                ["cpio", "-idm", "--quiet"], input=cpio.stdout, cwd=into,
                capture_output=True, timeout=UNPACK_TIMEOUT,
            )
            return extract.returncode == 0
        result = subprocess.run(
            ["dpkg-deb", "-x", package, into], capture_output=True, timeout=UNPACK_TIMEOUT
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def locales(mountpoint: str, family: str) -> "list[str]":
    """Локали, которые этот образ умеет поставить.

    У семейств разные источники, и это не прихоть: в РЕД ОС каждая локаль --
    свой пакет `glibc-langpack-*`, и его файлы видны в метаданных; в Debian
    локали генерируются из одного списка `SUPPORTED`, который лежит внутри
    пакета `locales`.
    """
    if family == "redos":
        names = {
            path.rsplit("/", 1)[-1]
            for path in _filelists_paths(mountpoint, directories=True)
            if path.startswith("/usr/lib/locale/") and path.count("/") == 4
        }
        # UTF-8 и только он: koi8-r и cp1251 существуют, но ставить систему в
        # однобайтовой кодировке в 2026 году -- решение, которое надо
        # принимать сознательно, а не выбирать случайно из списка.
        return sorted(name for name in names if name.endswith(".utf8") or "." not in name)

    # `locales` -- бинарный пакет, а pool раскладывает по **исходному**: он
    # лежит в pool/main/g/glibc/, а не в pool/main/l/locales/. Промах здесь
    # выглядел как «в образе нет локалей».
    supported = _package_file(
        mountpoint, "debian", r"^locales_.*\.deb$", "usr/share/i18n/SUPPORTED",
        letter="g", source="glibc",
    )
    if not supported:
        return []
    found = []
    for line in supported.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Строка вида `ru_RU.UTF-8 UTF-8`: слева локаль, справа кодировка.
        name = line.split()[0]
        if name.endswith(".UTF-8"):
            found.append(name)
    return sorted(found)


def desktop_applications(mountpoint: str, family: str) -> "list[str]":
    """Имена `.desktop`, которые в этом образе вообще есть.

    Ради этого списка механизм и затевался: администратор задаёт киоску
    разрешённые приложения по имени записи, и до сих пор это имя надо было
    знать наизусть.

    Для Debian возвращается пусто, и это не недоделка: на установочном DVD нет
    файла `Contents`, то есть узнать, какой пакет какие файлы принесёт, из
    образа нельзя вовсе. Сказано здесь, чтобы следующий читатель не искал.
    """
    if family != "redos":
        return []
    names = {
        path.rsplit("/", 1)[-1][: -len(".desktop")]
        for path in _filelists_paths(mountpoint)
        if path.endswith(".desktop") and "/applications/" in path
    }
    return sorted(names)


def _package_file(
    mountpoint: str,
    family: str,
    package_pattern: str,
    inner: str,
    letter: str = "",
    source: str = "",
) -> str:
    """Содержимое одного файла из одного пакета образа.

    Пакет распаковывается во временный каталог и удаляется сразу: образы
    смонтированы только для чтения, а держать распакованное незачем.
    """
    package = _find_package(mountpoint, family, package_pattern, letter, source)
    if not package:
        return ""
    with tempfile.TemporaryDirectory() as temporary:
        if not _unpack(package, temporary):
            return ""
        target = os.path.join(temporary, inner)
        if not os.path.isfile(target):
            return ""
        with open(target, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()


def timezones(mountpoint: str, family: str) -> "list[str]":
    """Часовые пояса из tzdata самого образа.

    Служебные каталоги отброшены: `right/` и `posix/` -- те же зоны в другой
    интерпретации секунд, `Etc/` -- смещения без места, а `US/`, `Canada/` и
    прочие -- устаревшие имена, оставленные ради совместимости. Администратору
    нужен список мест, а не полный набор файлов пакета.
    """
    pattern = r"^tzdata-\d.*\.rpm$" if family == "redos" else r"^tzdata_.*\.deb$"
    package = _find_package(mountpoint, family, pattern, "t", "tzdata")
    if not package:
        return []
    with tempfile.TemporaryDirectory() as temporary:
        if not _unpack(package, temporary):
            return []
        root = os.path.join(temporary, "usr", "share", "zoneinfo")
        if not os.path.isdir(root):
            return []
        found = []
        for directory, _dirs, files in os.walk(root):
            for name in files:
                zone = os.path.relpath(os.path.join(directory, name), root)
                if zone.startswith(_ZONE_NOISE) or "." in name or name.islower():
                    continue
                if "/" not in zone and zone not in ("UTC",):
                    continue
                found.append(zone)
        return sorted(set(found))


def keyboard_layouts(mountpoint: str, family: str) -> "list[tuple[str, str]]":
    """Раскладки и их названия из xkeyboard-config образа.

    Возвращается пара: код (`ru`) и название (`Russian`). Код уезжает в
    конфигурацию, название показывается человеку -- по одному коду выбрать
    нельзя, если не знать заранее.
    """
    pattern = r"^xkeyboard-config-\d.*\.rpm$" if family == "redos" else r"^xkb-data_.*\.deb$"
    package = _find_package(mountpoint, family, pattern, "x", "xkeyboard-config")
    if not package:
        return []
    with tempfile.TemporaryDirectory() as temporary:
        if not _unpack(package, temporary):
            return []
        rules = os.path.join(temporary, "usr", "share", "X11", "xkb", "rules", "base.lst")
        if not os.path.isfile(rules):
            return []
        found: list[tuple[str, str]] = []
        inside = False
        with open(rules, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("! layout"):
                    inside = True
                    continue
                if line.startswith("!"):
                    inside = False
                    continue
                if inside and line.strip():
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        found.append((parts[0], parts[1].strip()))
        return found


def _find_package(mountpoint: str, family: str, pattern: str, letter: str, source: str) -> "str | None":
    """Где семейство держит пакеты.

    РЕД ОС раскладывает их по первой букве имени, Debian -- по имени исходного
    пакета внутри `pool/main/<буква>/`. Обе раскладки проверены на образах
    РЕД ОС 8 и Debian 13.
    """
    if family == "redos":
        return _find(os.path.join(mountpoint, "Packages", letter), pattern) or _find(
            os.path.join(mountpoint, "Packages"), pattern
        )
    return _find(os.path.join(mountpoint, "pool", "main", letter, source), pattern)
