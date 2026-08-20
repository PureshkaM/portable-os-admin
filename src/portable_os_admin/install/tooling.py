"""Locating the external utilities the installer drives.

Every tool used here (`wipefs`, `parted`, `cryptsetup`, `mkfs.*`, `sgdisk`,
`partprobe`, `debootstrap`) lives in `/usr/sbin` or `/sbin`, which Debian and
RHEL-family distributions deliberately keep out of a *normal* user's `PATH`.
That matters because of how this application is launched: `sudo -E` (and the
desktop launcher) preserve the invoking user's `PATH`, so the process ends up
running as root with a `PATH` that cannot see any of its own tools. The
symptom is a bare `[Errno 2] No such file or directory: 'wipefs'` in the
middle of an install -- observed on a real machine.

Resolving tools here rather than trusting `PATH` also lets the installer say
"утилита не найдена, поставьте пакет util-linux" instead of surfacing an
errno at the user.
"""
from __future__ import annotations

import os
import shutil

from ..errors import InstallError

#: Prepended to whatever PATH the process inherited. Not a replacement: a
#: deliberately customised PATH (a test harness, a non-standard install) must
#: still win, so these are only a fallback for the standard locations.
SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")

#: Which package to name when a tool is missing, per family. Guessing wrong is
#: worse than saying nothing, so only tools verified to come from these
#: packages are listed.
PROVIDED_BY = {
    "wipefs": "util-linux",
    "lsblk": "util-linux",
    "blkid": "util-linux",
    "parted": "parted",
    "partprobe": "parted",
    "sgdisk": "gdisk (Debian) / gdisk (RHEL)",
    "cryptsetup": "cryptsetup",
    "mkfs.ext4": "e2fsprogs",
    "mkfs.vfat": "dosfstools",
    "udevadm": "systemd",
    "debootstrap": "debootstrap",
    "dnf": "dnf",
    "chroot": "coreutils",
    "mount": "util-linux",
    "umount": "util-linux",
}


def search_path() -> str:
    inherited = os.environ.get("PATH", "")
    parts = [p for p in inherited.split(os.pathsep) if p]
    for directory in SBIN_DIRS:
        if directory not in parts:
            parts.append(directory)
    return os.pathsep.join(parts)


def resolve_tool(name: str) -> str | None:
    """Absolute path of `name`, or None when it genuinely is not installed."""
    if os.path.isabs(name):
        return name if os.path.exists(name) else None
    return shutil.which(name, path=search_path())


def resolve_argv(argv: list[str]) -> list[str]:
    """Returns argv with its executable replaced by an absolute path.

    Left untouched when the tool cannot be found: the caller reports missing
    tools through `check_tools_available`, and a command that slips through
    should fail with its own error rather than being silently rewritten.
    """
    if not argv:
        return argv
    resolved = resolve_tool(argv[0])
    if resolved is None:
        return argv
    return [resolved, *argv[1:]]


def describe_missing(names: list[str]) -> str:
    lines = []
    for name in names:
        package = PROVIDED_BY.get(name)
        lines.append(f"  {name}" + (f"  (пакет {package})" if package else ""))
    return "\n".join(lines)


def check_tools_available(names: list[str]) -> None:
    """Raises if any tool is missing, listing all of them at once.

    Reported before anything is written, so the administrator learns the whole
    list up front instead of discovering the tools one failed install at a
    time.
    """
    missing = []
    for name in names:
        if name not in missing and resolve_tool(name) is None:
            missing.append(name)
    if not missing:
        return
    raise InstallError(
        "не найдены необходимые системные утилиты:\n"
        + describe_missing(missing)
        + "\n\nЕсли они установлены, программа запущена с PATH, в котором нет "
        "/usr/sbin — так бывает при запуске через «sudo -E» от обычного "
        "пользователя."
    )
