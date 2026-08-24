"""Opening a medium's encrypted log and exchange volumes as the administrator.

Whether cryptsetup actually unlocks a real volume is not something a unit test
can establish -- that belongs to a run on real hardware. What is worth pinning
down here is the part that would fail quietly on someone else's machine: a
medium is found by UUID rather than by device name, the logs are opened
read-only, and a record without a key is refused instead of producing a
confusing error from cryptsetup.
"""
import pytest

from portable_os_admin.admin.service_volumes import (
    ServiceVolume,
    ServiceVolumeError,
    find_partition_by_luks_uuid,
    open_exchange,
    open_logs,
)
from portable_os_admin.container.models import MediaRecord


def _record(**overrides):
    import base64

    data = dict(
        media_id="abc123",
        luks_uuid="00000000-0000-0000-0000-000000000000",
        label="Cruzer",
        config_name="stick1",
        target_os="debian",
        service_key_b64=base64.b64encode(b"k" * 64).decode("ascii"),
        logs_luks_uuid="11111111-1111-1111-1111-111111111111",
        exchange_luks_uuid="22222222-2222-2222-2222-222222222222",
    )
    data.update(overrides)
    return MediaRecord(**data)


def test_a_record_without_a_key_is_refused_with_an_explanation():
    """Media created before the volumes were encrypted have no key. Saying so
    is useful; letting cryptsetup fail on an empty key file is not."""
    with pytest.raises(ServiceVolumeError) as excinfo:
        open_logs(_record(service_key_b64=""))
    assert "ключа" in str(excinfo.value)


def test_a_record_without_volume_uuids_is_refused_before_touching_a_device():
    volume = open_logs(_record(logs_luks_uuid=""))
    with pytest.raises(ServiceVolumeError) as excinfo:
        volume.__enter__()
    assert "UUID" in str(excinfo.value)


def test_logs_are_opened_read_only_and_exchange_is_not():
    """The specification requires that the portable system's user cannot alter
    the logs; an administrator tool that mounted them writable would be the
    obvious way around that."""
    record = _record()
    assert open_logs(record).read_only is True
    assert open_exchange(record).read_only is False


def test_the_two_volumes_of_one_medium_get_distinct_mapper_names():
    record = _record()
    assert open_logs(record).mapper_name != open_exchange(record).mapper_name
    # and they carry the medium id, so two media open at once cannot collide
    assert record.media_id in open_logs(record).mapper_name


def test_a_medium_that_is_not_plugged_in_says_so(monkeypatch):
    import portable_os_admin.admin.service_volumes as module

    class _Result:
        stdout = ""

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: _Result())
    with pytest.raises(ServiceVolumeError) as excinfo:
        find_partition_by_luks_uuid("11111111-1111-1111-1111-111111111111")
    assert "не найден" in str(excinfo.value)


def test_the_volume_is_looked_up_by_uuid_not_by_device_name(monkeypatch):
    """The same medium is /dev/sdb on one machine and /dev/sdc on the next.
    Opening the wrong device here would mean opening someone else's volume."""
    seen = {}

    class _Result:
        stdout = "/dev/sdc4\n"

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return _Result()

    import portable_os_admin.admin.service_volumes as module

    monkeypatch.setattr(module.subprocess, "run", _fake_run)
    assert find_partition_by_luks_uuid("11111111-1111-1111-1111-111111111111") == "/dev/sdc4"
    assert "UUID=11111111-1111-1111-1111-111111111111" in seen["argv"]


def test_key_is_never_kept_as_text_on_the_record():
    """The container stores base64; what the code hands to cryptsetup is bytes."""
    record = _record()
    assert isinstance(record.service_key, bytes)
    assert len(record.service_key) == 64


def test_volume_can_be_closed_twice_without_error():
    """close() runs from __exit__, from the failure path inside __enter__ and
    from the screen being destroyed; all three can overlap."""
    volume = ServiceVolume("1111", b"k" * 32, "poa-test", mountpoint="/tmp/poa-test-never-mounted")
    volume.close()
    volume.close()
