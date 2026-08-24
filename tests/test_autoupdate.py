"""Unattended updates on the finished medium.

The feature exists for a fleet: sticks handed out and not easily collected
back, each patching itself from the configured repository. The risk it carries
is the mirror image of the kiosk's -- not a restriction that quietly does
nothing, but a service that quietly fails on every boot for the life of the
medium, writing to a log nobody thought to read.
"""
from __future__ import annotations

import pytest

from portable_os_admin.config import InstallConfig
from portable_os_admin.config.schema import AutoUpdateConfig
from portable_os_admin.config.validator import BuiltinConfigValidator
from portable_os_admin.install import autoupdate
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.disk import build_media_layout, plan_partitions


def make_config(target_os: str = "debian", *, mirror: str = "http://repo.local/debian", **kwargs):
    config = InstallConfig.create_default("fleet-stick", target_os)
    config.mirror = mirror
    config.auto_update = AutoUpdateConfig(enabled=True, **kwargs)
    return config


@pytest.fixture
def layout():
    config = InstallConfig.create_default("stick", "debian")
    return build_media_layout(
        plan_partitions("/dev/sdz", config.disk), mapper_name="poa-t", mount_root="/mnt/t"
    )


def test_disabled_by_default_and_generates_nothing(layout):
    config = InstallConfig.create_default("stick", "debian")

    assert not autoupdate.is_enabled(config)
    assert autoupdate.config_files(config, get_backend("debian")) == {}
    assert autoupdate.chroot_commands("/mnt/t", config, get_backend("debian")) == []


def test_configuration_written_before_the_feature_existed_still_loads():
    config = InstallConfig.from_dict({"name": "old", "target_os": "debian", "release": "bookworm"})
    assert config.auto_update.enabled is False


def test_script_and_unit_are_written_with_the_right_modes(layout):
    files = get_backend("debian").scripted_files(make_config(), layout)

    assert files[autoupdate.SCRIPT_PATH][1] == 0o755, "скрипт обновления записан без права запуска"
    assert autoupdate.SERVICE_PATH in files


def test_the_service_is_enabled_so_it_runs_every_boot():
    commands = get_backend("debian").chroot_setup_commands("/mnt/t", make_config())
    flat = [" ".join(argv) for argv in commands]

    assert any("enable poa-update.service" in c for c in flat)


def test_the_unit_waits_for_the_network_and_blocks_nothing():
    """It must not delay the boot or the user's login.

    An upgrade ordered before the session would look, to whoever is holding the
    stick, like a medium that takes ten minutes to start.
    """
    unit = autoupdate.service_unit(make_config())

    assert "After=network-online.target" in unit
    assert "Wants=network-online.target" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "Before=" not in unit
    # A default timeout would kill a slow USB upgrade partway through, which is
    # the one failure here that can leave a medium that does not boot.
    assert "TimeoutStartSec=3h" in unit


def test_the_update_yields_the_device_to_the_session():
    """Same slow stick the user is working from."""
    unit = autoupdate.service_unit(make_config())

    assert "IOSchedulingClass=idle" in unit
    assert "Nice=19" in unit


def test_the_script_logs_where_the_administrator_can_read_it():
    """/var/log is a separate unencrypted volume on this project's media.

    That is what makes "did this stick ever take an update?" answerable by
    plugging it into an admin machine, without the user's LUKS passphrase.
    """
    script = autoupdate.update_script(make_config(), get_backend("debian"))

    assert "/var/log/poa-update.log" in script


def test_the_script_gives_up_cleanly_when_there_is_no_network():
    script = autoupdate.update_script(make_config(), get_backend("debian"))

    assert "ip route show default" in script
    assert "сети нет" in script


def test_the_delay_reaches_the_script_in_seconds():
    script = autoupdate.update_script(make_config(delay_minutes=7), get_backend("debian"))
    assert "sleep 420" in script

    immediate = autoupdate.update_script(make_config(delay_minutes=0), get_backend("debian"))
    assert "sleep 0" in immediate


def test_debian_upgrade_answers_no_prompts_and_keeps_local_files():
    """Nobody is at the keyboard, and several of those files are ours.

    fstab, crypttab and the kiosk lockdown were written by this installer; a
    package replacing them would undo the install.
    """
    argv = get_backend("debian").update_upgrade_command(include_kernel=True)

    assert "DEBIAN_FRONTEND=noninteractive" in argv
    assert "Dpkg::Options::=--force-confold" in argv
    assert "-y" in argv


def test_kernel_can_be_excluded_on_both_families():
    debian_holds = get_backend("debian").update_hold_commands(include_kernel=False)
    assert any("apt-mark hold" in line for line in debian_holds)

    redos_argv = get_backend("redos").update_upgrade_command(include_kernel=False)
    # RED OS 8 names its kernel `kernel-lt`, which `kernel*` alone misses.
    assert any("kernel-lt" in part for part in redos_argv)


def test_including_the_kernel_actively_unholds_it():
    """A medium switched back to full upgrades must stop being pinned.

    Without this, ticking the option later leaves the kernel held from the
    earlier install and silently never updated.
    """
    holds = get_backend("debian").update_hold_commands(include_kernel=True)
    assert any("apt-mark unhold" in line for line in holds)


def test_auto_update_without_a_repository_is_refused():
    """The image is gone once the stick is unplugged.

    So a medium with no mirror has nothing to update from, and the service
    would fail on every boot for the life of the medium.
    """
    config = make_config(mirror="")
    errors = BuiltinConfigValidator().validate(config).errors

    assert any("репозитор" in e for e in errors)


def test_a_complete_auto_update_configuration_validates():
    assert BuiltinConfigValidator().validate(make_config()).errors == []


def test_it_works_alongside_the_kiosk(layout):
    """The two features touch the same hooks and must not displace each other."""
    from portable_os_admin.config.schema import KioskConfig

    config = make_config()
    config.kiosk = KioskConfig(enabled=True, allowed_apps=["mate-calc"])
    files = get_backend("debian").scripted_files(config, layout)
    flat = [" ".join(a) for a in get_backend("debian").chroot_setup_commands("/mnt/t", config)]

    assert autoupdate.SCRIPT_PATH in files
    assert "usr/local/sbin/poa-kiosk-apply" in files
    assert any("enable poa-update.service" in c for c in flat)
    assert any("poa-kiosk-apply" in c for c in flat)
    assert BuiltinConfigValidator().validate(config).errors == []
