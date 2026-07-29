"""Discovers external block devices eligible for installation.

Implements "Установка.ВыборНосителя" / "Интерфейсы оборудования-1,2,3". Reads
`lsblk -J` on Linux; `_lsblk_json` is factored out so tests can substitute
canned JSON without a real block device or even a Linux host.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from ..errors import DeviceNotEligibleError


@dataclass(frozen=True)
class BlockDevice:
    name: str
    path: str
    size_bytes: int
    model: str
    removable: bool
    mountpoints: tuple[str, ...]

    @property
    def is_mounted(self) -> bool:
        return any(m for m in self.mountpoints if m)


class DeviceLister:
    LSBLK_COLUMNS = "NAME,PATH,SIZE,MODEL,RM,TYPE,MOUNTPOINTS"

    def list_removable(self) -> list[BlockDevice]:
        data = self._lsblk_json()
        devices = []
        for entry in data.get("blockdevices", []):
            if entry.get("type") != "disk":
                continue
            if not _truthy(entry.get("rm")):
                continue
            devices.append(
                BlockDevice(
                    name=entry.get("name", ""),
                    path=entry.get("path") or f"/dev/{entry.get('name', '')}",
                    size_bytes=int(entry.get("size") or 0),
                    model=(entry.get("model") or "").strip(),
                    removable=True,
                    mountpoints=tuple(entry.get("mountpoints") or []),
                )
            )
        return devices

    def _lsblk_json(self) -> dict:
        proc = subprocess.run(
            ["lsblk", "-J", "-b", "-o", self.LSBLK_COLUMNS],
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


def check_install_eligible(device: BlockDevice, required_bytes: int) -> None:
    """Implements "Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
    a read-only sanity check performed before any data on the device changes."""
    if not device.removable:
        raise DeviceNotEligibleError(f"устройство {device.path} не является съёмным")
    if device.is_mounted:
        raise DeviceNotEligibleError(
            f"устройство {device.path} смонтировано ({', '.join(m for m in device.mountpoints if m)}); "
            "отмонтируйте перед установкой"
        )
    if device.size_bytes < required_bytes:
        raise DeviceNotEligibleError(
            f"недостаточно места на устройстве {device.path}: "
            f"{device.size_bytes} байт < требуется {required_bytes} байт"
        )
