"""Building a distribution once and deploying it to many media.

Building a medium takes an hour or two, almost all of it unpacking packages
and running their configuration scripts. Repeating that for each of ten sticks
is not work anyone will do, so it is split in two:

**Creating a distribution** takes a configuration and an installation image and
produces one file: an archive of the finished root filesystem, plus metadata
saying what it is. No medium is involved, nothing is erased, and the work
happens in a directory on the administrator's own disk -- which is also why it
is much faster than doing the same onto USB flash.

**Deploying a distribution** takes that archive and a medium: partitions it,
creates the encryption *afresh*, unpacks the root into it and writes everything
that has to be unique to this one stick. Minutes rather than hours, repeatable.

Why the archive is not encrypted, and why the medium is not simply cloned
-----------------------------------------------------------------------

LUKS encrypts the data with a *master key* and merely wraps that key with the
passphrase in a keyslot. Copying a finished medium byte for byte therefore
copies the master key: every stick made that way shares one, and breaking into
any one of them breaks into all of them -- no matter how different their
passphrases are. `cryptsetup reencrypt` fixes that but re-encrypts the whole
volume, which costs about what the install did.

So the distribution is stored as plain content and the encryption is created
per medium, with a master key generated on the spot. The archive is a build
artefact, not a secret: it holds no keys, no passphrases and no recovery data.
That is not incidental -- `SENSITIVE_PATHS` below is what keeps it true.

What must differ between two media made from one distribution
-------------------------------------------------------------

Everything in `SENSITIVE_PATHS`, and it is worth saying why each one matters:

- the LUKS master key and passphrase -- see above;
- the recovery code and its salt -- one recovery code opening every stick is
  the same failure as one master key;
- the service key for the log and exchange volumes, which lets an
  administrator read a medium without the user's passphrase;
- the media id, by which a medium is recognised later;
- filesystem UUIDs, which `fstab` and `crypttab` address by;
- **SSH host keys** -- shared ones let any stick impersonate any other, and
  they are generated on first boot when absent;
- `machine-id`, which systemd regenerates when the file is present but empty.

The last two are the ones easy to forget, because nothing visibly breaks when
they are shared.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from ..config.schema import InstallConfig
from ..errors import InstallError

#: Version of the archive layout. Read back on deployment so a distribution
#: made by a newer program is refused with an explanation rather than unpacked
#: into something subtly wrong. Version 2 stores the root as squashfs; version
#: 1 distributions (tar) are still read, see `unpack_command`.
FORMAT_VERSION = 2

#: Name of the metadata file inside the distribution directory.
METADATA_NAME = "distribution.json"
#: Name of the root filesystem image inside it.
#:
#: squashfs rather than a tar archive, and the difference is not cosmetic: the
#: image mounts as a filesystem, so deployment is literally "смонтировать и
#: скопировать систему в зашифрованный том", the reference root can be opened
#: and inspected without unpacking four gigabytes, and ownership, permissions
#: and xattrs survive because squashfs stores them itself.
ARCHIVE_NAME = "rootfs.squashfs"
#: Names used by version 1 distributions, still accepted on deployment.
LEGACY_ARCHIVE_NAMES = ("rootfs.tar.zst", "rootfs.tar.gz")

#: Paths removed from the root before it is archived.
#:
#: Every one of them is either a secret that must not travel in a build
#: artefact, or a value that has to be unique per medium. Directories are
#: emptied rather than deleted where the system expects them to exist.
SENSITIVE_PATHS = (
    # Keys and recovery data written per medium.
    "etc/poa/service.key",
    "etc/poa/recovery",
    # Addressed by UUID, which does not exist yet.
    "etc/fstab",
    "etc/crypttab",
    # Regenerated on first boot when absent. Shared host keys would let any
    # medium impersonate any other.
    "etc/ssh/ssh_host_rsa_key",
    "etc/ssh/ssh_host_rsa_key.pub",
    "etc/ssh/ssh_host_ecdsa_key",
    "etc/ssh/ssh_host_ecdsa_key.pub",
    "etc/ssh/ssh_host_ed25519_key",
    "etc/ssh/ssh_host_ed25519_key.pub",
    # The bootloader and initramfs are rebuilt per medium: both embed the
    # UUID of the volume they unlock.
    # boot/efi/EFI отсюда убрано намеренно. В RHEL-семействе загрузчик
    # приходит готовым и подписанным в пакетах shim-x64 и grub2-efi-x64 и
    # ставится копированием файлов; grub2-install там для EFI отключён
    # патчем Red Hat. Вычищая этот каталог, сборка выбрасывала единственный
    # загрузчик, который у неё был, и развёртывание не могло его вернуть.
    # Секрета в этих файлах нет, и для всех носителей из одного дистрибутива
    # они одинаковы.
    "boot/efi/poa",
)

#: Files emptied rather than removed: systemd regenerates machine-id on boot
#: when the file exists and is empty, but treats a *missing* one differently.
EMPTIED_PATHS = (
    "etc/machine-id",
    "var/lib/dbus/machine-id",
)

#: Directories whose contents are dropped -- caches and logs of the build,
#: which are dead weight on every medium made from this distribution.
CLEARED_DIRECTORIES = (
    "var/cache/apt/archives",
    "var/lib/apt/lists",
    "var/log",
    "tmp",
    "var/tmp",
)


@dataclass
class DistributionMetadata:
    """What a distribution says about itself.

    Enough for the deployment step to refuse a mismatch before it erases a
    medium: the wrong architecture, a format it does not understand, or a
    configuration whose kiosk settings do not match what was actually built.
    """

    format_version: int = FORMAT_VERSION
    name: str = ""
    #: Distribution family and release the root was built from.
    target_os: str = ""
    release: str = ""
    #: Architecture of the binaries inside. A medium for another architecture
    #: cannot be made from this.
    arch: str = ""
    #: Profile the root was built as; a console root has no kiosk restrictions
    #: in it and cannot be turned into a kiosk by deployment alone.
    profile: str = "console"
    desktop_environment: str = ""
    session_type: str = ""
    #: The configuration the root was built from, kept whole so the deployment
    #: writes fstab, crypttab and the account exactly as the build intended.
    config: dict = field(default_factory=dict)
    archive: str = ARCHIVE_NAME
    #: SHA-256 of the archive. Checked before a medium is touched, so a
    #: damaged distribution costs an error message rather than an hour and an
    #: unbootable stick. It is an integrity check, not a signature: see
    #: verify_archive() for what that does and does not prove.
    sha256: str = ""
    #: Uncompressed size of the root, for the "will it fit" check before
    #: anything is erased.
    rootfs_bytes: int = 0
    built_at: str = ""
    #: Несёт ли дистрибутив готовый загрузчик. Для RHEL-семейства это
    #: обязательно: там подписанные shim и grub приходят в пакетах, а
    #: grub2-install для EFI отключён, так что развернуть загрузчик из ничего
    #: невозможно. Проверяется до стирания носителя: дистрибутив, собранный
    #: версией программы, которая выбрасывала boot/efi/EFI, иначе выяснился бы
    #: через час, на последнем шаге.
    has_bootloader: bool = False
    #: Emulation note when the root was built for a foreign architecture, so
    #: it travels with the artefact instead of being forgotten.
    built_under_emulation: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DistributionMetadata":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def for_config(
        cls,
        config: InstallConfig,
        *,
        release: str,
        arch: str,
        rootfs_bytes: int,
        emulated: bool,
        archive: str = ARCHIVE_NAME,
    ) -> "DistributionMetadata":
        return cls(
            name=config.name,
            target_os=config.target_os,
            release=release,
            arch=arch,
            profile=config.profile,
            desktop_environment=config.kiosk.desktop_environment if config.kiosk.enabled else "",
            session_type=config.kiosk.session_type if config.kiosk.enabled else "",
            config=config.to_dict(),
            archive=archive,
            rootfs_bytes=rootfs_bytes,
            built_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            built_under_emulation=emulated,
        )


def write_metadata(directory: str, metadata: DistributionMetadata) -> str:
    path = os.path.join(directory, METADATA_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(metadata.to_dict(), handle, ensure_ascii=False, indent=2)
    return path


def read_metadata(directory: str) -> DistributionMetadata:
    """Reads a distribution's metadata, refusing what this program cannot use.

    Checked before the medium is touched: an unreadable or newer distribution
    should cost an error message, not a wiped stick.
    """
    path = os.path.join(directory, METADATA_NAME)
    if not os.path.isfile(path):
        raise InstallError(
            f"это не дистрибутив: в каталоге {directory} нет файла {METADATA_NAME}"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"не удалось прочитать описание дистрибутива: {exc}") from exc

    metadata = DistributionMetadata.from_dict(data)
    if metadata.format_version > FORMAT_VERSION:
        raise InstallError(
            f"дистрибутив создан более новой версией программы "
            f"(формат {metadata.format_version}, эта программа понимает {FORMAT_VERSION})"
        )
    archive = os.path.join(directory, metadata.archive)
    if not os.path.isfile(archive):
        raise InstallError(f"в дистрибутиве нет файла системы: {metadata.archive}")
    return metadata


def archive_path(directory: str, metadata: DistributionMetadata) -> str:
    return os.path.join(directory, metadata.archive)


# ----------------------------------------------------------------------
# Integrity
# ----------------------------------------------------------------------
#: Read in chunks: a distribution is gigabytes, and hashing it must not depend
#: on having that much memory free.
_HASH_CHUNK = 4 * 1024 * 1024


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def checksum_path(archive: str) -> str:
    return f"{archive}.sha256"


def write_checksum(archive: str) -> tuple[str, str]:
    """Writes `<archive>.sha256` beside the archive and returns (path, digest).

    In the format `sha256sum` itself reads, so an administrator can verify a
    distribution with the tool they already know:

        sha256sum -c rootfs.tar.zst.sha256

    rather than having to trust this program to check its own work.
    """
    digest = sha256_of(archive)
    path = checksum_path(archive)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{digest}  {os.path.basename(archive)}\n")
    return path, digest


def verify_archive(directory: str, metadata: DistributionMetadata) -> str:
    """Checks the archive against the recorded digest before anything is erased.

    What this does and does not prove is worth being exact about, because the
    difference matters for the thing this project is for.

    It **does** catch a distribution that was damaged: a copy that ran out of
    space, a truncated transfer, a failing disk, an archive silently corrupted
    in storage. Those are the realistic ways a build artefact goes wrong, and
    each of them would otherwise surface as an unbootable medium an hour later.

    It does **not** prove the distribution is the one the administrator built.
    Whoever can replace the archive can replace the checksum file beside it and
    the digest inside the metadata. Only a signature over the digest, made with
    a key the substituting party does not hold, would answer that -- see
    docs/HANDOVER.md for where that is recorded as the next step.
    """
    archive = archive_path(directory, metadata)
    expected = metadata.sha256
    if not expected:
        raise InstallError(
            "в описании дистрибутива нет контрольной суммы: он собран более старой "
            "версией программы или файл описания изменён вручную"
        )
    actual = sha256_of(archive)
    if actual != expected:
        raise InstallError(
            "контрольная сумма дистрибутива не совпала — файл повреждён или подменён.\n"
            f"  ожидалось: {expected}\n"
            f"  получено:  {actual}\n"
            "Установка не начата, носитель не изменён."
        )
    return actual


def sanitise_commands(root: str) -> list[list[str]]:
    """Commands that strip a built root of everything medium-specific.

    Run in the build step, before the archive is made, so the artefact itself
    never holds a key. Deliberately expressed as plain rm/truncate rather than
    anything clever: this is the code that decides whether ten media made from
    one distribution are ten systems or one, and it should be readable at a
    glance.
    """
    commands: list[list[str]] = []
    for relative in SENSITIVE_PATHS:
        commands.append(["rm", "-rf", f"{root}/{relative}"])
    for relative in EMPTIED_PATHS:
        # Emptied, not removed: systemd regenerates machine-id when the file is
        # present and empty, and treats a missing one as a different situation.
        commands.append(["sh", "-c", f": > {root}/{relative} 2>/dev/null || true"])
    for relative in CLEARED_DIRECTORIES:
        commands.append(
            ["sh", "-c", f"rm -rf {root}/{relative}/* {root}/{relative}/.[!.]* 2>/dev/null || true"]
        )
    return commands


def is_squashfs(archive: str) -> bool:
    """Whether this distribution's root is a squashfs image (format 2)."""
    return archive.endswith(".squashfs")


