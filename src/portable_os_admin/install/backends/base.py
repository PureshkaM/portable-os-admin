"""Common backend contract for "Установка.ОС".

Both shipped families (Debian/debootstrap and RedOS-RHEL/dnf) differ in how
they populate the target filesystem tree (`bootstrap_command`), in package
naming for the kernel/bootloader/initramfs, and in how the initramfs is
regenerated. Everything else -- fstab/crypttab syntax, account setup, sudo,
remote access, autologin -- is standard shadow-utils/systemd territory shared
by both families, so it lives here once.
"""
from __future__ import annotations

import platform
import shutil
from abc import ABC, abstractmethod

from ...config.schema import InstallConfig
from ..disk import MediaLayout

# Where the two unencrypted service partitions end up inside the installed
# system. The log partition is mounted over /var/log on purpose: it is what
# makes the portable system's logs readable by the administrator later without
# the user's LUKS passphrase (see admin/logs.py).
LOG_MOUNTPOINT = "/var/log"
EXCHANGE_MOUNTPOINT = "/mnt/exchange"

#: Key file that opens the log and exchange volumes at boot. It lives on the
#: encrypted root, so it is only readable once the root has been unlocked with
#: the user's passphrase -- the volumes are not protected *from* that user,
#: they are protected from whoever finds the medium.
SERVICE_KEY_DIR = "etc/poa"
SERVICE_KEY_PATH = f"{SERVICE_KEY_DIR}/service.key"

#: Where the recovery module, its salt and its settings are placed inside the
#: installed system. They live on the encrypted root and are copied into the
#: initramfs when it is built, which is the only place they are needed: the
#: recovery flow runs before the root has been unlocked.
RECOVERY_DIR = "etc/poa/recovery"
#: Where the same files end up inside the initramfs.
RECOVERY_INITRAMFS_DIR = "/poa"


def host_arch() -> str:
    return platform.machine()


def maybe_eatmydata(argv: list[str]) -> list[str]:
    """Prefixes `eatmydata` when the host has it, otherwise returns argv as is.

    Bootstrapping is dominated by dpkg's fsync() after every unpacked file,
    which on USB flash turns a ten-minute job into an hour -- measured, not
    guessed: the same debootstrap run took ~55 minutes on a slow port.
    debootstrap does not disable that itself (checked on Debian trixie: it
    leaves /etc/dpkg/dpkg.cfg.d empty), and the dpkg config this project drops
    in only covers the package installs that happen *after* bootstrap, since
    the target's /etc does not exist before it.

    eatmydata is therefore an optional accelerator rather than a requirement --
    a medium interrupted mid-install is unusable regardless of whether the
    writes were synced, and the installer ends with an explicit `sync`.
    """
    if shutil.which("eatmydata"):
        return ["eatmydata", *argv]
    return argv


