"""Discovers external block devices eligible for installation.

Implements "Установка.ВыборНосителя" / "Интерфейсы оборудования-1,2,3". Reads
`lsblk -J` on Linux; `_lsblk_json` is factored out so tests can substitute
canned JSON without a real block device or even a Linux host.
"""
from __future__ import annotations

import contextlib
import fcntl
import glob
import json
import os
import stat
import struct
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
    #: UUID раздела -- у тома LUKS это UUID его заголовка.
    #:
    #: По нему подключённый носитель узнаётся, не открывая ни одного тома:
    #: те же UUID записаны в контейнере администратора при выдаче. Пароль для
    #: этого не нужен, монтировать ничего не надо, и носитель, который просто
    #: воткнули в порт, называет себя сам.
    uuid: str = ""

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

    kind: str = "disk"
    serial: str = ""
    wwn: str = ""
    # Linux меняет diskseq при повторном подключении, даже если имя /dev прежнее.
    diskseq: int = 0
    mounts_known: bool = False
    loop_file: tuple[int, int] | None = None

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
                    uuid=(child.get("uuid") or "").strip(),
                )
            )
        found.extend(collect_partitions(child))
    return tuple(found)


def busy_holders(path: str) -> "list[str]":
    """Кто ещё держит это устройство открытым -- по одной строке на держателя.

    Пустой список означает «никто», и это единственное состояние, в котором на
    носитель можно писать.

    Зачем. Прогон 04.09.2026 стоил испорченного носителя и часа работы:
    виртуальная машина стенда осталась запущенной и держала файл образа
    открытым на запись, установка отработала все 18 шагов, отчиталась
    «Носитель готов» -- а внутри системного тома не оказалось файловой
    системы вовсе. Две программы писали в одно место, и ни одна об этом не
    знала. Проверка «носитель смонтирован» такое не ловит: смонтировано
    ничего не было.

    Смотрим в двух местах, потому что держать устройство можно двумя
    способами:

    * `/sys/block/<имя>/holders` -- ядро само перечисляет тех, кто построен
      поверх: открытый том dm-crypt, собранный RAID, LVM. Это не «процесс», а
      устройство, и отсюда же берётся понятная строка для сообщения;
    * открытые файловые дескрипторы в `/proc/*/fd` -- так виден процесс,
      который просто открыл устройство или файл-образ и пишет в него. Для
      файла, подключённого через losetup, проверяется и сам файл: с точки
      зрения ядра это разные объекты, а портится один и тот же носитель.

    Ничего не открывается и не изменяется: читаются только имена в /proc и
    /sys, доступные и без прав root.
    """
    found: list[str] = []
    name = os.path.basename(os.path.realpath(path))

    # Держатели самого устройства и каждого его раздела.
    #
    # Раздел -- отдельная запись в /sys, и открытый том dm-crypt висит именно
    # на нём: у `/sys/block/loop0/holders` в этот момент пусто, а у
    # `/sys/block/loop0/loop0p3/holders` -- нет. Проверено 04.09.2026: первая
    # версия этой функции открытый том не замечала вовсе.
    targets = {os.path.realpath(path)}
    holder_dirs = [f"/sys/block/{name}/holders"]
    try:
        for entry in os.listdir(f"/sys/block/{name}"):
            if os.path.isfile(f"/sys/block/{name}/{entry}/partition"):
                holder_dirs.append(f"/sys/block/{name}/{entry}/holders")
                targets.add(f"/dev/{entry}")
    except FileNotFoundError:
        pass
    for holders_dir in holder_dirs:
        try:
            for holder in sorted(os.listdir(holders_dir)):
                description = f"/dev/{holder} (том или устройство поверх носителя)"
                if description not in found:
                    found.append(description)
        except FileNotFoundError:
            continue

    # Два loop-устройства могут ссылаться на один inode, в том числе через разные имена файла.
    backing = f"/sys/block/{name}/loop/backing_file"
    if os.path.exists(backing):
        with open(backing, encoding="utf-8") as handle:
            image_path = handle.read().rstrip("\n")
        image_stat = os.stat(image_path)
        targets.add(image_path)
        for other in glob.glob("/sys/block/loop*/loop/backing_file"):
            if other == backing:
                continue
            try:
                with open(other, encoding="utf-8") as handle:
                    other_path = handle.read().rstrip("\n")
                if os.path.samestat(image_stat, os.stat(other_path)):
                    found.append(f"{other_path} уже подключён к {other.split('/')[3]}")
            except FileNotFoundError:
                continue  # loop успели отключить

    files, block_devices = set(), set()
    for target in targets:
        try:
            info = os.stat(target)
        except FileNotFoundError:
            continue
        if stat.S_ISBLK(info.st_mode):
            block_devices.add(info.st_rdev)
        else:
            files.add((info.st_dev, info.st_ino))

    if not files and not block_devices:
        return found

    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        fd_dir = f"/proc/{entry}/fd"
        try:
            descriptors = os.listdir(fd_dir)
        except (FileNotFoundError, ProcessLookupError):
            continue  # процесс уже завершился
        for descriptor in descriptors:
            try:
                info = os.stat(os.path.join(fd_dir, descriptor))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if (info.st_dev, info.st_ino) not in files and not (
                stat.S_ISBLK(info.st_mode) and info.st_rdev in block_devices
            ):
                continue
            try:
                with open(f"/proc/{entry}/comm", encoding="utf-8") as handle:
                    command = handle.read().strip()
            except OSError:
                command = "?"
            description = f"процесс {entry} ({command})"
            if description not in found:
                found.append(description)
            break

    return found


