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
