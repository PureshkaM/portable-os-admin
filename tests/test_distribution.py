"""Building a distribution once and deploying it to many media.

The property these tests exist to protect is not "the files are written" but
"two media made from one distribution are two systems". That is decided by a
handful of lines -- what gets stripped before the archive is made, and what is
generated per medium -- and getting it wrong produces media that install
perfectly and share a master key.
"""
from __future__ import annotations

import json
import os

import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.config.schema import KioskConfig
from portable_os_admin.container import ContainerManager
from portable_os_admin.errors import ConfigValidationError, InstallError
from portable_os_admin.install import BlockDevice, OSInstaller, distribution
from portable_os_admin.session import AdminSession


@pytest.fixture
def session(tmp_path):
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    return AdminSession(container)


@pytest.fixture
def kiosk_config():
    config = InstallConfig.create_baseline("kiosk-stick", "debian")
    config.release = "trixie"
    config.kiosk = KioskConfig(enabled=True, allowed_apps=["mate-calc"])
    ConfigManager().validate(config)
    return config


@pytest.fixture
def big_device():
    return BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer",
        removable=True, mountpoints=(None,),
    )


# ----------------------------------------------------------------------
# What must never travel in the artefact
# ----------------------------------------------------------------------
def test_nothing_secret_or_identifying_survives_into_the_archive():
    """The archive is a build artefact, not a secret.

    Each of these is either a key or something that has to differ between two
    media. A distribution carrying any of them turns ten sticks into one
    system with ten copies.
    """
    commands = [" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root")]
    joined = "\n".join(commands)

    for path in (
        "etc/poa/service.key",      # opens the log and exchange volumes
        "etc/poa/recovery",         # recovery code salt
        "etc/fstab",                # addresses volumes by UUID
        "etc/crypttab",
        "etc/ssh/ssh_host_rsa_key",  # shared host keys let sticks impersonate each other
        "etc/ssh/ssh_host_ed25519_key",
    ):
        assert path in joined, f"{path} остаётся в эталоне"


def test_machine_id_is_emptied_rather_than_deleted():
    """systemd regenerates it when the file exists and is empty.

    A *missing* machine-id is a different situation and not the one wanted
    here, so this is not a detail that can be simplified away.
    """
    commands = [" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root")]

    machine_id = [c for c in commands if "machine-id" in c]
    assert machine_id, "machine-id не обрабатывается вовсе"
    for command in machine_id:
        assert ": >" in command, "machine-id удаляется, а должен опустошаться"
        assert "rm -rf /mnt/root/etc/machine-id" not in command


def test_build_caches_are_dropped():
    """Dead weight on every medium made from this distribution."""
    joined = "\n".join(" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root"))

    for path in ("var/cache/apt/archives", "var/lib/apt/lists", "var/log"):
        assert path in joined


# ----------------------------------------------------------------------
# Integrity
# ----------------------------------------------------------------------
def test_checksum_file_is_readable_by_sha256sum(tmp_path):
    """So an administrator can verify a distribution without trusting us."""
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents" * 100)

    path, digest = distribution.write_checksum(str(archive))

    assert path.endswith(".sha256")
    line = open(path, encoding="utf-8").read().strip()
    # `<digest>  <basename>` is what the tool expects.
    assert line == f"{digest}  rootfs.tar.zst"
    assert len(digest) == 64


def test_a_damaged_distribution_is_refused_before_anything_is_erased(tmp_path):
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"original contents")
    metadata = distribution.DistributionMetadata(
        archive="rootfs.tar.zst", sha256=distribution.sha256_of(str(archive))
    )
    distribution.write_metadata(str(tmp_path), metadata)

    # Verifies while intact...
    assert distribution.verify_archive(str(tmp_path), metadata)

    # ...and refuses once damaged.
    archive.write_bytes(b"corrupted contents")
    with pytest.raises(InstallError) as excinfo:
        distribution.verify_archive(str(tmp_path), metadata)
    assert "не совпала" in str(excinfo.value)
    assert "носитель не изменён" in str(excinfo.value).lower()


def test_a_distribution_without_a_checksum_is_refused(tmp_path):
    """Older artefacts, or a metadata file edited by hand."""
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents")
    metadata = distribution.DistributionMetadata(archive="rootfs.tar.zst", sha256="")

    with pytest.raises(InstallError) as excinfo:
        distribution.verify_archive(str(tmp_path), metadata)
    assert "контрольной суммы" in str(excinfo.value)


def test_a_newer_format_is_refused_with_an_explanation(tmp_path):
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents")
    data = distribution.DistributionMetadata(archive="rootfs.tar.zst").to_dict()
    data["format_version"] = distribution.FORMAT_VERSION + 5
    (tmp_path / distribution.METADATA_NAME).write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(InstallError) as excinfo:
        distribution.read_metadata(str(tmp_path))
    assert "более новой версией" in str(excinfo.value)


def test_a_directory_that_is_not_a_distribution_says_so(tmp_path):
    with pytest.raises(InstallError) as excinfo:
        distribution.read_metadata(str(tmp_path))
    assert "не дистрибутив" in str(excinfo.value)


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------
def test_building_touches_no_medium(session, kiosk_config, tmp_path):
    """The long half of the work is also the safe half."""
    installer = OSInstaller(session)

    result = installer.build_distribution(
        kiosk_config, str(tmp_path / "dist"), image_path="", dry_run=True
    )

    assert installer.medium_modified is False
    # The *command* being run, not the whole line: "cryptsetup" also appears
    # as the name of a package being installed, which is not the same thing at
    # all and would make this test fail for the wrong reason.
    programs = {os.path.basename(r.argv[0]) for r in result.plan.results if r.argv}
    for destructive in ("wipefs", "sgdisk", "parted", "cryptsetup", "mkfs.ext4", "mkfs.vfat"):
        assert destructive not in programs, f"сборка выполняет {destructive}"


