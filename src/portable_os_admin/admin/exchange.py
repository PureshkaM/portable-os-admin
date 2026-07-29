"""Exchange-partition manager (UC-6 "Взаимодействие с разделом обмена").

POA_EXCHANGE is a plain ext4 partition (outside the LUKS system volume, see
install/disk.py) used to shuttle files between the administrator and the
portable-system user ("Обмен.Назначение"). Files are never auto-executed
("Обмен.Исполнение") -- this module only ever copies bytes, it never invokes
anything it finds on the partition.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import ExchangeError, InsufficientSpaceError
from ..install.command_runner import CommandPlan, CommandRunner


@dataclass(frozen=True)
class ExchangeFileInfo:
    name: str
    size_bytes: int


class ExchangeManager:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def mount(self, partition: str, mount_point: str, plan: CommandPlan, *, dry_run: bool = True, confirm_device: str | None = None) -> None:
        self.runner.run(["mkdir", "-p", mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)
        self.runner.run(["mount", partition, mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)

    def unmount(self, mount_point: str, plan: CommandPlan, *, dry_run: bool = True, confirm_device: str | None = None) -> None:
        self.runner.run(["umount", mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)

    def list_files(self, mount_point: str) -> list[ExchangeFileInfo]:
        """"Обмен.Список"."""
        directory = Path(mount_point)
        if not directory.is_dir():
            raise ExchangeError(f"раздел обмена недоступен: {mount_point}")
        return sorted(
            (ExchangeFileInfo(name=p.name, size_bytes=p.stat().st_size) for p in directory.iterdir() if p.is_file()),
            key=lambda info: info.name,
        )

    def write_file(self, mount_point: str, source_path: Path, dest_name: str | None = None) -> None:
        """"Обмен.Запись"; checks free space first ("Обмен.Место.Недостаточно")."""
        directory = Path(mount_point)
        if not directory.is_dir():
            raise ExchangeError(f"раздел обмена недоступен: {mount_point}")

        source_path = Path(source_path)
        size = source_path.stat().st_size
        free = shutil.disk_usage(directory).free
        if size > free:
            raise InsufficientSpaceError(
                f"недостаточно свободного места в разделе обмена: требуется {size} байт, доступно {free} байт"
            )

        destination = directory / (dest_name or source_path.name)
        shutil.copy2(source_path, destination)

    def read_file(self, mount_point: str, name: str, destination_path: Path) -> None:
        """"Обмен.Чтение": copies a file from the exchange partition out to
        the administrator's workstation."""
        source = Path(mount_point) / name
        if not source.is_file():
            raise ExchangeError(f"файл не найден в разделе обмена: {name}")
        shutil.copy2(source, destination_path)

    def delete_file(self, mount_point: str, name: str) -> None:
        """"Обмен.Удаление". Confirmation (UC-6's "Система запрашивает
        подтверждение удаления") is a UI-layer concern; by the time this is
        called the operator has already confirmed."""
        target = Path(mount_point) / name
        if not target.is_file():
            raise ExchangeError(f"файл не найден в разделе обмена: {name}")
        target.unlink()
