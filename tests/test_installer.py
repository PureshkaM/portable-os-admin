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


def test_run_rejects_ineligible_device(session, make_config):
    cfgm = ConfigManager()
    config = make_config()
    cfgm.validate(config)

    tiny_device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=100_000_000, model="Tiny", removable=True, mountpoints=(None,)
    )
    installer = OSInstaller(session)
    with pytest.raises(DeviceNotEligibleError):
        installer.run(tiny_device, config, b"user-passphrase", dry_run=True)


def test_successful_dry_run_produces_media_and_recovery_records(session, big_removable_device, make_config):
    cfgm = ConfigManager()
    config = make_config()
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


def _plan_argvs(session, make_config, target_os="debian"):
    cfgm = ConfigManager()
    config = make_config("stick1", target_os)
    cfgm.validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True,
        mountpoints=("/media/user/flash",),
    )
    result = OSInstaller(session).run(device, config, b"user-passphrase-123", dry_run=True)
    return [r.argv for r in result.plan.results], result


def test_target_system_is_actually_written_onto_the_medium(session, make_config):
    """Regression test for the defect that made this project useless in practice.

    The original pipeline formatted the LUKS container and then bootstrapped
    the OS into `/mnt/poa-install-<id>` without ever opening that container or
    mounting anything there -- so the base system landed in a directory on the
    administrator's own machine and the medium stayed empty. Every assertion
    here is a link in the chain that was missing.
    """
    argvs, _ = _plan_argvs(session, make_config)
    flat = [" ".join(a) for a in argvs]

    open_idx = next(i for i, a in enumerate(argvs) if a[0] == "cryptsetup" and "luksOpen" in a)
    mapper = argvs[open_idx][3]
    mkfs_idx = next(i for i, a in enumerate(argvs) if a[0] == "mkfs.ext4" and a[-1].endswith(mapper))
    mount_idx = next(i for i, a in enumerate(argvs) if a[0] == "mount" and a[1].endswith(mapper))
    # "debootstrap" is not necessarily argv[0] -- it may be wrapped in eatmydata
    bootstrap_idx = next(i for i, a in enumerate(argvs) if "debootstrap" in a)

    # unlock -> make a filesystem -> mount it -> only then install into it
    assert open_idx < mkfs_idx < mount_idx < bootstrap_idx
    mount_root = argvs[mount_idx][2]
    assert mount_root in argvs[bootstrap_idx], "bootstrap does not target the mounted medium"

    # the medium is unmounted first: a stick is automounted by the desktop the
    # moment it is plugged in, and partitioning under a live filesystem corrupts it
    assert argvs[0] == ["umount", "/media/user/flash"]

    # a bootable result needs a kernel, an initramfs that can unlock the root,
    # and a bootloader -- debootstrap/dnf install none of the three
    assert any("linux-image" in f for f in flat)
    assert any("cryptsetup-initramfs" in f for f in flat)
    assert any("update-initramfs" in f for f in flat)
    assert any("grub-install" in f for f in flat)

    # and it needs to know what to mount at boot
    assert f"<write-file> {mount_root}/etc/fstab" in flat
    assert f"<write-file> {mount_root}/etc/crypttab" in flat


def test_everything_opened_by_the_install_is_released_afterwards(session, make_config):
    """A medium left mounted cannot be safely unplugged, and a LUKS mapping left
    open collides with the next install."""
    argvs, _ = _plan_argvs(session, make_config)
    mount_targets = [a[-1] for a in argvs if a[0] == "mount"]
    umounted = [a[-1] for a in argvs if a[0] == "umount"]

    for target in mount_targets:
        assert target in umounted, f"{target} mounted but never unmounted"
    assert any(a[0] == "cryptsetup" and "luksClose" in a for a in argvs)


def test_fstab_and_crypttab_reference_uuids_not_device_names(session):
    """The whole point is a medium that boots on a machine it was not built on,
    where /dev/sdX ordering is different."""
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick1", "debian")
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    layout.root_uuid = "11111111-1111-1111-1111-111111111111"
    layout.boot_uuid = "22222222-2222-2222-2222-222222222222"
    layout.esp_uuid = "3333-4444"
    layout.logs_uuid = "55555555-5555-5555-5555-555555555555"
    layout.exchange_uuid = "66666666-6666-6666-6666-666666666666"
    layout.luks_uuid = "77777777-7777-7777-7777-777777777777"

    files = get_backend("debian").config_files(config, layout)
    fstab = files["etc/fstab"]
    assert "/dev/sd" not in fstab
    assert f"UUID={layout.root_uuid}\t/\t" in fstab
    assert "/boot/efi" in fstab
    assert f"UUID={layout.luks_uuid}" in files["etc/crypttab"]
    assert "poa-abc" in files["etc/crypttab"]


def test_locked_account_still_gets_a_way_to_log_in(session):
    """The account password is deliberately locked (the LUKS passphrase is the
    only front-door secret), so without autologin the booted medium would show
    a login prompt nobody can satisfy."""
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick1", "debian")
    config.account.username = "portable"
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    files = get_backend("debian").config_files(config, layout)

    autologin = files["etc/systemd/system/getty@tty1.service.d/autologin.conf"]
    assert "--autologin portable" in autologin


def test_foreign_architecture_is_refused_with_an_explanation(session, make_config):
    import platform

    from portable_os_admin.errors import InstallError

    config = make_config()
    config.arch = "amd64" if platform.machine() in ("aarch64", "arm64") else "arm64"
    ConfigManager().validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True, mountpoints=()
    )
    with pytest.raises(InstallError, match="qemu-user-static"):
        OSInstaller(session).run(device, config, b"user-passphrase-123", dry_run=True)
