"""Finding and clearing what an interrupted install left behind.

Written after watching it happen: the application was killed while an install
was running, and because the orchestrator that owns the cleanup died with it,
the medium stayed mounted, its LUKS volume stayed open, and the bootstrap
process it had started went on writing to the stick for nine more minutes --
orphaned, unable to ever finish, and heating the drive for nothing.

Nothing here guesses. Every leftover is identified by this project's own
naming (`poa-<media-id>` mappings, `/mnt/poa-install-*`, `/mnt/poa-image-*`),
so a device-mapper volume or mountpoint belonging to anything else on the
machine is never touched.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field

from .tooling import resolve_argv

#: Everything this project creates is named with this prefix, which is what
#: makes leftovers identifiable without keeping state across runs.
MAPPER_PREFIX = "poa-"
MOUNT_PREFIXES = ("/mnt/poa-install-", "/mnt/poa-image-")


@dataclass
class Leftovers:
    """What an interrupted run left on the machine."""

    mounts: list[str] = field(default_factory=list)
    mappings: list[str] = field(default_factory=list)
    processes: list[tuple[int, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.mounts or self.mappings or self.processes)

    def describe(self) -> str:
        lines = []
        if self.processes:
            lines.append("Незавершённые процессы установки:")
            lines += [f"    • {command} (pid {pid})" for pid, command in self.processes]
        if self.mounts:
            lines.append("Смонтировано:")
            lines += [f"    • {point}" for point in self.mounts]
        if self.mappings:
            lines.append("Открытые зашифрованные тома:")
            lines += [f"    • /dev/mapper/{name}" for name in self.mappings]
        return "\n".join(lines)


def find() -> Leftovers:
    return Leftovers(
        mounts=_our_mounts(),
        mappings=_our_mappings(),
        processes=_our_processes(),
    )


def _our_mounts() -> list[str]:
    points: list[str] = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) > 1 and fields[1].startswith(MOUNT_PREFIXES):
                    points.append(fields[1])
    except OSError:
        return []
    # Deepest first, so unmounting in this order never fights a busy parent.
    return sorted(set(points), key=len, reverse=True)


def _our_mappings() -> list[str]:
    proc = _run(["dmsetup", "ls"])
    if proc is None:
        return []
    names = []
    for line in proc.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith(MAPPER_PREFIX):
            names.append(name)
    return names


def _our_processes() -> list[tuple[int, str]]:
    """Bootstrap processes still writing to a medium nobody is managing.

    Matched on the mount root this project uses, so an unrelated debootstrap the
    administrator started by hand is left alone.
    """
    found: list[tuple[int, str]] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        except OSError:
            continue
        if not cmdline:
            continue
        if any(prefix in cmdline for prefix in MOUNT_PREFIXES):
            found.append((int(entry), cmdline[:120]))
    return found


def clear(leftovers: Leftovers | None = None) -> Leftovers:
    """Releases everything found, in the order that actually works.

    Processes first: a mountpoint with a live writer on it cannot be released,
    and a lazy unmount would merely postpone the writes rather than stop them.
    """
    leftovers = leftovers or find()

    for pid, _command in leftovers.processes:
        _terminate(pid)

    for point in leftovers.mounts:
        _run(["umount", "-l", point])

    for name in leftovers.mappings:
        # Retried once: a lazy unmount above may still be settling, and
        # cryptsetup refuses while the volume is technically still in use.
        if _run(["cryptsetup", "luksClose", name]) is None:
            _run(["sync"])
            _run(["cryptsetup", "luksClose", name])

    for point in leftovers.mounts:
        try:
            os.rmdir(point)
        except OSError:
            pass

    return find()


def _terminate(pid: int) -> None:
    import signal
    import time

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except OSError:
            return
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                return


def _run(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            resolve_argv(argv), capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None
