"""Common backend contract for "Установка.ОС".

Both shipped backends (Debian/debootstrap and RedOS-RHEL/dnf) only really
differ in how they populate the target filesystem tree
(`bootstrap_command`) and in package-manager/service naming; the rest of
"post-install" chroot configuration (user account, sudo, remote access,
system tasks) is standard shadow-utils/systemd territory shared by both
families, so it lives here once.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ...config.schema import InstallConfig


class InstallBackend(ABC):
    name: str
    ssh_package: str = "openssh-server"
    ssh_service: str = "ssh"
    sudo_group: str = "sudo"

    @abstractmethod
    def bootstrap_command(self, mount_root: str, config: InstallConfig) -> list[str]:
        """argv that populates `mount_root` with a minimal base system."""

    @abstractmethod
    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        """argv (run outside chroot, targeting mount_root) that installs
        `config.packages`, or None if there is nothing to install."""

    def chroot(self, mount_root: str, *argv_inside: str) -> list[str]:
        return ["chroot", mount_root, *argv_inside]

    def chroot_setup_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Implements the OS-account-and-service half of UC-4's "настраивает
        содержимое системного раздела" step. The OS-level login password is
        intentionally locked (`passwd -l`) rather than set here: the LUKS
        passphrase supplied at install time is this project's single
        front-door secret (see docs/ARCHITECTURE.md)."""
        account = config.account
        commands = [
            self.chroot(mount_root, "useradd", "-m", "-s", account.shell, "-c", account.full_name, account.username),
            self.chroot(mount_root, "passwd", "-l", account.username),
        ]
        if account.sudo:
            commands.append(self.chroot(mount_root, "usermod", "-aG", self.sudo_group, account.username))

        pkg_cmd = self.package_install_command(mount_root, config.packages)
        if pkg_cmd is not None:
            commands.append(pkg_cmd)

        if config.remote_access.enabled and config.remote_access.method == "openssh":
            ssh_pkg_cmd = self.package_install_command(mount_root, [self.ssh_package])
            if ssh_pkg_cmd is not None:
                commands.append(ssh_pkg_cmd)
            commands.append(self.chroot(mount_root, "systemctl", "enable", self.ssh_service))

        for task in config.system_tasks:
            commands.append(self.chroot(mount_root, "sh", "-c", task))

        return commands

    def config_files(self, config: InstallConfig) -> dict[str, str]:
        """Relative-path -> content for files written directly under
        mount_root (no chroot needed since it is just a mounted directory
        tree from the host's point of view). Shared across backends because
        fstab/crypttab syntax is not distro-specific."""
        return {
            "etc/hostname": "portable-os\n",
        }
