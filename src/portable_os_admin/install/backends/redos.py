"""RedOS/RHEL-family backend, using dnf --installroot.

RedOS is the RHEL-based distribution named as the install target in the
course-project diagrams (Рисунки 12, 17). Verified directly against RedOS's
own repositories (repo1.red-soft.ru, mirrored at repo2/repo3) rather than
generic RHEL assumptions:

- Actively maintained release trees are `7.3` and `8.0` -- `--releasever`
  should be exactly that string (matches the repo URL path and the
  `redos-release-<ver>-*.rpm` filename, no hidden point-release suffix).
- `@core` is a real package group in both releases' comps.xml, so the
  bootstrap command below is valid as written.
- `openssh-server` is a mandatory package in the core/base groups on both
  releases, confirming `ssh_package`/`ssh_service` below.
- Layout under each release is `x86_64/os` (base), `x86_64/updates`,
  `x86_64/extras` -- if a caller supplies `config.mirror` it is passed
  through as-is via `--repofrompath`, so it must already point at one such
  path (e.g. `https://repo1.red-soft.ru/redos/8.0/x86_64/os`); when no
  mirror is given, dnf falls back to whatever repos are already configured
  on the admin host running this command (i.e. a real RedOS machine's own
  `/etc/yum.repos.d/*.repo`), which is the expected case for this project.
- GPG key handling was NOT independently verified (the exact
  `RPM-GPG-KEY-RED-SOFT`-style key name/path is unconfirmed) -- if
  `dry_run=False` fails on a GPG check, inspect the real machine's
  `/etc/yum.repos.d/*.repo` `gpgkey=` line rather than guessing one here.
"""
from __future__ import annotations

from ...config.schema import InstallConfig
from ..disk import MediaLayout
from .base import InstallBackend

# NOT VERIFIED ON REAL HARDWARE: the kernel/bootloader half of this backend has
# never been executed against a real RedOS host. It follows RHEL-family
# conventions and the package names exist in RedOS's own 8.0 repository
# metadata, but grub2-install/dracut behaviour on RedOS specifically is
# unconfirmed -- unlike the Debian backend, which has been run end to end.


class RedOSInstallBackend(InstallBackend):
    name = "redos"
    ssh_package = "openssh-server"
    ssh_service = "sshd"
    sudo_group = "wheel"

    ARCH_NAMES = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    GRUB_TARGETS = {"x86_64": "x86_64-efi", "aarch64": "arm64-efi"}

    #: RHEL-family EFI package suffixes, which do not match the arch name
    EFI_SUFFIX = {"x86_64": "x64", "aarch64": "aa64"}

    def bootstrap_command(self, mount_root: str, config: InstallConfig) -> list[str]:
        argv = ["dnf", "--installroot", mount_root, "--releasever", config.release, "-y"]
        if config.mirror:
            argv += [f"--repofrompath=poa-repo,{config.mirror}", "--repo=poa-repo"]
        argv += ["--forcearch", self.normalize_arch(config.arch), "install", "@core"]
        return argv

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        if not packages:
            return None
        return ["dnf", "--installroot", mount_root, "-y", "install", *packages]

    def boot_packages(self, config: InstallConfig) -> list[str]:
        arch = self.normalize_arch(config.arch)
        suffix = self.EFI_SUFFIX[arch]
        return [
            "kernel",
            f"grub2-efi-{suffix}",
            # the -modules package is what makes grub2-install able to build an
            # EFI image; without it grub2-install fails on RHEL-family systems.
            f"grub2-efi-{suffix}-modules",
            "grub2-tools",
            "grub2-tools-extra",
            f"shim-{suffix}",
            "efibootmgr",
            "dracut",
            "cryptsetup",
            self.syslog_package,
        ]

    def initramfs_command(self, mount_root: str) -> list[str]:
        return self.chroot(mount_root, "dracut", "--force", "--regenerate-all")

    def bootloader_commands(self, layout: MediaLayout, config: InstallConfig) -> list[list[str]]:
        return [
            self.chroot(
                layout.mount_root,
                "grub2-install",
                f"--target={self.grub_target(config.arch)}",
                "--efi-directory=/boot/efi",
                "--boot-directory=/boot",
                "--removable",
                "--no-nvram",
                "--recheck",
            ),
            self.chroot(layout.mount_root, "grub2-mkconfig", "-o", "/boot/grub2/grub.cfg"),
        ]
