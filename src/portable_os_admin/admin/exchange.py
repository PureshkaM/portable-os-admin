"""Exchange-partition manager (UC-6 "Взаимодействие с разделом обмена").

POA_EXCHANGE is a plain ext4 partition (outside the LUKS system volume, see
install/disk.py) used to shuttle files between the administrator and the
portable-system user ("Обмен.Назначение"). Files are never auto-executed
("Обмен.Исполнение") -- this module only ever copies bytes, it never invokes
anything it finds on the partition.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import ExchangeError, InsufficientSpaceError
from ..install.command_runner import CommandPlan, CommandRunner


@dataclass(frozen=True)
class ExchangeFileInfo:
    name: str
    size_bytes: int


def _tree_size(path: Path) -> int:
    """Сколько занимает каталог со всем содержимым."""
    total = 0
    for directory, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(directory, name))
            except OSError:
                # Файл мог исчезнуть между обходом и замером -- это не повод
                # отказываться отвечать на вопрос «сколько примерно».
                pass
    return total


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
        entries = []
        for item in directory.iterdir():
            if item.is_file():
                entries.append(ExchangeFileInfo(name=item.name, size_bytes=item.stat().st_size))
            elif item.is_dir():
                # Каталог показывается своим размером -- суммой того, что в
                # нём лежит. Через обмен едут настройки программ, а это чаще
                # дерево, чем один файл; каталог, не показанный вовсе, выглядел
                # бы как пропавшая работа.
                entries.append(
                    ExchangeFileInfo(name=item.name + "/", size_bytes=_tree_size(item))
                )
        return sorted(entries, key=lambda info: info.name)

    def free_space(self, mount_point: str) -> "tuple[int, int]":
        """(свободно, всего) байт в разделе обмена.

        Показывается до записи, а не выясняется отказом после неё: раздел
        обмена невелик, а кладут в него то, что уже выбрали.
        """
        usage = shutil.disk_usage(Path(mount_point))
        return usage.free, usage.total

    def write_file(self, mount_point: str, source_path: Path, dest_name: str | None = None) -> None:
        """"Обмен.Запись"; checks free space first ("Обмен.Место.Недостаточно")."""
        directory = Path(mount_point)
        if not directory.is_dir():
            raise ExchangeError(f"раздел обмена недоступен: {mount_point}")

        source_path = Path(source_path)
        if not source_path.exists():
            raise ExchangeError(f"нечего копировать: {source_path} не существует")
        size = _tree_size(source_path) if source_path.is_dir() else source_path.stat().st_size
        free = shutil.disk_usage(directory).free
        if size > free:
            raise InsufficientSpaceError(
                f"недостаточно свободного места в разделе обмена: требуется {size} байт, "
                f"доступно {free} байт"
            )

        destination = directory / (dest_name or source_path.name)
        if source_path.is_dir():
            # `dirs_exist_ok`: повторная запись того же каталога -- обычное
            # дело, настройки правят и кладут заново. Отказ на этом месте
            # заставил бы сначала удалять старое.
            shutil.copytree(source_path, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, destination)

    def read_file(self, mount_point: str, name: str, destination_path: Path) -> None:
        """"Обмен.Чтение": copies a file from the exchange partition out to
        the administrator's workstation."""
        source = Path(mount_point) / name.rstrip("/")
        if source.is_dir():
            shutil.copytree(source, Path(destination_path), dirs_exist_ok=True)
            return
        if not source.is_file():
            raise ExchangeError(f"файл не найден в разделе обмена: {name}")
        shutil.copy2(source, destination_path)

    def delete_file(self, mount_point: str, name: str) -> None:
        """"Обмен.Удаление". Confirmation (UC-6's "Система запрашивает
        подтверждение удаления") is a UI-layer concern; by the time this is
        called the operator has already confirmed."""
        target = Path(mount_point) / name.rstrip("/")
        if target.is_dir():
            shutil.rmtree(target)
            return
        if not target.is_file():
            raise ExchangeError(f"файл не найден в разделе обмена: {name}")
        target.unlink()