class DeviceLister:
    LSBLK_COLUMNS = "NAME,PATH,SIZE,MODEL,RM,TRAN,TYPE,FSTYPE,LABEL,UUID,MOUNTPOINTS,SERIAL,WWN"

    def list_removable(self) -> list[BlockDevice]:
        return [d for d in self.list_all() if d.removable]

    def list_all(self) -> list[BlockDevice]:
        return [
            self._device(entry) for entry in self._lsblk_json()["blockdevices"]
            if entry.get("type") in ("disk", "loop")
        ]

    @staticmethod
    def _device(entry: dict) -> BlockDevice:
        name = entry["name"]
        sys_device = f"/sys/class/block/{name}"
        try:
            with open(f"{sys_device}/diskseq", encoding="ascii") as handle:
                diskseq = int(handle.read())
        except (OSError, ValueError):
            diskseq = 0
        loop_file = None
        kind = entry["type"]
        if kind == "loop":
            try:
                with open(f"{sys_device}/loop/backing_file", encoding="utf-8") as handle:
                    backing = os.stat(handle.read().rstrip("\n"))
                if stat.S_ISREG(backing.st_mode):
                    loop_file = (backing.st_dev, backing.st_ino)
            except OSError:
                pass

        def mounts_reported(node: dict) -> bool:
            return isinstance(node.get("mountpoints"), list) and all(
                mounts_reported(child) for child in node.get("children") or []
            )

        removable = kind == "disk" and (
            _truthy(entry.get("rm")) or (entry.get("tran") or "").strip().lower() == "usb"
        )
        return BlockDevice(
            name=name, path=entry["path"], size_bytes=int(entry.get("size") or 0),
            model=(entry.get("model") or "").strip(), removable=removable,
            mountpoints=collect_mountpoints(entry), partitions=collect_partitions(entry),
            kind=kind, diskseq=diskseq, serial=(entry.get("serial") or "").strip(),
            wwn=(entry.get("wwn") or "").strip(), loop_file=loop_file,
            mounts_known=mounts_reported(entry),
        )

    def find(self, path: str) -> BlockDevice | None:
        """Reads a listed removable device by path."""
        for device in self.list_removable():
            if device.path == path:
                return device
        return None

    def describe(self, path: str) -> BlockDevice | None:
        """Describe a whole disk or loop device at its canonical device path."""
        if not path.startswith("/dev/"):
            return None
        try:
            entries = self._lsblk_json(path)["blockdevices"]
            if len(entries) != 1:
                return None
            entry = entries[0]
            if entry.get("type") not in ("disk", "loop") or entry.get("path") != path:
                return None
            return self._device(entry)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            return None

    def recheck_for_write(self, selected: BlockDevice, confirmed_path: str | None) -> BlockDevice:
        """Сверяет подтверждённый снимок с текущим устройством перед записью."""
        if selected.path != confirmed_path:
            raise DeviceNotEligibleError("путь к носителю не подтверждён")
        current = self.describe(selected.path)
        if current is None or current.diskseq <= 0 or not current.mounts_known:
            raise DeviceNotEligibleError(f"недостаточно сведений для записи на {selected.path}")
        if current != selected:
            raise DeviceNotEligibleError(
                f"устройство {selected.path} или его разделы изменились. Выберите носитель заново."
            )
        return current

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


# BLKGETDISKSEQ из linux/fs.h для поддерживаемых x86_64 и arm64.
_BLKGETDISKSEQ = 0x80081280


def check_kernel_device(device: BlockDevice) -> None:
    """Проверяет занятость целого диска ядром, в том числе вне текущей области монтирования."""
    try:
        descriptor = os.open(device.path, os.O_RDONLY | os.O_EXCL | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISBLK(os.fstat(descriptor).st_mode):
                raise DeviceNotEligibleError(f"{device.path} не является блочным устройством")
            number, = struct.unpack("=Q", fcntl.ioctl(descriptor, _BLKGETDISKSEQ, bytes(8)))
            if number != device.diskseq:
                raise DeviceNotEligibleError(f"подключение {device.path} изменилось")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise DeviceNotEligibleError(f"не удалось подтвердить свободный диск {device.path}: {exc}") from exc


@contextlib.contextmanager
def reserve_writer(device: BlockDevice, directory: str = "/run"):
    """Удерживает блокировку двух записей Якоря на одно подключение до завершения уборки."""
    if not isinstance(device.diskseq, int) or device.diskseq <= 0:
        raise DeviceNotEligibleError(f"не удалось определить подключение {device.path}")
    key = f"disk-{device.diskseq}"
    if device.kind == "loop" and device.loop_file:
        filesystem, inode = device.loop_file
        key = f"loop-{filesystem}-{inode}"
    path = os.path.join(directory, f"poa-write-{key}.lock")
    descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeviceNotEligibleError(f"идёт другая запись на {device.path}") from exc
        yield
    finally:
        os.close(descriptor)


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


def check_install_eligible(
    device: BlockDevice, required_bytes: int, *, require_unmounted: bool = True
) -> None:
    """Implements "Установка.Проверка.Носитель" / "Установка.Проверка.ДоИзменения":
    a read-only sanity check performed before any data on the device changes.
    """
    stand_image = (
        device.kind == "loop" and device.loop_file is not None and os.environ.get(ALLOW_NON_REMOVABLE) == "1"
    )
    if not device.removable and not stand_image:
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
