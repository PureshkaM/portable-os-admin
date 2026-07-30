"""Install-configuration schema (the course project's "Kickstart-конфигурация").

Field groups mirror "Конфигурация.Состав": disk/partition parameters, LUKS
parameters, the user account, system tasks, remote-access parameters, and the
boot/recovery attempt limit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

SUPPORTED_TARGET_OS = ("debian", "redos", "astra")


@dataclass
class DiskLayoutConfig:
    boot_partition_mb: int = 512
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
