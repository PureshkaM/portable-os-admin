"""Reading single packages out of a mounted installation image.

An image is a repository, and some of the answers this program needs are
inside its packages rather than loose on the disc: the kickstart parser used
to check configurations, and the repository settings (including the signing
key's path) that the distribution's own release package installs.

Nothing about the layout is assumed. Packages are found by pattern, unpacked
with whichever extractor the machine has, and the result is read from disk --
so a rebuild that moves things around costs a slower search rather than a
wrong answer.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .tooling import resolve_tool

#: Where images usually keep their packages. Tried in order, then the whole
#: tree, so an unusual layout still works.
PACKAGE_DIRS = ("Packages", "AppStream/Packages", "BaseOS/Packages", "RedOS/Packages")


class PackageNotOnImageError(RuntimeError):
    """The image has no such package, or nothing here can unpack it."""


def find_package(mountpoint: str, patterns: tuple[str, ...]) -> str:
    """Path to the first package on the image matching any of `patterns`."""
    root = Path(mountpoint)
    searched = [root / name for name in PACKAGE_DIRS if (root / name).is_dir()]
    searched.append(root)
    for directory in searched:
        for pattern in patterns:
            matches = sorted(directory.rglob(pattern))
            if matches:
                return str(matches[0])
    raise PackageNotOnImageError(
        "на образе не найден пакет по образцу " + ", ".join(patterns)
    )


def _extractors(rpm_path: str, dest: str) -> list[tuple[str, list[str]]]:
    """Ways to unpack an RPM, best first.

    bsdtar reads the RPM container directly; rpm2cpio is the traditional pair;
    7z needs a second pass over the cpio it leaves behind. Several are offered
    so this works across machines instead of demanding one particular package.
    """
    return [
        ("bsdtar", ["bsdtar", "-x", "-f", rpm_path, "-C", dest]),
        ("rpm2cpio", ["sh", "-c", f"rpm2cpio {rpm_path!r} | cpio -idm --quiet"]),
        ("7z", ["7z", "x", "-y", f"-o{dest}", rpm_path]),
    ]


def unpack(rpm_path: str, dest: str) -> str:
    """Unpacks `rpm_path` into `dest`; returns the tool that managed it."""
    os.makedirs(dest, exist_ok=True)
    tried = []
    for tool, argv in _extractors(rpm_path, dest):
        if tool != "sh" and resolve_tool(tool) is None:
            tried.append(tool)
            continue
        if tool == "rpm2cpio" and resolve_tool("cpio") is None:
            tried.append("rpm2cpio+cpio")
            continue
        proc = subprocess.run(argv, cwd=dest, capture_output=True, text=True, timeout=180, check=False)
        if proc.returncode == 0:
            if tool == "7z":
                _unpack_cpio_leftovers(dest)
            return tool
        tried.append(f"{tool} (код {proc.returncode})")
    raise PackageNotOnImageError(
        "нечем распаковать пакет с образа: "
        + (", ".join(tried) if tried else "ни один инструмент не найден")
        + ". Установите bsdtar (пакет libarchive-tools) либо rpm."
    )


def _unpack_cpio_leftovers(dest: str) -> None:
    """7z stops at the cpio inside the RPM, so that has to be opened too."""
    for entry in sorted(Path(dest).glob("*.cpio")):
        subprocess.run(
            ["7z", "x", "-y", f"-o{dest}", str(entry)],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        entry.unlink(missing_ok=True)
