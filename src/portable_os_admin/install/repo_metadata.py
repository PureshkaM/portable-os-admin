"""What the target distribution says about its own package repositories.

Read off the installation image rather than written down here. The reason is
concrete: this project's documentation used to name `RPM-GPG-KEY-RED-SOFT` as
the RedOS signing key with the honest caveat that it was a plausible guess.
Checking the image settled it -- the guess was right -- but a value that has
to be guessed once will be wrong the next time, on another release or another
RHEL rebuild.

So the release package on the image is unpacked and its own `.repo` files are
read. What comes out is used only when the administrator has configured an
organisation mirror; with no mirror the release package installs those files
itself during the bootstrap, and this program has no business overwriting
them.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .rpm_extract import PackageNotOnImageError, find_package, unpack

#: The package that carries /etc/yum.repos.d/*.repo and the signing key. The
#: plain name is matched first so `redos-release` wins over
#: `redos-release-server`, which describes a variant rather than the base.
RELEASE_PACKAGE_PATTERNS = ("*-release-[0-9]*.noarch.rpm", "*-release-*.noarch.rpm", "*release*.rpm")

_KEY_RE = re.compile(r"^\s*gpgkey\s*=\s*(?P<value>\S+)", re.MULTILINE)
_CHECK_RE = re.compile(r"^\s*gpgcheck\s*=\s*(?P<value>[01])", re.MULTILINE)


@dataclass(frozen=True)
class RepoSigning:
    """How this distribution expects its packages to be verified."""

    #: e.g. "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"; "" when unknown
    gpgkey: str = ""
    gpgcheck: bool = False
    #: where the answer came from, for the log
    source: str = ""

    def as_repo_lines(self) -> str:
        if self.gpgcheck and self.gpgkey:
            return f"gpgcheck=1\ngpgkey={self.gpgkey}\n"
        # Saying "not verified" out loud beats pointing at a key file that may
        # not exist, which makes every dnf command fail with a confusing error.
        return "gpgcheck=0\n"


def discover_signing(mountpoint: str) -> RepoSigning:
    """Signing settings taken from the image's own release package.

    Never raises: a missing or unreadable release package means "unknown",
    which is reported as gpgcheck=0 with the reason, rather than as a failed
    installation.
    """
    try:
        package = find_package(mountpoint, RELEASE_PACKAGE_PATTERNS)
    except PackageNotOnImageError as exc:
        return RepoSigning(source=f"не найден пакет с настройками репозиториев: {exc}")

    with tempfile.TemporaryDirectory(prefix="poa-release-") as tmp:
        try:
            unpack(package, tmp)
        except PackageNotOnImageError as exc:
            return RepoSigning(source=f"не удалось распаковать {Path(package).name}: {exc}")

        for repo_file in sorted(Path(tmp).rglob("*.repo")):
            try:
                text = repo_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            key = _KEY_RE.search(text)
            check = _CHECK_RE.search(text)
            if key:
                return RepoSigning(
                    gpgkey=key.group("value"),
                    gpgcheck=bool(check and check.group("value") == "1"),
                    source=f"{Path(package).name} → {repo_file.name}",
                )
    return RepoSigning(source=f"в {Path(package).name} нет файлов репозиториев с ключом")


@dataclass(frozen=True)
class Repo:
    """Репозиторий из `.repo` пакета выпуска: id, первый адрес, ключ."""

    repo_id: str
    baseurl: str
    gpgkey: str = ""


_SECTION_RE = re.compile(r"^\s*\[(?P<id>[^\]]+)\]\s*$", re.MULTILINE)


def discover_repos(mountpoint: str, arch: str) -> tuple[Repo, ...]:
    """Официальные репозитории дистрибутива, как их описывает его пакет выпуска.

    Этап Э22: LiveCD собирается из свежих пакетов, и источником служит то, что
    дистрибутив сам записывает в /etc/yum.repos.d, а не адреса, придуманные
    здесь. Берутся только включённые разделы; из списка адресов -- первый
    (dnf `--repofrompath` принимает один), `$basearch` подставляется.
    Никогда не бросает: нечитаемый пакет -- пустой ответ.
    """
    try:
        package = find_package(mountpoint, RELEASE_PACKAGE_PATTERNS)
    except PackageNotOnImageError:
        return ()
    repos: list[Repo] = []
    with tempfile.TemporaryDirectory(prefix="poa-release-") as tmp:
        try:
            unpack(package, tmp)
        except PackageNotOnImageError:
            return ()
        for repo_file in sorted(Path(tmp).rglob("*.repo")):
            text = repo_file.read_text(encoding="utf-8", errors="replace")
            sections = list(_SECTION_RE.finditer(text))
            for index, match in enumerate(sections):
                end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
                body = text[match.end():end]
                fields = dict(
                    (k.strip().lower(), v.strip())
                    for k, _, v in (line.partition("=") for line in body.splitlines())
                    if _ and not k.strip().startswith("#")
                )
                if fields.get("enabled", "1") != "1" or not fields.get("baseurl"):
                    continue
                url = fields["baseurl"].split(",")[0].strip().replace("$basearch", arch)
                repos.append(Repo(match.group("id").strip(), url, fields.get("gpgkey", "")))
    return tuple(repos)
