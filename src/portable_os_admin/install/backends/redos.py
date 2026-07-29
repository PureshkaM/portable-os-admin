"""RedOS/RHEL-family backend, using dnf --installroot.

RedOS is the RHEL-based distribution named as the install target in the
course-project diagrams (Рисунки 12, 17); this backend targets it (and any
other dnf-based RHEL derivative) by release version rather than a
RedOS-specific package set, since this project was built without access to
RedOS's own repositories to verify exact package names.
"""
from __future__ import annotations

from ...config.schema import InstallConfig
from .base import InstallBackend


class RedOSInstallBackend(InstallBackend):
    name = "redos"
    ssh_package = "openssh-server"
    ssh_service = "sshd"
    sudo_group = "wheel"

    def bootstrap_command(self, mount_root: str, config: InstallConfig) -> list[str]:
        argv = ["dnf", "--installroot", mount_root, "--releasever", config.release, "-y"]
        if config.mirror:
            argv += [f"--repofrompath=poa-repo,{config.mirror}", "--repo=poa-repo"]
        argv += ["install", "@core"]
        return argv

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        if not packages:
            return None
        return ["dnf", "--installroot", mount_root, "-y", "install", *packages]
