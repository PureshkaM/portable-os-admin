"""Live package browsing against a target OS's own repositories.

Lets a user building an InstallConfig pick packages to preinstall by name/
summary fetched from the real repository, instead of typing exact package
names from memory into `config.packages` blind. Two backends, matching the
two package-metadata formats actually in use by the supported install
targets (verified live against real mirrors, not guessed):

- AptPackageSource (Debian): apt's `Packages.gz` index at
  `<mirror>/dists/<release>/<component>/binary-<arch>/Packages.gz` --
  confirmed against deb.debian.org/debian/dists/bookworm.
- DnfPackageSource (RedOS): rpm-md `repodata/repomd.xml` -> primary
  metadata. Confirmed live against repo1.red-soft.ru/redos/8.0: RedOS's
  primary metadata is zstd-compressed (`*-primary.xml.zst`), not gzip, so
  this shells out to the `zstd` CLI (the same class of external-tool
  dependency as parted/cryptsetup elsewhere in this project) instead of
  adding a `zstandard` pip dependency just for this one lookup.

Both sources are read-only HTTP GETs against public metadata endpoints --
nothing here executes a destructive command, so none of this goes through
CommandRunner's dry-run gate.

Основной источник с 15.09.2026 -- не зеркало, а установочный образ
(`list_image_packages`, `load_packages`): в закрытом контуре сети нет, а образ
есть всегда. Зеркало осталось запасным путём. Разбор индексов общий -- формат
метаданных не зависит от того, где они лежат.
"""
from __future__ import annotations

import os

import gzip
import lzma
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET

from ..errors import RepoBrowseError

_TIMEOUT = 30
_USER_AGENT = "portable-os-admin/0.1 (+package-browser)"
_RPM_NS = {"repo": "http://linux.duke.edu/metadata/repo"}


@dataclass(frozen=True)
class RepoPackage:
    name: str
    version: str
    summary: str


def as_repository_url(location: str) -> str:
    """Приводит адрес репозитория к виду, который понимает загрузчик.

    Смонтированный установочный образ — это тот же репозиторий: и rpm-md, и
    apt раскладывают метаданные одинаково, где бы они ни лежали. Поэтому
    обычный путь превращается в file://, и дальше всё работает без единой
    правки в разборе.

    Это важнее, чем кажется: основной путь проекта — контур без выхода в
    интернет, а просмотр пакетов до сих пор требовал сети, то есть именно там,
    где программой и пользуются, эта функция не работала.
    """
    location = location.strip()
    if not location:
        return ""
    if "://" in location:
        return location
    return "file://" + os.path.abspath(location)


def _fetch(url: str) -> bytes:
    url = as_repository_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise RepoBrowseError(f"не удалось получить {url}: {exc}") from exc


class PackageSource(Protocol):
    def list_packages(self, mirror: str, release: str) -> list[RepoPackage]:
        ...


