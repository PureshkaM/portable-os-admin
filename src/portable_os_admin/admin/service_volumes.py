"""Opening a medium's encrypted log and exchange volumes on the admin machine.

Both volumes are LUKS containers with two keyslots (see install/installer.py):
one holds a key file that lives on the medium's encrypted root, which is how
the portable system opens them at boot; the other holds the administrator's
key, which exists only inside the administrator container.

That second key is what this module uses. It means the administrator reads
logs and exchanges files **without the user's LUKS passphrase**, and it means
a medium that is merely found or stolen yields nothing -- neither key is on it
in a form anyone else can reach.

It also means these volumes can no longer be mounted by hand, which is the
point: they are opened by this program or not at all.
"""
from __future__ import annotations

import subprocess
import tempfile

from ..errors import PortableOSAdminError
from ..install.command_runner import CommandPlan, CommandRunner
from ..install.luks import LuksManager
from ..install.tooling import resolve_argv


class ServiceVolumeError(PortableOSAdminError):
    """The volume could not be found, opened or mounted."""


def find_partition_by_luks_uuid(luks_uuid: str) -> str:
    """The partition holding the LUKS container with this UUID.

    Looked up by UUID rather than by device path on purpose: the same medium
    is /dev/sdb on one machine and /dev/sdc on the next, and a wrong guess here
    would mean opening someone else's volume.
    """
    if not luks_uuid:
        raise ServiceVolumeError("для этого носителя не записан UUID тома — он создан старой версией программы")
    proc = subprocess.run(
        resolve_argv(["blkid", "-t", f"UUID={luks_uuid}", "-o", "device"]),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    devices = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not devices:
        raise ServiceVolumeError(
            f"носитель с томом {luks_uuid} не найден — подключён ли он к этой машине?"
        )
    return devices[0]


class ServiceVolume:
    """Opens and mounts one service volume for the duration of a `with` block.

    Everything is released on the way out, including on failure: a volume left
    open would keep the medium busy and the next attempt would collide with a
    stale mapper name.
    """

    def __init__(
        self,
        luks_uuid: str,
        key: bytes,
        mapper_name: str,
        *,
        read_only: bool = False,
        mountpoint: str | None = None,
        runner: CommandRunner | None = None,
    ):
        if not key:
            raise ServiceVolumeError(
                "в контейнере администратора нет ключа для этого носителя, "
                "поэтому открыть его том нечем"
            )
        self.luks_uuid = luks_uuid
        self.key = key
        self.mapper_name = mapper_name
        self.read_only = read_only
        self.mountpoint = mountpoint or tempfile.mkdtemp(prefix="poa-volume-")
        self._own_mountpoint = mountpoint is None
        self.runner = runner or CommandRunner()
        self.luks = LuksManager(self.runner)
        self._opened = False
        self._mounted = False

    def __enter__(self) -> "ServiceVolume":
        partition = find_partition_by_luks_uuid(self.luks_uuid)
        plan = CommandPlan(device=partition)
        try:
            self.luks.open(
                partition, self.mapper_name, self.key, dry_run=False, confirm_device=partition, plan=plan
            )
        except Exception as exc:  # noqa: BLE001 - a wrong key is the common case
            raise ServiceVolumeError(
                f"не удалось открыть том на {partition}: {exc}"
            ) from exc
        self._opened = True

        options = "ro" if self.read_only else "rw"
        try:
            self.runner.run(["mkdir", "-p", self.mountpoint], plan, dry_run=False, confirm_device=partition)
            self.runner.run(
                ["mount", "-o", options, LuksManager.mapper_path(self.mapper_name), self.mountpoint],
                plan,
                dry_run=False,
                confirm_device=partition,
            )
        except Exception as exc:  # noqa: BLE001
            self.close()
            raise ServiceVolumeError(f"не удалось смонтировать том: {exc}") from exc
        self._mounted = True
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        plan = CommandPlan(device=LuksManager.mapper_path(self.mapper_name))
        if self._mounted:
            self._try(["umount", self.mountpoint], plan)
            self._mounted = False
        if self._opened:
            self._try(["cryptsetup", "luksClose", self.mapper_name], plan)
            self._opened = False
        if self._own_mountpoint:
            self._try(["rmdir", self.mountpoint], plan)

    def _try(self, argv: list[str], plan: CommandPlan) -> None:
        # Release is best effort for the same reason the installer's teardown
        # is: reporting "umount failed" instead of the original error would
        # hide what actually went wrong.
        try:
            self.runner.run(argv, plan, dry_run=False, confirm_device=plan.device)
        except Exception:  # noqa: BLE001
            pass


def open_logs(record, **kwargs) -> ServiceVolume:
    """The log volume of `record`, read-only.

    Read-only is not a formality: the specification requires that the portable
    system's user cannot alter the logs, and mounting them writable here would
    make the administrator's own tool the way to do it.
    """
    return ServiceVolume(
        record.logs_luks_uuid,
        record.service_key,
        f"poa-{record.media_id}-logs-admin",
        read_only=True,
        **kwargs,
    )


def open_exchange(record, **kwargs) -> ServiceVolume:
    """The exchange volume of `record`, writable -- files go both ways."""
    return ServiceVolume(
        record.exchange_luks_uuid,
        record.service_key,
        f"poa-{record.media_id}-exch-admin",
        read_only=False,
        **kwargs,
    )