class InstallBackend(ABC):
    name: str
    ssh_package: str = "openssh-server"
    ssh_service: str = "ssh"
    sudo_group: str = "sudo"
    syslog_package: str = "rsyslog"

    # ------------------------------------------------------------------
    # Architecture
    # ------------------------------------------------------------------
    #: maps `platform.machine()` values onto the names this family uses
    ARCH_NAMES: dict[str, str] = {}
    #: maps this family's arch name onto GRUB's EFI target triple
    GRUB_TARGETS: dict[str, str] = {"amd64": "x86_64-efi", "arm64": "arm64-efi"}

    def normalize_arch(self, arch: str) -> str:
        """Translates a machine/arch string into this family's own naming."""
        key = (arch or host_arch()).strip().lower()
        return self.ARCH_NAMES.get(key, key)

    def grub_target(self, arch: str) -> str:
        return self.GRUB_TARGETS[self.normalize_arch(arch)]

    # ------------------------------------------------------------------
    # Bootstrapping and packages
    # ------------------------------------------------------------------
    @abstractmethod
    def bootstrap_command(
        self, mount_root: str, config: InstallConfig, source_url: str, release: str
    ) -> list[str]:
        """argv that populates `mount_root` with a minimal base system.

        `source_url` is the mounted installation image (`file:///…`) and
        `release` is what that image turned out to contain. Both are parameters
        rather than config fields: the base system always comes from the local
        image, and taking the release from the image keeps distribution version
        numbers out of the configuration entirely."""

    @abstractmethod
    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        """argv (run outside chroot, targeting mount_root) that installs
        `config.packages`, or None if there is nothing to install."""

    #: External utilities every install needs regardless of target family.
    COMMON_TOOLS = (
        "wipefs", "sgdisk", "parted", "partprobe", "udevadm",
        "cryptsetup", "mkfs.ext4", "mkfs.vfat", "blkid",
        "mount", "umount", "chroot",
    )
    #: Family-specific additions (the bootstrapper, mostly).
    EXTRA_TOOLS: tuple[str, ...] = ()

    def required_tools(self) -> list[str]:
        """Everything that must exist before an install may start, so a missing
        utility is reported up front rather than halfway through writing the
        medium."""
        return [*self.COMMON_TOOLS, *self.EXTRA_TOOLS]

    #: The repository this install draws packages from, bound once by the
    #: installer before anything runs. It matters for a family whose package
    #: manager is invoked from *outside* the chroot -- dnf --installroot does
    #: not inherit the target's own configuration, so without this it would
    #: reach for whatever repositories the host has, which on an isolated
    #: network answer nothing and are in any case not the chosen image.
    install_source_url: str = ""
    install_release: str = ""

    def bind_install_source(self, source_url: str, release: str) -> None:
        self.install_source_url = source_url
        self.install_release = release

    #: Where this family keeps the package sources this program writes.
    #: Debian has one file; the RHEL family has a directory of them, so a
    #: distinct name is used there rather than overwriting what the
    #: distribution's own release package installs.
    sources_path: str = "etc/apt/sources.list"

    def install_time_sources(
        self, release: str, source_url: str, components: tuple[str, ...]
    ) -> tuple[str, str] | None:
        """(relative path, content) telling the *target's* package manager
        where to get packages from while it is being installed, or None if the
        family does not need it.

        Distinct from sources_list(), which is what the finished medium keeps.
        This one exists only for the duration of the install and points at the
        image as seen from inside the chroot.
        """
        return None

    def recovery_boot_files(self, layout: MediaLayout, config: InstallConfig) -> dict[str, tuple[str, int | None]]:
        """Relative path -> (content, mode) wiring the recovery module into
        this family's boot environment.

        Empty by default: a family with no integration written simply boots
        without recovery rather than with a half-working one.
        """
        return {}

    def sources_list(self, config: InstallConfig, release: str, image_mountpoint: str = "") -> str | None:
        """Package sources to leave behind in the installed system, or None if
        the family needs nothing written.

        `image_mountpoint` is the mounted installation image, when there is
        one. It lets a backend read settings off the image -- the signing key,
        for instance -- instead of this project writing down values that are
        right for one release and wrong for the next.
        """
        return None

    def temporary_files(self) -> dict[str, tuple[str, int | None]]:
        """Relative path -> (content, mode) for files that exist only while the
        target is being populated, and are deleted before the medium is
        finished. Used for package-manager tuning that must not survive into
        the installed system."""
        return {}

    def package_refresh_command(self, mount_root: str) -> list[str] | None:
        """argv refreshing the target's package index before installing into
        it, or None if the family's installer does not need one."""
        return None

    @abstractmethod
    def boot_packages(self, config: InstallConfig) -> list[str]:
        """Kernel, bootloader, initramfs generator and LUKS tooling.

        debootstrap/`dnf install @core` deliberately produce a root filesystem
        with *no kernel and no bootloader*; without this step the medium can
        never boot, which is exactly the state the first version of this
        installer left it in."""

    @abstractmethod
    def initramfs_command(self, mount_root: str) -> list[str]:
        """argv (chroot) regenerating the initramfs so it embeds the LUKS
        unlocking logic derived from the /etc/crypttab written below."""

    @abstractmethod
    def bootloader_commands(self, layout: MediaLayout, config: InstallConfig) -> list[list[str]]:
        """argv list (chroot) installing the bootloader onto the ESP."""

    def chroot(self, mount_root: str, *argv_inside: str) -> list[str]:
        return ["chroot", mount_root, *argv_inside]

    def systemctl_enable(self, mount_root: str, unit: str) -> list[str]:
        """SYSTEMD_OFFLINE=1 forces systemctl to treat the chroot as an offline
        system tree. Without it, systemctl can notice the host's running
        systemd through the bind-mounted /proc and act on the administrator's
        machine instead of the medium being built."""
        return self.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "enable", unit)

    def chroot_setup_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Implements the OS-account-and-service half of UC-4's "настраивает
        содержимое системного раздела" step. The OS-level login password is
        intentionally locked (`passwd -l`) rather than set here: the LUKS
        passphrase supplied at install time is this project's single
        front-door secret (see docs/ARCHITECTURE.md). Because the account has
        no usable password, `config_files` also drops in a getty autologin
        override -- otherwise unlocking the disk would land the user at a
        login prompt they cannot possibly satisfy."""
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
            commands.append(self.systemctl_enable(mount_root, self.ssh_service))

        commands.append(self.systemctl_enable(mount_root, "rsyslog"))

        for task in config.system_tasks:
            commands.append(self.chroot(mount_root, "sh", "-c", task))

        return commands

    # ------------------------------------------------------------------
    # Files written directly into the mounted target tree
    # ------------------------------------------------------------------
    def config_files(self, config: InstallConfig, layout: MediaLayout) -> dict[str, str]:
        """Relative-path -> content for files written directly under
        mount_root (no chroot needed since it is just a mounted directory tree
        from the host's point of view). Shared across backends because
        fstab/crypttab syntax is not distro-specific.

        Everything is addressed by UUID rather than by /dev/sdaN: the whole
        point of this project is a medium that boots on a machine it was not
        created on, where the kernel's device ordering is different."""
        account = config.account
        files = {
            "etc/hostname": "portable-os\n",
            "etc/hosts": "127.0.0.1\tlocalhost\n127.0.1.1\tportable-os\n",
            "etc/fstab": self._fstab(layout),
            "etc/crypttab": self._crypttab(layout, config),
            "etc/rsyslog.d/10-poa.conf": (
                "# Portable OS admin: mirror everything into a file on the separate\n"
                "# log volume, which the administrator can open with their own key\n"
                "# without the user's LUKS passphrase (see admin/logs.py).\n"
                "*.*\t-/var/log/poa-system.log\n"
            ),
            "etc/systemd/system/getty@tty1.service.d/autologin.conf": (
                "# The account's password is deliberately locked; the LUKS passphrase is\n"
                "# the single front-door secret, so the console logs the user in directly.\n"
                "[Service]\n"
                "ExecStart=\n"
                f"ExecStart=-/sbin/agetty --autologin {account.username} --noclear %I $TERM\n"
            ),
        }
        return files

    def _fstab(self, layout: MediaLayout) -> str:
        rows = [
            ("UUID=" + layout.root_uuid, "/", "ext4", "errors=remount-ro", "0", "1"),
            ("UUID=" + layout.boot_uuid, "/boot", "ext4", "defaults", "0", "2"),
            ("UUID=" + layout.esp_uuid, "/boot/efi", "vfat", "umask=0077", "0", "2"),
            # The service volumes are addressed by their mapper name, not by a
            # filesystem UUID: that filesystem does not exist until crypttab
            # has opened the container, and systemd orders the mount after the
            # cryptsetup unit precisely because the source is /dev/mapper/*.
            #
            # nofail on both: a medium whose log or exchange volume got damaged
            # must still boot, not drop to an emergency shell the user has no
            # password for.
            (layout.logs_mapper_path, LOG_MOUNTPOINT, "ext4", "defaults,nofail", "0", "2"),
            (layout.exchange_mapper_path, EXCHANGE_MOUNTPOINT, "ext4", "defaults,nofail", "0", "2"),
        ]
        lines = ["# generated by portable-os-admin", "# <file system> <mount point> <type> <options> <dump> <pass>"]
        lines += ["\t".join(row) for row in rows]
        return "\n".join(lines) + "\n"

    def _crypttab(self, layout: MediaLayout, config: InstallConfig) -> str:
        """Root asks for a passphrase; the service volumes never do.

        The log and exchange volumes are opened from a key file on the root,
        which by then is already unlocked. That is what lets the user see the
        exchange folder as an ordinary directory without a second password,
        while the medium stays unreadable to anyone who merely holds it.

        nofail keeps a damaged service volume from stopping the boot -- the
        user has no account password to get out of an emergency shell with.
        """
        key = f"/{SERVICE_KEY_PATH}"
        # tries=N is what makes the recovery module reachable: cryptroot asks
        # this many times and then gives up, which is when the recovery hook
        # takes over. Without it the limit configured by the administrator was
        # only ever validated in the interface and never reached the medium.
        tries = max(1, int(getattr(config.luks, "failed_attempts_limit", 3) or 3))
        return (
            "# generated by portable-os-admin\n"
            "# <target name> <source device> <key file> <options>\n"
            f"{layout.mapper_name}\tUUID={layout.luks_uuid}\tnone\tluks,discard,tries={tries}\n"
            f"{layout.logs_mapper_name}\tUUID={layout.logs_luks_uuid}\t{key}\tluks,discard,nofail\n"
            f"{layout.exchange_mapper_name}\tUUID={layout.exchange_luks_uuid}\t{key}\tluks,discard,nofail\n"
        )
