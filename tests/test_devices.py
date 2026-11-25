from dataclasses import replace

import pytest

from portable_os_admin.errors import DeviceNotEligibleError
from portable_os_admin.install.devices import BlockDevice, DeviceLister, check_install_eligible

FAKE_LSBLK_OUTPUT = {
    "blockdevices": [
        {
            "name": "sdb",
            "path": "/dev/sdb",
            "size": 16_000_000_000,
            "model": "Cruzer",
            "rm": True,
            "type": "disk",
            "mountpoints": [None],
        },
        {
            "name": "sda",
            "path": "/dev/sda",
            "size": 500_000_000_000,
            "model": "SSD",
            "rm": False,
            "type": "disk",
            "mountpoints": ["/"],
        },
        {
            "name": "sdb1",
            "path": "/dev/sdb1",
            "size": 500_000_000,
            "model": "",
            "rm": True,
            "type": "part",
            "mountpoints": [None],
        },
    ]
}


class FakeDeviceLister(DeviceLister):
    def _lsblk_json(self):
        return FAKE_LSBLK_OUTPUT


def test_list_removable_filters_out_fixed_disks_and_partitions():
    devices = FakeDeviceLister().list_removable()
    assert len(devices) == 1
    assert devices[0].path == "/dev/sdb"
    assert devices[0].removable is True


def test_list_removable_accepts_usb_transport_even_when_rm_flag_is_wrong():
    # Real-world case that blocked live testing: many USB enclosures/sticks
    # report RM=0 even though they're plainly hot-pluggable flash media.
    # lsblk's TRAN=usb is then the only reliable signal that it's removable.
    class UsbFlakyRmLister(DeviceLister):
        def _lsblk_json(self):
            return {
                "blockdevices": [
                    {
                        "name": "sdc",
                        "path": "/dev/sdc",
                        "size": 32_000_000_000,
                        "model": "Flash Drive",
                        "rm": False,
                        "tran": "usb",
                        "type": "disk",
                        "mountpoints": [None],
                    }
                ]
            }

    devices = UsbFlakyRmLister().list_removable()
    assert len(devices) == 1
    assert devices[0].path == "/dev/sdc"


def test_check_install_eligible_passes_for_sufficient_removable_device():
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=16_000_000_000, model="Cruzer", removable=True, mountpoints=(None,)
    )
    check_install_eligible(device, required_bytes=8_000_000_000)  # must not raise


def test_check_install_eligible_rejects_non_removable():
    device = BlockDevice(
        name="sda", path="/dev/sda", size_bytes=500_000_000_000, model="SSD", removable=False, mountpoints=("/",)
    )
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(device, required_bytes=1_000_000)


def test_check_install_eligible_rejects_mounted_device():
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=16_000_000_000, model="Cruzer", removable=True, mountpoints=("/media/x",)
    )
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(device, required_bytes=1_000_000)


def test_check_install_eligible_rejects_too_small():
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=16_000_000_000, model="Cruzer", removable=True, mountpoints=(None,)
    )
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(device, required_bytes=32_000_000_000)


def test_a_non_removable_device_is_refused_unless_the_stand_hatch_is_open(monkeypatch):
    """Проверка стоит между «стереть флешку» и «стереть системный диск».

    Снимать её можно только явной переменной окружения и только на стенде:
    развёртывание на файл-образ через losetup выполняет тот же код, но
    занимает секунды вместо часа.
    """
    from portable_os_admin.install import devices as devices_module

    disk = BlockDevice(
        name="loop0", path="/dev/loop0", size_bytes=32_000_000_000,
        model="", removable=False, mountpoints=(), kind="loop", loop_file=(1, 123),
    )

    monkeypatch.delenv(devices_module.ALLOW_NON_REMOVABLE, raising=False)
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(disk, 1_000_000)

    monkeypatch.setenv(devices_module.ALLOW_NON_REMOVABLE, "1")
    check_install_eligible(disk, 1_000_000)


LOOP_LSBLK_OUTPUT = {
    "blockdevices": [
        {
            "name": "loop1",
            "path": "/dev/loop1",
            "size": 17_179_869_184,
            "model": None,
            "rm": False,
            "type": "loop",
            "mountpoints": [None],
            "children": [
                {
                    "name": "loop1p1",
                    "path": "/dev/loop1p1",
                    "size": 536_870_912,
                    "type": "part",
                    "fstype": "vfat",
                    "label": "POA_ESP",
                    "mountpoints": ["/mnt/poa-esp"],
                },
                {
                    "name": "loop1p2",
                    "path": "/dev/loop1p2",
                    "size": 1_073_741_824,
                    "type": "part",
                    "fstype": "ext4",
                    "label": "POA_BOOT",
                    "mountpoints": [None],
                },
            ],
        }
    ]
}


