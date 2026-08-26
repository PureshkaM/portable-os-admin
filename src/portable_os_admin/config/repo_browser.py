"""Live package browsing against a target OS's own repositories.

Lets a user building an InstallConfig pick packages to preinstall by name/
summary fetched from the real repository, instead of typing exact package
names from memory into `config.packages` blind. Two backends, matching the
two package-metadata formats actually in use by the supported install
targets (verified live against real mirrors, not guessed):

- AptPackageSource (Debian, Astra Linux): apt's `Packages.gz` index at
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
    """Debian/Astra Linux apt Packages index."""

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
        repomd_raw = _fetch(f"{base}/repodata/repomd.xml")
        try:
            repomd = ET.fromstring(repomd_raw)
        except ET.ParseError as exc:
            raise RepoBrowseError(f"не удалось разобрать repomd.xml: {exc}") from exc

        primary_href = None
        for data in repomd.findall("repo:data", _RPM_NS):
            if data.get("type") == "primary":
                location = data.find("repo:location", _RPM_NS)
                if location is not None:
                    primary_href = location.get("href")
                break
        if not primary_href:
            raise RepoBrowseError("в repomd.xml не найдены метаданные primary")

        primary_raw = _fetch(f"{base}/{primary_href}")
        primary_path = self._decompress(primary_href, primary_raw)
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
    if target_os in ("debian", "astra"):
        return AptPackageSource()
    if target_os == "redos":
        return DnfPackageSource()
    raise RepoBrowseError(f"просмотр пакетов из репозитория не поддерживается для {target_os!r}")