class AptPackageSource:
    """Debian apt Packages index."""

    def __init__(self, component: str = "main", arch: str = "amd64"):
        self.component = component
        self.arch = arch

    def list_packages(self, mirror: str, release: str) -> list[RepoPackage]:
        if not mirror:
            raise RepoBrowseError("для просмотра пакетов нужно указать адрес репозитория (mirror)")
        if not release:
            raise RepoBrowseError("для просмотра пакетов нужно указать релиз/suite")
        base = mirror.rstrip("/")
        url = f"{base}/dists/{release}/{self.component}/binary-{self.arch}/Packages.gz"
        raw = _fetch(url)
        try:
            text = gzip.decompress(raw).decode("utf-8", errors="replace")
        except OSError as exc:
            raise RepoBrowseError(f"не удалось распаковать индекс пакетов: {exc}") from exc
        return self.parse_index(text)

    @staticmethod
    def parse_index(text: str) -> list[RepoPackage]:
        """Разбор индекса `Packages` -- общий для зеркала и образа."""
        packages: list[RepoPackage] = []
        name = version = summary = ""
        for line in text.splitlines():
            if line.startswith("Package:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("Version:"):
                version = line.split(":", 1)[1].strip()
            elif line.startswith("Description:") and not summary:
                summary = line.split(":", 1)[1].strip()
            elif not line.strip():
                if name:
                    packages.append(RepoPackage(name=name, version=version, summary=summary))
                name = version = summary = ""
        if name:
            packages.append(RepoPackage(name=name, version=version, summary=summary))
        return packages


class DnfPackageSource:
    """RHEL-family (RedOS) rpm-md repodata."""

    def __init__(self, arches: tuple[str, ...] = ("x86_64", "noarch")):
        self.arches = arches

    def list_packages(self, mirror: str, release: str) -> list[RepoPackage]:
        if not mirror:
            raise RepoBrowseError("для просмотра пакетов нужно указать адрес репозитория (mirror)")
        base = mirror.rstrip("/")
        primary_href = self.primary_href(_fetch(f"{base}/repodata/repomd.xml"))
        if not primary_href:
            raise RepoBrowseError("в repomd.xml не найдены метаданные primary")

        primary_raw = _fetch(f"{base}/{primary_href}")
        return self.parse_primary_bytes(primary_href, primary_raw)

    @staticmethod
    def primary_href(repomd_raw: bytes) -> "str | None":
        """Путь к метаданным primary из repomd.xml, если он там объявлен."""
        try:
            repomd = ET.fromstring(repomd_raw)
        except ET.ParseError as exc:
            raise RepoBrowseError(f"не удалось разобрать repomd.xml: {exc}") from exc
        for data in repomd.findall("repo:data", _RPM_NS):
            if data.get("type") == "primary":
                location = data.find("repo:location", _RPM_NS)
                return location.get("href") if location is not None else None
        return None

    def parse_primary_bytes(self, href: str, raw: bytes) -> list[RepoPackage]:
        """Разбор primary в любом из сжатий -- общий для зеркала и образа."""
        primary_path = self._decompress(href, raw)
        try:
            return self._parse_primary(primary_path)
        finally:
            shutil.rmtree(primary_path.parent, ignore_errors=True)

    def _decompress(self, href: str, raw: bytes) -> Path:
        suffix = Path(href).suffix
        tmp_dir = Path(tempfile.mkdtemp(prefix="poa-repo-"))
        src = tmp_dir / f"primary{suffix}"
        src.write_bytes(raw)

        if suffix == ".zst":
            if shutil.which("zstd") is None:
                raise RepoBrowseError(
                    "метаданные RedOS сжаты zstd, а утилита zstd не найдена в PATH — установите пакет zstd"
                )
            dst = tmp_dir / "primary.xml"
            proc = subprocess.run(
                ["zstd", "-d", "--long=31", "-f", str(src), "-o", str(dst)],
                capture_output=True,
                timeout=120,
            )
            if proc.returncode != 0:
                raise RepoBrowseError(
                    f"zstd не смог распаковать метаданные: {proc.stderr.decode(errors='replace')}"
                )
            return dst

        if suffix == ".gz":
            dst = tmp_dir / "primary.xml"
            with gzip.open(src, "rb") as f_in, open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            return dst

        if suffix == ".xz":
            dst = tmp_dir / "primary.xml"
            with lzma.open(src, "rb") as f_in, open(dst, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            return dst

        return src

    def _parse_primary(self, path: Path) -> list[RepoPackage]:
        packages: dict[str, RepoPackage] = {}
        try:
            for _, elem in ET.iterparse(str(path), events=("end",)):
                if elem.tag.rsplit("}", 1)[-1] != "package" or elem.get("type") != "rpm":
                    continue
                arch = self._child_text(elem, "arch")
                if arch not in self.arches:
                    elem.clear()
                    continue
                name = self._child_text(elem, "name")
                if name:
                    summary = self._child_text(elem, "summary") or ""
                    version = self._version_text(elem)
                    packages.setdefault(name, RepoPackage(name=name, version=version, summary=summary))
                elem.clear()
        except ET.ParseError as exc:
            raise RepoBrowseError(f"не удалось разобрать метаданные пакетов: {exc}") from exc
        return sorted(packages.values(), key=lambda p: p.name)

    @staticmethod
    def _child(elem: ET.Element, local_name: str) -> ET.Element | None:
        for child in elem:
            if child.tag.rsplit("}", 1)[-1] == local_name:
                return child
        return None

    @classmethod
    def _child_text(cls, elem: ET.Element, local_name: str) -> str | None:
        child = cls._child(elem, local_name)
        return child.text.strip() if child is not None and child.text else None

    @classmethod
    def _version_text(cls, elem: ET.Element) -> str:
        version_elem = cls._child(elem, "version")
        if version_elem is None:
            return ""
        ver = version_elem.get("ver", "")
        rel = version_elem.get("rel")
        return f"{ver}-{rel}" if rel else ver


def get_package_source(target_os: str) -> PackageSource:
    if target_os == "debian":
        return AptPackageSource()
    if target_os == "redos":
        return DnfPackageSource()
    raise RepoBrowseError(f"просмотр пакетов из репозитория не поддерживается для {target_os!r}")


#: Имена архитектур в раскладке apt. Конфигурация принимает оба написания
#: (см. `schema.SUPPORTED_ARCH`), а каталог образа называется только так.
_APT_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def list_image_packages(
    mountpoint: str, target_os: str, release: str = "", arch: str = ""
) -> list[RepoPackage]:
    """Пакеты смонтированного установочного образа -- без сети.

    Основной путь проекта -- закрытый контур, и именно там просмотр пакетов
    через зеркало не работал: метаданные скачивались из интернета. Между тем
    образ, из которого собирается носитель, сам является репозиторием, и его
    список -- ещё и точнее зеркального: предложено будет ровно то, что потом
    поставится, а не то, чего на DVD нет.

    Раскладка та же, что уже читают `install/image_source.py` и
    `install/image_catalog.py`: у РЕД ОС `repodata/` в корне образа, у Debian
    `dists/<выпуск>/<компонент>/binary-<arch>/Packages*`. Файлы читаются
    напрямую, а не через file:// -- так ошибка называет путь на образе.
    """
    if not mountpoint or not os.path.isdir(mountpoint):
        raise RepoBrowseError(f"образ не смонтирован: {mountpoint!r}")
    if target_os == "redos":
        return _rpm_image_packages(mountpoint)
    if target_os == "debian":
        return _apt_image_packages(mountpoint, release, arch)
    raise RepoBrowseError(f"просмотр пакетов образа не поддерживается для {target_os!r}")


def _rpm_image_packages(mountpoint: str) -> list[RepoPackage]:
    repodata = os.path.join(mountpoint, "repodata")
    if not os.path.isdir(repodata):
        raise RepoBrowseError("в образе нет каталога repodata/ -- это не образ РЕД ОС")
    source = DnfPackageSource()
    href = None
    repomd = os.path.join(repodata, "repomd.xml")
    if os.path.isfile(repomd):
        with open(repomd, "rb") as handle:
            href = source.primary_href(handle.read())
    if not href:
        # repomd.xml -- штатный указатель, но без него primary всё равно
        # узнаётся по имени: `<хеш>-primary.xml.gz` (или .zst/.xz).
        names = sorted(name for name in os.listdir(repodata) if "primary.xml" in name)
        if not names:
            raise RepoBrowseError("в repodata/ образа не найдены метаданные primary")
        href = f"repodata/{names[0]}"
    path = os.path.join(mountpoint, href)
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise RepoBrowseError(f"не удалось прочитать {href} с образа: {exc}") from exc
    return source.parse_primary_bytes(href, raw)


def _apt_image_packages(mountpoint: str, release: str, arch: str) -> list[RepoPackage]:
    dists = os.path.join(mountpoint, "dists")
    if not os.path.isdir(dists):
        raise RepoBrowseError("в образе нет каталога dists/ -- это не образ Debian")
    # Выпуск из конфигурации -- если он на образе есть. Иначе любой с файлом
    # Release: на DVD их обычно два, `trixie` и ссылка `stable` на него же,
    # поэтому одинаковые каталоги сводятся по настоящему пути.
    if release and os.path.isdir(os.path.join(dists, release)):
        suites = [os.path.join(dists, release)]
    else:
        seen: set[str] = set()
        suites = []
        for name in sorted(os.listdir(dists)):
            suite = os.path.join(dists, name)
            real = os.path.realpath(suite)
            if os.path.isfile(os.path.join(suite, "Release")) and real not in seen:
                seen.add(real)
                suites.append(suite)
    wanted_arch = _APT_ARCH.get(arch, arch)

    packages: dict[str, RepoPackage] = {}
    read_any = False
    for suite in suites:
        # Компоненты не перечисляются в коде: main и contrib есть почти всегда,
        # non-free-firmware -- с Debian 12, и берётся всё, что образ несёт.
        components = sorted(
            (name for name in os.listdir(suite) if os.path.isdir(os.path.join(suite, name))),
            key=lambda name: (name != "main", name),
        )
        for component in components:
            index = _apt_index(os.path.join(suite, component), wanted_arch)
            if index is None:
                continue
            read_any = True
            for package in AptPackageSource.parse_index(_read_index(index)):
                packages.setdefault(package.name, package)
    if not read_any:
        raise RepoBrowseError(
            "в dists/ образа не найден индекс пакетов"
            + (f" для архитектуры {wanted_arch}" if wanted_arch else "")
        )
    return sorted(packages.values(), key=lambda p: p.name)


def _apt_index(component_dir: str, arch: str) -> "str | None":
    """Индекс компонента: `.gz` раньше `.xz`, сжатый раньше голого."""
    if arch:
        candidates = [os.path.join(component_dir, f"binary-{arch}")]
    else:
        candidates = sorted(
            os.path.join(component_dir, name)
            for name in os.listdir(component_dir)
            if name.startswith("binary-") and name != "binary-all"
        )
    for binary_dir in candidates:
        for name in ("Packages.gz", "Packages.xz", "Packages"):
            path = os.path.join(binary_dir, name)
            if os.path.isfile(path):
                return path
    return None


def _read_index(path: str) -> str:
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        if path.endswith(".xz"):
            with lzma.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                return handle.read()
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, EOFError, lzma.LZMAError) as exc:
        raise RepoBrowseError(f"не удалось прочитать индекс пакетов {path}: {exc}") from exc


def load_packages(
    target_os: str,
    release: str,
    image_mountpoint: str = "",
    mirror: str = "",
    arch: str = "",
) -> "tuple[list[RepoPackage], str]":
    """Каталог пакетов: сначала образ, сеть -- только запасной путь.

    Возвращает список и откуда он взят (`image` или `network`), чтобы
    интерфейс мог честно сказать, что показано. Если образ прочитать не
    удалось, а зеркало задано, пробуется зеркало; если не вышло и там --
    ошибка называет обе причины, а не только последнюю: иначе администратор в
    закрытом контуре увидел бы «нет сети» и не узнал, что было не так с
    образом.
    """
    image_error = ""
    if image_mountpoint:
        try:
            return list_image_packages(image_mountpoint, target_os, release, arch), "image"
        except RepoBrowseError as exc:
            image_error = str(exc)
            if not mirror:
                raise
    if not mirror:
        raise RepoBrowseError("не выбран установочный образ и не задан адрес репозитория")
    try:
        return get_package_source(target_os).list_packages(mirror, release), "network"
    except RepoBrowseError as exc:
        if image_error:
            raise RepoBrowseError(f"образ: {image_error}; репозиторий: {exc}") from exc
        raise
