"""Discovers external block devices eligible for installation.

Implements "Установка.ВыборНосителя" / "Интерфейсы оборудования-1,2,3". Reads
`lsblk -J` on Linux; `_lsblk_json` is factored out so tests can substitute
canned JSON without a real block device or even a Linux host.
"""
from __future__ import annotations

import os

import json
import subprocess
from dataclasses import dataclass

from ..errors import DeviceNotEligibleError
from .tooling import resolve_argv


@dataclass(frozen=True)
class Partition:
    """What currently occupies a slice of the medium.

    Carried so the installer can erase each old partition's signatures by name,
    and so the user can be shown exactly what is about to be destroyed instead
    of a bare "устройство будет очищено"."""

    path: str
    size_bytes: int
    fstype: str
    label: str
    mountpoint: str

    def describe(self) -> str:
        size_gb = self.size_bytes / 1024**3
        parts = [f"{self.path}", f"{size_gb:.1f} ГБ"]
        if self.fstype:
            parts.append(self.fstype)
        if self.label:
            parts.append(f"«{self.label}»")
        if self.mountpoint:
            parts.append(f"смонтирован в {self.mountpoint}")
        return ", ".join(parts)


@dataclass(frozen=True)
class BlockDevice:
    name: str
    path: str
    size_bytes: int
    model: str
    removable: bool
    mountpoints: tuple[str, ...]
    partitions: tuple[Partition, ...] = ()

    @property
    def is_mounted(self) -> bool:
        return any(m for m in self.mountpoints if m)

    @property
    def active_mountpoints(self) -> tuple[str, ...]:
        return tuple(m for m in self.mountpoints if m)

    @property
    def partition_paths(self) -> tuple[str, ...]:
        return tuple(p.path for p in self.partitions)


def collect_mountpoints(entry: dict) -> tuple[str, ...]:
    """Gathers mountpoints from a whole lsblk subtree, not just its root node.

    This matters more than it looks: a USB stick is virtually never mounted at
    the *disk* level -- the desktop automounter mounts its partitions
    (`/dev/sda1` -> `/media/user/flash`), so a check that only inspected the
    disk's own "mountpoints" array saw an empty list on every real machine and
    silently let the installer partition a device with live filesystems on it.
    Deepest-first ordering matters for unmounting, so children come first.
    """
    found: list[str] = []
    for child in entry.get("children") or []:
        found.extend(collect_mountpoints(child))
    for mountpoint in entry.get("mountpoints") or []:
        if mountpoint:
            found.append(mountpoint)
    return tuple(found)


def collect_partitions(entry: dict) -> tuple[Partition, ...]:
    """Flattens an lsblk subtree into the partitions it holds.

    Nested: a partition can carry an open LUKS mapping or an LVM volume as a
    child, and those hold the filesystem signatures that must be erased too.
    """
    found: list[Partition] = []
    for child in entry.get("children") or []:
        if child.get("type") in ("part", "crypt", "lvm"):
            mountpoints = [m for m in (child.get("mountpoints") or []) if m]
            found.append(
                Partition(
                    path=child.get("path") or f"/dev/{child.get('name', '')}",
                    size_bytes=int(child.get("size") or 0),
                    fstype=(child.get("fstype") or "").strip(),
                    label=(child.get("label") or "").strip(),
                    mountpoint=mountpoints[0] if mountpoints else "",
                )
            )
        found.extend(collect_partitions(child))
    return tuple(found)


