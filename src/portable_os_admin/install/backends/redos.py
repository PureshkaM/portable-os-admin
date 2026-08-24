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
from .. import kiosk as kiosk_mode
from ..disk import MediaLayout
from .base import RECOVERY_DIR, RECOVERY_INITRAMFS_DIR, InstallBackend

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
    # RED OS's image carries sddm and gdm but no lightdm. sddm is the lighter
    # of the two and needs no session bus of its own to autologin.
    display_manager_service = "sddm"

    EXTRA_TOOLS = ("dnf",)

    ARCH_NAMES = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    GRUB_TARGETS = {"x86_64": "x86_64-efi", "aarch64": "arm64-efi"}

    #: RHEL-family EFI package suffixes, which do not match the arch name
    EFI_SUFFIX = {"x86_64": "x64", "aarch64": "aa64"}

    #: Checked name by name against redos-8's own repodata, not recalled. The
    #: distribution's `mate-desktop` comps group is deliberately not used: it
    #: lists 103 packages including mate-terminal, mate-control-center,
    #: mate-system-monitor, caja-open-terminal and gparted -- every one of them
    #: a way out of a kiosk. RED OS's own kiosk mode blocks those tools after
    #: installing them; not installing them is the stronger position.
    KIOSK_PACKAGES = (
        # X and drivers. The distribution collects these in the `base-x` group,
        # named individually here for the same reason as above.
        "xorg-x11-server-Xorg",
        # No xorg-x11-xinit: that is startx, for bringing X up by hand. The
        # display manager starts the server itself.
        "xorg-x11-xauth",
        "xorg-x11-drv-libinput",
        "xorg-x11-drv-evdev",
        "xorg-x11-drv-fbdev",
        "xorg-x11-drv-vesa",
        "xorg-x11-drv-qxl",
        "mesa-dri-drivers",
        "dbus-x11",
        "sddm",
        # MATE, the same desktop RED OS's own kiosk mode is built on.
        "mate-session-manager",
        "mate-panel",
        "marco",
        "caja",
        "mate-settings-daemon",
        "mate-polkit",
        "mate-notification-daemon",
        "mate-themes",
        "mate-icon-theme",
        "dconf",
        "desktop-file-utils",
        "polkit",
        "NetworkManager",
        "network-manager-applet",
        "dejavu-sans-fonts",
    )

    # ------------------------------------------------------------------
    # Unattended updates, run on the medium itself
    # ------------------------------------------------------------------
    def update_refresh_command(self) -> list[str]:
        return ["dnf", "-y", "makecache"]

    def update_upgrade_command(self, *, include_kernel: bool) -> list[str]:
        argv = ["dnf", "-y", "upgrade"]
        if not include_kernel:
            # dnf expresses this as an exclusion on the transaction, so unlike
            # apt there is nothing to pin beforehand and nothing left pinned
            # afterwards. `kernel-lt` is named too: that is what RED OS 8 calls
            # its kernel, and `kernel*` alone would not match it.
            argv.append("--exclude=kernel*,kernel-lt*,grub2*,shim*")
        return argv

    def desktop_packages(self, config: InstallConfig) -> list[str]:
        if not kiosk_mode.is_enabled(config):
            return []
        packages = list(self.KIOSK_PACKAGES)
        if config.kiosk.screen_blank_minutes > 0:
            packages.append("mate-screensaver")
        return packages

    def display_manager_files(self, config: InstallConfig) -> dict[str, str]:
        """SDDM autologin into the MATE session.

        `Relogin=true` matters here in a way it does not on a normal desktop:
        the kiosk account cannot log in by hand -- its password is locked -- so
        without it, logging out would leave a greeter nobody can get past.
        """
        return {
            "etc/sddm.conf.d/50-poa-kiosk.conf": (
                "# generated by portable-os-admin\n"
                "[Autologin]\n"
                f"User={config.account.username}\n"
                "Session=mate\n"
                "Relogin=true\n"
                "\n"
                "[Users]\n"
                "HideUsers=*\n"
            )
        }

    def bootstrap_command(
        self, mount_root: str, config: InstallConfig, source_url: str, release: str
    ) -> list[str]:
        # The mounted image carries repodata/, so dnf reads it as a repository
        # directly. --disablerepo='*' keeps it from reaching for the host's own
        # configured repositories, which on an isolated network do not answer.
        return [
            "dnf",
            "--installroot", mount_root,
            "--releasever", release,
            "-y",
            "--disablerepo=*",
            f"--repofrompath=poa-image,{source_url}",
            "--repo=poa-image",
            "--nogpgcheck",
            "--forcearch", self.normalize_arch(config.arch),
            "install", "@core",
        ]

    def recovery_boot_files(self, layout, config) -> dict:
        """dracut integration for the recovery module.

        A dracut module of three files plus one line of configuration. dracut
        does not pick up modules by their presence alone, so the module is
        listed explicitly in /etc/dracut.conf.d -- leaving that out is the
        usual way this kind of integration silently does nothing.

        The hook runs at pre-mount, by which point systemd-cryptsetup has asked
        for the passphrase as many times as crypttab allows. It hands over only
        if the root mapping is still missing.
        """
        mapper = layout.mapper_name
        module_setup = (
            "#!/bin/bash\n"
            "# generated by portable-os-admin\n"
            "check() { return 0; }\n"
            "depends() { echo crypt; }\n"
            "install() {\n"
            "    # scrypt for the recovery passphrase, plus the small tools the\n"
            "    # module uses; cryptsetup comes with the crypt module.\n"
            "    inst_multiple openssl od stty sha256sum\n"
            "    inst_hook pre-mount 99 \"$moddir/poa-recovery-hook.sh\"\n"
            f"    mkdir -p \"${{initdir}}{RECOVERY_INITRAMFS_DIR}\"\n"
            f"    cp -a /{RECOVERY_DIR}/. \"${{initdir}}{RECOVERY_INITRAMFS_DIR}/\" 2>/dev/null || true\n"
            f"    chmod 0755 \"${{initdir}}{RECOVERY_INITRAMFS_DIR}/recovery_module.sh\" 2>/dev/null || true\n"
            "}\n"
        )
        hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            f"[ -b /dev/mapper/{mapper} ] && exit 0\n"
            f"[ -x {RECOVERY_INITRAMFS_DIR}/recovery_module.sh ] || exit 0\n"
            f"{RECOVERY_INITRAMFS_DIR}/recovery_module.sh </dev/console >/dev/console 2>&1\n"
        )
        return {
            "usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/91poa-recovery/poa-recovery-hook.sh": (hook, 0o755),
            "etc/dracut.conf.d/91-poa-recovery.conf": (
                "# generated by portable-os-admin\n"
                "# dracut does not enable a module just because it is present.\n"
                'add_dracutmodules+=" poa-recovery "\n',
                0o644,
            ),
        }

    #: A file of this program's own, next to -- never on top of -- the ones
    #: the distribution's release package installs during the bootstrap.
    sources_path = "etc/yum.repos.d/poa-mirror.repo"

    def sources_list(self, config: InstallConfig, release: str, image_mountpoint: str = "") -> str | None:
        """An organisation mirror for the installed system, or nothing.

        With no mirror configured this returns None on purpose. `@core`
        installs the distribution's release package, which writes correct
        repository files and the signing key itself -- verified by unpacking
        `redos-release` off a RED OS 8 image: it carries RedOS-Base.repo and
        RedOS-Updates.repo pointing at repo1.red-soft.ru with
        gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT, and ships that key.
        Overwriting any of that would replace something correct with something
        guessed.

        With a mirror, an extra file is written, and its signing settings are
        read off the image rather than named here. That is the whole point of
        this method: the key path used to be a plausible guess in the
        documentation, and a guess that happens to be right today is still a
        guess.
        """
        if not config.mirror:
            return None

        from ..repo_metadata import RepoSigning, discover_signing

        signing = discover_signing(image_mountpoint) if image_mountpoint else RepoSigning(
            source="образ не подключён — установка шла из сети"
        )
        arch = self.normalize_arch(config.arch)
        return (
            "# generated by portable-os-admin\n"
            f"# подписи: {signing.source or 'не определены'}\n"
            "[poa-mirror]\n"
            "name=Repository configured by the administrator\n"
            f"baseurl={config.mirror.rstrip('/')}/{release}/{arch}/os\n"
            "enabled=1\n"
            + signing.as_repo_lines()
        )

    def _repo_arguments(self) -> list[str]:
        """Point dnf at the same repository the bootstrap used.

        dnf runs outside the chroot, so it does not inherit the target's own
        /etc/yum.repos.d. Without these it falls back to the *host's*
        repositories -- which on an isolated network answer nothing, and which
        are not the image the administrator chose in any case.
        """
        if not self.install_source_url:
            return []
        arguments = ["--disablerepo=*", f"--repofrompath=poa-image,{self.install_source_url}", "--repo=poa-image"]
        if self.install_source_url.startswith("file:"):
            # The image is not signed; the trust anchor is the file itself.
            arguments.append("--nogpgcheck")
        if self.install_release:
            arguments += ["--releasever", self.install_release]
        return arguments

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        if not packages:
            return None
        return ["dnf", "--installroot", mount_root, "-y", *self._repo_arguments(), "install", *packages]

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
            # openssl, because the recovery module derives the recovery
            # passphrase with scrypt and nothing else in a boot
            # environment can do that. The initramfs hook copies it in.
            "openssl",
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