def test_building_refuses_an_unvalidated_configuration(session, tmp_path):
    config = InstallConfig.create_baseline("stick", "debian")  # validation_status unchecked

    with pytest.raises(ConfigValidationError):
        OSInstaller(session).build_distribution(config, str(tmp_path), dry_run=True)


def test_the_metadata_describes_what_was_built(session, kiosk_config, tmp_path):
    """Read back on deployment to refuse a mismatch before erasing anything."""
    kiosk_config.arch = "amd64"
    kiosk_config.kiosk.desktop_environment = "kde"
    ConfigManager().validate(kiosk_config)

    result = OSInstaller(session).build_distribution(
        kiosk_config, str(tmp_path / "dist"), image_path="", dry_run=True
    )

    assert result.metadata.profile == "kiosk"
    assert result.metadata.desktop_environment == "kde"
    assert result.metadata.arch == "amd64"
    assert result.metadata.target_os == "debian"
    # The configuration travels whole, so deployment writes fstab and the
    # account exactly as the build intended.
    assert result.metadata.config["kiosk"]["allowed_apps"] == ["mate-calc"]


def test_the_archive_is_packed_with_ownership_and_attributes_preserved():
    """Unpacked on a machine whose /etc/passwd is not the target's.

    Without --numeric-owner tar maps uid 1000 through the *host's* names, and
    the medium's files end up owned by whoever is uid 1000 there.
    """
    argv = distribution.archive_command("/mnt/root", "/out/rootfs.tar.zst", compressor="zstd -T0")

    assert "--numeric-owner" in argv
    assert "--xattrs" in argv
    assert "--acls" in argv
    # One filesystem only: /proc and /dev are bind-mounted during the build.
    assert "--one-file-system" in argv

    unpack = distribution.unpack_command("/d/rootfs.tar.zst", "/mnt/target")
    assert "--numeric-owner" in unpack


# ----------------------------------------------------------------------
# Deploying
# ----------------------------------------------------------------------
def test_deploying_checks_the_distribution_before_erasing(session, big_device, tmp_path):
    """A missing distribution must not cost a medium."""
    with pytest.raises(InstallError):
        OSInstaller(session).deploy_distribution(
            big_device, str(tmp_path / "nothing-here"), b"passphrase", dry_run=True
        )


def test_deploying_generates_the_encryption_afresh(session, kiosk_config, big_device, tmp_path):
    """The property the whole split rests on.

    A byte-for-byte copy of a finished medium shares its LUKS master key, so
    breaking one breaks all. Deployment therefore runs luksFormat itself,
    per medium.
    """
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    result = OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
    )

    commands = [" ".join(r.argv) for r in result.plan.results]
    assert any("luksFormat" in c for c in commands), "шифрование не создаётся заново"
    assert any("tar" in c and "--extract" in c for c in commands), "эталон не распаковывается"
    # And the medium gets its own identity.
    assert result.media_record.media_id
    assert result.recovery_record.code


def test_two_deployments_get_different_identities(session, kiosk_config, big_device, tmp_path):
    """Same distribution, two media -- and nothing shared between them."""
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    installer = OSInstaller(session)
    first = installer.deploy_distribution(
        big_device, str(directory), b"passphrase-one", dry_run=True, confirm_device="/dev/sdb"
    )
    second = installer.deploy_distribution(
        big_device, str(directory), b"passphrase-two", dry_run=True, confirm_device="/dev/sdb"
    )

    assert first.media_record.media_id != second.media_record.media_id
    assert first.recovery_record.code != second.recovery_record.code
    assert first.media_record.service_key_b64 != second.media_record.service_key_b64


# ----------------------------------------------------------------------
# RED OS backend, first executed 25.08.2026
# ----------------------------------------------------------------------
def test_dnf_is_not_given_two_flags_it_refuses_together():
    """Regression: the RED OS backend died on its very first real command.

        argument --repo/--repoid: not allowed with argument --disablerepo

    `--repo` already enables what it names and disables the rest, so the pair
    was redundant as well as invalid. It appeared in two places -- the
    bootstrap and the arguments used for every package install afterwards --
    and neither had ever been run before, which is exactly why a backend
    "written to the family's conventions" is not the same as a working one.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    config = InstallConfig.create_default("stick", "redos")

    bootstrap = backend.bootstrap_command("/mnt/root", config, "file:///mnt/image", "8.0")
    assert "--repo=poa-image" in bootstrap
    assert not any(a.startswith("--disablerepo") for a in bootstrap)

    backend.bind_install_source("file:///mnt/image", "8.0")
    install = backend.package_install_command("/mnt/root", ["mate-calc"])
    assert "--repo=poa-image" in install
    assert not any(a.startswith("--disablerepo") for a in install)


def test_redos_does_not_ask_dnf_to_resolve_a_group():
    """Regression: resolving `@core` segfaults dnf on the build host.

    Reproduced precisely before changing anything: a single package from the
    same repository installs fine, `repoquery` works, and removing
    `--forcearch` changes nothing -- only the group does it, and dnf dies
    right after reading the metadata. Nothing in this project can fix a crash
    inside dnf's group handling, so the group is not used: the base system is
    named package by package and dependencies pull in the rest.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    config = InstallConfig.create_default("stick", "redos")
    argv = backend.bootstrap_command("/mnt/root", config, "file:///mnt/image", "8.0")

    assert not any(a.startswith("@") for a in argv), "группа comps снова передаётся dnf"
    # The leaves that pull a working base system in behind them.
    for package in ("basesystem", "systemd", "bash", "coreutils", "redos-release"):
        assert package in argv
