"""Log viewer (UC-5 "Просмотр журналов операционной системы").

The log partition (POA_LOGS) is a plain ext4 filesystem outside the LUKS
container (see install/disk.py) -- it holds plain-text `*.log` files written
by the portable OS at runtime, so the administrator can read it without ever
needing the user's LUKS passphrase. Mounted read-only here
("Пользователь переносной системы не должен иметь возможность изменять
журналы... штатными средствами" is enforced OS-side by this being a
read-only bind for the admin tool, and by not exposing the same access to
the portable OS as writable admin channel).
"""
from __future__ import annotations

from pathlib import Path

from ..errors import LogsCorruptedError, LogsUnavailableError
from .media_lookup import find_media_by_boot_metadata  # noqa: F401  (re-exported for callers)
from ..install.command_runner import CommandPlan, CommandRunner


class LogViewer:
    def __init__(self, runner: CommandRunner | None = None):
        self.runner = runner or CommandRunner()

    def mount(self, partition: str, mount_point: str, plan: CommandPlan, *, dry_run: bool = True, confirm_device: str | None = None) -> None:
        self.runner.run(["mkdir", "-p", mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)
        self.runner.run(["mount", "-o", "ro", partition, mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)

    def unmount(self, mount_point: str, plan: CommandPlan, *, dry_run: bool = True, confirm_device: str | None = None) -> None:
        self.runner.run(["umount", mount_point], plan, dry_run=dry_run, confirm_device=confirm_device)

    def read_logs(self, mount_point: str, filter_substring: str | None = None) -> list[str]:
        """"Журналы.Чтение" / "Журналы.Фильтрация"."""
        log_dir = Path(mount_point)
        if not log_dir.is_dir():
            raise LogsUnavailableError(f"раздел логов недоступен: {mount_point}")

        log_files = sorted(log_dir.glob("*.log"))
        if not log_files:
            raise LogsCorruptedError("журналы отсутствуют или повреждены")

        lines: list[str] = []
        for path in log_files:
            try:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError as exc:
                raise LogsCorruptedError(f"не удалось прочитать {path.name}: {exc}") from exc

        if filter_substring:
            lines = [line for line in lines if filter_substring in line]
        return lines
