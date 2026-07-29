import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.container import ContainerManager
from portable_os_admin.errors import ConfigValidationError, DeviceNotEligibleError
from portable_os_admin.install import BlockDevice, OSInstaller
from portable_os_admin.session import AdminSession


@pytest.fixture
def session(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    return AdminSession(container)


@pytest.fixture
def big_removable_device():
    return BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True, mountpoints=(None,)
    )


def test_run_rejects_unvalidated_config(session, big_removable_device):
    config = InstallConfig.create_default("stick1", "debian")  # validation_status == "unchecked"
    installer = OSInstaller(session)
    with pytest.raises(ConfigValidationError):
        installer.run(big_removable_device, config, b"user-passphrase", dry_run=True)


def test_run_rejects_ineligible_device(session):
    cfgm = ConfigManager()
    config = InstallConfig.create_default("stick1", "debian")
    cfgm.validate(config)

    tiny_device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=100_000_000, model="Tiny", removable=True, mountpoints=(None,)
    )
    installer = OSInstaller(session)
    with pytest.raises(DeviceNotEligibleError):
        installer.run(tiny_device, config, b"user-passphrase", dry_run=True)


def test_successful_dry_run_produces_media_and_recovery_records(session, big_removable_device):
    cfgm = ConfigManager()
    config = InstallConfig.create_default("stick1", "debian")
    cfgm.validate(config)
    assert config.validation_status == "valid"

    installer = OSInstaller(session)
    result = installer.run(big_removable_device, config, b"user-passphrase-123", dry_run=True)

    assert result.media_record.target_os == "debian"
    assert result.recovery_record.slot == 7
    assert result.recovery_record.status == "available"
    assert all(r.executed is False for r in result.plan.results)

    # records must be reflected in the session's container immediately
    assert session.container.find_media(result.media_record.media_id) is not None
    assert session.container.find_recovery(result.media_record.media_id, 7) is not None


def test_dry_run_plan_includes_partitioning_luks_and_backend_steps(session, big_removable_device):
    cfgm = ConfigManager()
    config = InstallConfig.create_default("stick1", "redos")
    cfgm.validate(config)

    installer = OSInstaller(session)
    result = installer.run(big_removable_device, config, b"user-passphrase-123", dry_run=True)

    argvs = [r.argv for r in result.plan.results]
    assert any(a[0] == "parted" for a in argvs)
    assert any(a[0] == "cryptsetup" and "luksFormat" in a for a in argvs)
    assert any(a[0] == "cryptsetup" and "luksAddKey" in a for a in argvs)
    assert any(a[0] == "dnf" for a in argvs)
    assert any(a[0] == "mkfs.ext4" for a in argvs)
    assert any(a[0] == "mkfs.vfat" for a in argvs)
