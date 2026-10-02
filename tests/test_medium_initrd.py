"""Э2: общий initramfs и второй initrd носителя."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from portable_os_admin.config.schema import InstallConfig
from portable_os_admin.install import medium_initrd
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.backends.constants import RECOVERY_INITRAMFS_DIR
from portable_os_admin.install.installer import recovery_env


def _generic_boot_files(family: str) -> dict:
    config = InstallConfig.create_baseline("stick", family)
    backend = get_backend(family)
    return {**backend.scripted_files(config, medium_initrd.GENERIC, overlay_root=True),
            **backend.recovery_boot_files(medium_initrd.GENERIC, config)}


def test_the_env_path_matches_the_backends():
    assert medium_initrd.RECOVERY_INITRAMFS_DIR == RECOVERY_INITRAMFS_DIR


@pytest.mark.skipif(shutil.which("cpio") is None, reason="нет cpio")
def test_the_archive_is_read_by_cpio_as_is(tmp_path):
    """Ядро и cpio читают один формат: если cpio распаковал, распакует и ядро."""
    archive = medium_initrd.medium_archive("POA_MEDIA_ID=a\n", b"\x01" * 32, "r 1\n",
                                           ("cryptroot/crypttab", "poa-a UUID=x none luks\n"))
    result = subprocess.run(["cpio", "-idm", "--quiet"], input=archive, cwd=tmp_path,
                            capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "poa/env").read_text() == "POA_MEDIA_ID=a\n"
    assert (tmp_path / "poa/recovery_salt.bin").read_bytes() == b"\x01" * 32
    assert (tmp_path / "cryptroot/crypttab").read_text().startswith("poa-a ")


def test_the_archive_is_reproducible():
    one = medium_initrd.medium_archive("A=1\n", b"s", "r\n")
    assert one == medium_initrd.medium_archive("A=1\n", b"s", "r\n")
    assert len(one) % 512 == 0


@pytest.mark.parametrize("family", ["debian", "redos"])
def test_generic_boot_scripts_know_nothing_about_a_medium(family):
    """Общий initramfs не должен нести ни имени тома, ни UUID, ни ID носителя:
    всё -- из /poa/env, и каждый, кто им пользуется, его читает."""
    for path, (text, _mode) in _generic_boot_files(family).items():
        assert "poa-poa" not in text
        uses = any(v in text for v in ("${POA_MAPPER}", "${POA_MEDIA_ID}", "${POA_LUKS_UUID}"))
        if uses:
            assert "/poa/env" in text, path


@pytest.mark.parametrize("family", ["debian", "redos"])
def test_generic_boot_scripts_are_valid_shell(family):
    for path, (text, _mode) in _generic_boot_files(family).items():
        if text.startswith("#!/bin/sh") or text.startswith("#!/bin/bash"):
            shell = "bash" if text.startswith("#!/bin/bash") else "sh"
            checked = subprocess.run([shell, "-n"], input=text, text=True, capture_output=True)
            assert checked.returncode == 0, (path, checked.stderr)


def test_generic_build_does_not_get_a_revocation_script_for_nobody():
    """Отзыв называет конкретный носитель -- в общий корень он не пишется."""
    config = InstallConfig.create_baseline("stick", "debian")
    config.revocation.enabled = True
    config.revocation.url = "https://revoke.example.org"
    files = get_backend("debian").scripted_files(config, medium_initrd.GENERIC, overlay_root=True)
    assert not any("revocation" in path for path in files)


def test_debian_keeps_cryptsetup_and_the_key_script_without_a_crypttab():
    files = _generic_boot_files("debian")
    assert files["etc/cryptsetup-initramfs/conf-hook"][0].endswith("CRYPTSETUP=y\n")
    assert "copy_exec /usr/local/sbin/poa-machine-key" in files["etc/initramfs-tools/hooks/poa-machine"][0]


def test_env_names_the_volume():
    config = InstallConfig.create_baseline("stick", "debian")
    env = recovery_env(config, "ab-cd", "uuid-1", "poa-ab-cd")
    assert "POA_MAPPER=poa-ab-cd\n" in env and "POA_LUKS_UUID=uuid-1\n" in env


def test_grub_loads_the_medium_initrd_when_it_is_there():
    from portable_os_admin.install.disk import MediaLayout

    layout = MediaLayout(
        device="/dev/sdz", esp_partition="/dev/sdz1", boot_partition="/dev/sdz2",
        system_partition="/dev/sdz3", logs_partition="/dev/sdz4", images_partition="/dev/sdz5",
        mapper_name="poa-x", mapper_path="/dev/mapper/poa-x", mount_root="/mnt/x",
        esp_uuid="AAAA-BBBB", boot_uuid="b", luks_uuid="l",
        logs_luks_uuid="g", exchange_luks_uuid="e",
    )
    config = InstallConfig.create_baseline("stick", "debian")
    script = get_backend("debian").grub_config_script(layout, config, "/boot/grub/grub.cfg")
    assert '[ -f /boot/poa-medium.img ] && medium_initrd=" /poa-medium.img"' in script
    assert "initrd ${initrd#/boot}$medium_initrd" in script
