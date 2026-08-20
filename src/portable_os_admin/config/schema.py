"""Install-configuration schema (the course project's "Kickstart-конфигурация").

Field groups mirror "Конфигурация.Состав": disk/partition parameters, LUKS
parameters, the user account, system tasks, remote-access parameters, and the
boot/recovery attempt limit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SUPPORTED_TARGET_OS = ("debian", "redos", "astra")
#: Accepted values for InstallConfig.arch. Both spellings of each architecture
#: are allowed because the two families name them differently (Debian says
#: amd64/arm64, RHEL says x86_64/aarch64) and users copy whichever they saw.
SUPPORTED_ARCH = ("amd64", "x86_64", "arm64", "aarch64")


@dataclass
class DiskLayoutConfig:
    """Partition sizes for the medium.

    `boot_partition_mb` is a *separate, unencrypted* /boot, distinct from the
    ESP. It has to exist: GRUB cannot unlock a LUKS2 container that uses the
    argon2id KDF (its cryptodisk support is PBKDF2-only), so a layout with
    /boot inside the encrypted volume produces a medium that never boots.
    Kernel and initramfs live here, and the initramfs prompts for the LUKS
    passphrase once, the normal way an encrypted-root install behaves.
    """

    esp_partition_mb: int = 512
    boot_partition_mb: int = 1024
    log_partition_mb: int = 512
    exchange_partition_mb: int = 2048
    # the LUKS/system partition consumes the remainder of the device


@dataclass
class LuksConfig:
    cipher: str = "aes-xts-plain64"
    key_size_bits: int = 512
    kdf: str = "argon2id"
    failed_attempts_limit: int = 5


@dataclass
class UserAccountConfig:
    username: str = "user"
    full_name: str = ""
    sudo: bool = False
    shell: str = "/bin/bash"
    min_password_length: int = 8


@dataclass
class RemoteAccessConfig:
    enabled: bool = False
    method: str = "openssh"  # "openssh" | "vpn"
    port: int = 22
    params: dict = field(default_factory=dict)


@dataclass
class InstallConfig:
    name: str
    target_os: str = "debian"
    release: str = ""
    mirror: str = ""
    # CPU architecture of the *target* system. Empty means "same as the machine
    # running the installer", which is the only combination that works without
    # extra tooling: debootstrap/dnf populate a foreign-architecture root only
    # under qemu-user-static, and the chroot steps below would otherwise fail
    # with "Exec format error". Kept explicit in the config so a wrong value is
    # reported by validation instead of surfacing as a cryptic chroot failure.
    arch: str = ""
    disk: DiskLayoutConfig = field(default_factory=DiskLayoutConfig)
    luks: LuksConfig = field(default_factory=LuksConfig)
    account: UserAccountConfig = field(default_factory=UserAccountConfig)
    remote_access: RemoteAccessConfig = field(default_factory=RemoteAccessConfig)
    system_tasks: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    validation_status: str = "unchecked"  # "unchecked" | "valid" | "invalid"
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InstallConfig":
        data = dict(data)
        disk = DiskLayoutConfig(**data.pop("disk", {}))
        luks = LuksConfig(**data.pop("luks", {}))
        account = UserAccountConfig(**data.pop("account", {}))
        remote_access = RemoteAccessConfig(**data.pop("remote_access", {}))
        return cls(disk=disk, luks=luks, account=account, remote_access=remote_access, **data)

    @classmethod
    def create_default(cls, name: str, target_os: str = "debian") -> "InstallConfig":
        release = {"debian": "bookworm", "redos": "8.0", "astra": "orel"}.get(target_os, "")
        return cls(name=name, target_os=target_os, release=release)