def test_describe_reads_a_loop_device_with_its_partitions_and_mountpoints():
    """Файл-образ через losetup -- штатная цель стенда, и его надо видеть.

    lsblk называет такое устройство типом "loop", а фильтр принимал только
    "disk". describe возвращал None, мастер строил устройство из текстовых
    полей, и экран подтверждения объявлял «разделов не обнаружено» о носителе
    с пятью разделами. Вместе с разделами терялись точки монтирования -- то
    есть на файл-образе не работала защита от разметки поверх смонтированной
    файловой системы, ради которой describe и существует.

    Найдено живым прогоном 28.08.2026.
    """

    class LoopLister(DeviceLister):
        def _lsblk_json(self, path=None):
            return LOOP_LSBLK_OUTPUT

    device = LoopLister().describe("/dev/loop1")

    assert device is not None
    assert device.path == "/dev/loop1"
    assert device.size_bytes == 17_179_869_184
    assert len(device.partitions) == 2
    assert "/mnt/poa-esp" in device.active_mountpoints

def test_manual_path_preserves_fixed_disk_type(monkeypatch):
    entry = FAKE_LSBLK_OUTPUT["blockdevices"][1]
    monkeypatch.setattr(DeviceLister, "_lsblk_json", lambda self, path: {"blockdevices": [entry]})
    disk = DeviceLister().describe(entry["path"])
    assert disk and not disk.removable
    monkeypatch.setenv("POA_ALLOW_NON_REMOVABLE", "1")
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(disk, 1)


@pytest.mark.parametrize("entries", [[], [FAKE_LSBLK_OUTPUT["blockdevices"][0]] * 2,
                                     [FAKE_LSBLK_OUTPUT["blockdevices"][1]]])
def test_manual_selection_requires_one_exact_disk(monkeypatch, entries):
    monkeypatch.setattr(DeviceLister, "_lsblk_json", lambda self, path: {"blockdevices": entries})
    assert DeviceLister().describe("/dev/sdb") is None


@pytest.mark.parametrize("changes", [{"diskseq": 73}, {"diskseq": 0}, {"mounts_known": False},
                                      {"size_bytes": 1}, {"serial": "another"}, {"mountpoints": ("/",)}])
def test_changed_or_incomplete_device_cannot_be_written(monkeypatch, changes):
    selected = BlockDevice("sdb", "/dev/sdb", 32_000_000_000, "Cruzer", True, (),
                           serial="original", diskseq=72, mounts_known=True)
    monkeypatch.setattr(DeviceLister, "describe", lambda self, path: replace(selected, **changes))
    with pytest.raises(DeviceNotEligibleError):
        DeviceLister().recheck_for_write(selected, selected.path)


def test_only_the_confirmed_loop_backing_file_can_be_used(monkeypatch):
    selected = BlockDevice("loop1", "/dev/loop1", 32_000_000_000, "", False, (), kind="loop",
                           diskseq=30, mounts_known=True, loop_file=(1, 123))
    monkeypatch.setattr(DeviceLister, "describe", lambda self, path: selected)
    monkeypatch.setenv("POA_ALLOW_NON_REMOVABLE", "1")
    assert DeviceLister().recheck_for_write(selected, selected.path) == selected
    check_install_eligible(selected, 1)
    with pytest.raises(DeviceNotEligibleError):
        DeviceLister().recheck_for_write(replace(selected, loop_file=(1, 124)), selected.path)
    with pytest.raises(DeviceNotEligibleError):
        DeviceLister().recheck_for_write(selected, "/dev/loop2")


def test_only_one_writer_holds_a_disk_and_retry_releases_it(tmp_path):
    from portable_os_admin.install.devices import reserve_writer

    disk = BlockDevice("sdb", "/dev/sdb", 1, "", True, (), diskseq=71)
    with reserve_writer(disk, tmp_path):
        with pytest.raises(DeviceNotEligibleError):
            with reserve_writer(disk, tmp_path):
                pytest.fail("две записи получили одну блокировку")
    with reserve_writer(disk, tmp_path):
        pass


def test_kernel_checks_the_connection_on_the_opened_disk(monkeypatch):
    import stat
    import struct
    from types import SimpleNamespace
    from portable_os_admin.install import devices

    disk = BlockDevice("sdb", "/dev/sdb", 1, "", True, (), diskseq=72)
    closed = []

    def open_exclusive(path, flags):
        assert path == disk.path
        assert flags & devices.os.O_EXCL
        assert flags & devices.os.O_ACCMODE == devices.os.O_RDONLY
        return 91

    monkeypatch.setattr(devices.os, "open", open_exclusive)
    monkeypatch.setattr(devices.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFBLK))
    monkeypatch.setattr(devices.os, "close", closed.append)
    monkeypatch.setattr(devices.fcntl, "ioctl", lambda *args: struct.pack("=Q", disk.diskseq + 1))
    with pytest.raises(DeviceNotEligibleError):
        devices.check_kernel_device(disk)
    assert closed == [91]


def test_mounted_system_disk_stops_before_any_write_even_on_the_stand(monkeypatch):
    monkeypatch.setenv("POA_ALLOW_NON_REMOVABLE", "1")
    disk = BlockDevice("sdb", "/dev/sdb", 32_000_000_000, "", True, ("/",), diskseq=41)
    with pytest.raises(DeviceNotEligibleError):
        check_install_eligible(disk, 1)