def archive_command(root: str, destination: str, *, compressor: str) -> list[str]:
    """Packs the root into a single squashfs image.

    `-xattrs` matters more than it looks: capabilities set on binaries live in
    xattrs, and silently dropping them leaves programs that fail in ways that
    have nothing obviously to do with the medium being built. POSIX ACLs ride
    along as xattrs too.

    Ownership is stored numerically, which is what a root filesystem built for
    another machine needs -- the target's /etc/passwd is not this machine's.
    """
    return [
        "mksquashfs",
        root,
        destination,
        "-comp", compressor,
        "-xattrs",
        "-noappend",
        "-no-progress",
        "-quiet",
    ]


def mount_command(archive: str, mountpoint: str) -> list[str]:
    """Mounts the reference root read-only, so it can be copied out of."""
    return ["mount", "-t", "squashfs", "-o", "loop,ro", archive, mountpoint]


def copy_command(mountpoint: str, root: str) -> list[str]:
    """Copies the reference system into the (already unlocked) volume.

    `-a` is `--preserve=all`: ownership, permissions, timestamps, ACLs and
    xattrs. `mountpoint/.` rather than `mountpoint` so the contents land in
    `root` instead of a subdirectory of it.
    """
    return ["cp", "-a", f"{mountpoint}/.", f"{root}/"]


def unpack_command(archive: str, root: str) -> list[str]:
    """Version 1 distributions, kept readable so an existing one still deploys."""
    return [
        "tar",
        "--extract",
        "--file",
        archive,
        "--numeric-owner",
        "--acls",
        "--xattrs",
        "--preserve-permissions",
        "--directory",
        root,
    ]