class DeviceLister:
    LSBLK_COLUMNS = "NAME,PATH,SIZE,MODEL,RM,TRAN,TYPE,FSTYPE,LABEL,MOUNTPOINTS"

    def list_removable(self) -> list[BlockDevice]:
        data = self._lsblk_json()
        devices = []
        for entry in data.get("blockdevices", []):
            if entry.get("type") != "disk":
                continue
            # lsblk's own RM ("removable") bit is frequently wrong for real USB
            # sticks -- many controllers/enclosures report RM=0 even though the
            # disk is plainly hot-pluggable. Treat "connected over USB" (TRAN)
            # as an equally valid signal so a real flash drive isn't silently
            # dropped from the list just because RM lied.
            if not _truthy(entry.get("rm")) and (entry.get("tran") or "").strip().lower() != "usb":
                continue
            devices.append(
                BlockDevice(
                    name=entry.get("name", ""),
                    path=entry.get("path") or f"/dev/{entry.get('name', '')}",
                    size_bytes=int(entry.get("size") or 0),
                    model=(entry.get("model") or "").strip(),
                    removable=True,
                    mountpoints=collect_mountpoints(entry),
                    partitions=collect_partitions(entry),
                )
            )
        return devices

    def find(self, path: str) -> BlockDevice | None:
        """Re-reads one device's current state by path, so a caller can verify
        (rather than assume) that something like an unmount actually took."""
        for device in self.list_removable():
            if device.path == path:
                return device
        return None

    def describe(self, path: str) -> BlockDevice | None:
        """Reads one device by path, bypassing the removable filter.

        This is for a path the user typed in by hand -- which is precisely the
        case where the removable heuristics already failed, so re-applying them
        would just refuse the device again. What matters is that the caller
        gets the device's *real* size and, above all, its real mountpoints: a
        device object invented from a text field has none, and the installer's
        unmount preflight would then silently do nothing and partition over a
        live filesystem.

        Whether the device is an acceptable install target remains the caller's
        decision (and the user's, via the repeat-the-path confirmation).
        """
        try:
            data = self._lsblk_json(path)
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
        # "loop" наравне с "disk": файл-образ, подключённый через losetup, --
        # это штатная цель стенда, а lsblk называет его типом "loop". Фильтр
        # по одному "disk" отбрасывал его, describe возвращал None, и мастер
        # строил устройство из текстовых полей. Последствие серьёзнее пустого
        # списка разделов на экране подтверждения: вместе с ними терялись
        # точки монтирования, ради которых эта функция и написана, -- то есть
        # на файл-образе не работала защита от разметки поверх смонтированной
        # файловой системы. Найдено живым прогоном 28.08.2026: экран сказал
        # «разделов не обнаружено» о носителе с пятью разделами.
        entries = [e for e in data.get("blockdevices", []) if e.get("type") in ("disk", "loop")]
        if not entries:
            return None
        entry = entries[0]
        return BlockDevice(
            name=entry.get("name", ""),
            path=entry.get("path") or path,
            size_bytes=int(entry.get("size") or 0),
            model=(entry.get("model") or "").strip(),
            removable=True,
            mountpoints=collect_mountpoints(entry),
            partitions=collect_partitions(entry),
        )

    def _lsblk_json(self, path: str | None = None) -> dict:
        argv = ["lsblk", "-J", "-b", "-o", self.LSBLK_COLUMNS]
        if path:
            argv.append(path)
        proc = subprocess.run(
            resolve_argv(argv),
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(proc.stdout)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip() in ("1", "true", "True")
    return bool(value)


#: Отмычка для стенда, наружу не выводится — как POA_NO_ELEVATE.
#:
#: Нужна ровно для одного: разворачивать на файл-образ через losetup вместо
#: настоящей флешки. Цена вопроса измерена — копирование 4 ГБ на тестовый
#: носитель занимает час, на файл в ВМ секунды, и однострочная ошибка на
#: последнем шаге стоит этого часа. Установщик при этом выполняет ровно тот же
#: код: разметку, LUKS, копирование, chroot, dracut, загрузчик.
#:
#: В обычной работе снимать эту проверку нельзя: она стоит между «стереть
#: флешку» и «стереть системный диск».
ALLOW_NON_REMOVABLE = "POA_ALLOW_NON_REMOVABLE"


def _non_removable_allowed() -> bool:
    return bool(os.environ.get(ALLOW_NON_REMOVABLE))


def check_install_eligible(
    device: BlockDevice, required_bytes: int, *, require_unmounted: bool = True
) -> None:
    """Implements "Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
    a read-only sanity check performed before any data on the device changes.

    `require_unmounted=False` is for the installer itself, which unmounts the
    device as an explicit, logged preflight step instead of refusing outright --
    the user has already confirmed they are about to erase it.
    """
    if not device.removable and not _non_removable_allowed():
        raise DeviceNotEligibleError(f"устройство {device.path} не является съёмным")
    if require_unmounted and device.is_mounted:
        raise DeviceNotEligibleError(
            f"устройство {device.path} смонтировано ({', '.join(device.active_mountpoints)}); "
            "отмонтируйте перед установкой"
        )
    if device.size_bytes < required_bytes:
        raise DeviceNotEligibleError(
            f"недостаточно места на устройстве {device.path}: "
            f"{device.size_bytes} байт < требуется {required_bytes} байт"
        )
