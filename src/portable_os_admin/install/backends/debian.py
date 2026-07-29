"""Debian/Ubuntu backend, using debootstrap ("Программные интерфейсы-4":
automated install driven by the install configuration)."""
from __future__ import annotations

from ...config.schema import InstallConfig
from .base import InstallBackend


class DebianInstallBackend(InstallBackend):
    name = "debian"
    ssh_package = "openssh-server"
    ssh_service = "ssh"
    sudo_group = "sudo"

    def bootstrap_command(self, mount_root: str, config: InstallConfig) -> list[str]:
        argv = ["debootstrap", config.release, mount_root]
        if config.mirror:
            argv.append(config.mirror)
        return argv

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        if not packages:
            return None
        return self.chroot(mount_root, "apt-get", "install", "-y", *packages)
