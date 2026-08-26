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
import shlex
import shutil
from abc import ABC, abstractmethod

from ...config.schema import InstallConfig
from .. import autoupdate
from .. import kiosk as kiosk_mode
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
    #: Non-empty means this family supports the kiosk profile at all. The unit
    #: actually enabled depends on the desktop as well, so ask
    #: display_manager_for() rather than reading this; a kiosk configuration
    #: for a family whose value is empty is refused rather than half-built.
    display_manager_service: str = ""
    #: Display manager used for a KDE session. SDDM is KDE's own, ships in both
    #: families' images, and autologins without a session bus of its own.
    kde_display_manager: str = "sddm"

    def display_manager_for(self, config: InstallConfig) -> str:
        if not self.display_manager_service:
            return ""
        if kiosk_mode.desktop_of(config) == kiosk_mode.DESKTOP_KDE:
            return self.kde_display_manager
        return self.display_manager_service

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

    def apply_package_overrides(
        self, config: InstallConfig, chosen: str, packages: list[str] | tuple[str, ...]
    ) -> list[str]:
        """Подставляет заданный администратором набор и вычитает исключения.

        Одна точка на все наборы: иначе `exclude` работал бы там, где о нём
        вспомнили, и молча не работал в остальных местах — а «пакет, который я
        убрал, всё равно установился» это худший вид дефекта, потому что он не
        выглядит дефектом.
        """
        override = getattr(config.package_sets, chosen, None) or None
        result = list(override if override is not None else packages)
        excluded = set(config.package_sets.exclude)
        return [name for name in result if name not in excluded]

    def service_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Включение, выключение и маскировка служб из конфигурации.

        Маскировка идёт последней и сильнее прочего: замаскированную службу не
        поднимет и зависимость. Порядок важен, если одна и та же служба попала
        в два списка — тогда выигрывает более строгое.
        """
        commands: list[list[str]] = []
        for unit in config.services.enable:
            commands.append(self.systemctl_enable(mount_root, unit))
        for unit in config.services.disable:
            commands.append(
                self.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "disable", unit)
            )
        for unit in config.services.mask:
            commands.append(
                self.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "mask", unit)
            )
        return commands

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

    #: Base-system packages read from the image itself, when the family's
    #: images carry such a list. Preferred over anything written into this
    #: program: a list of package names here would be right for one version of
    #: one distribution and quietly wrong for the next.
    image_core_packages: tuple[str, ...] = ()

    #: Architecture the medium is being built for, in this family's spelling.
    #: Needed by a package manager that runs on the *host* and installs into a
    #: root of another architecture.
    install_arch: str = ""

    def bind_install_source(
        self,
        source_url: str,
        release: str,
        core_packages: tuple[str, ...] = (),
        arch: str = "",
    ) -> None:
        self.install_source_url = source_url
        self.install_release = release
        self.image_core_packages = core_packages
        self.install_arch = arch

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

    # ------------------------------------------------------------------
    # Unattended updates on the finished medium
    # ------------------------------------------------------------------
    # These three run *on the medium*, long after this program is done, so they
    # carry no chroot prefix and no reference to the install-time repository.
    def update_refresh_command(self) -> list[str]:
        """Refreshes the package index on the running portable system."""
        raise NotImplementedError

    def update_upgrade_command(self, *, include_kernel: bool) -> list[str]:
        """Installs available upgrades, optionally leaving the kernel alone."""
        raise NotImplementedError

    def update_hold_commands(self, *, include_kernel: bool) -> list[str]:
        """Shell lines pinning packages before the upgrade runs.

        Returned as text rather than argv because they are written straight
        into the update script; a family that expresses exclusions as flags on
        the upgrade command itself returns nothing here.
        """
        return []

    def package_cleanup_command(self, mount_root: str) -> list[str] | None:
        """Frees the space the package manager used while building the medium.

        Every package downloaded during the install stays in the target's cache
        afterwards -- measured mid-install on this project's own run: 144 files
        and 48 MB before the desktop was even installed, and the desktop and
        firmware are an order of magnitude larger. On a medium whose whole
        point is being small and portable, that is space taken from the user
        for files that will never be read again.
        """
        return None

    def firmware_install_command(self, mount_root: str, image_mountpoint: str) -> list[str] | None:
        """Installs device firmware carried by the image but absent from its index.

        Returns None where the family needs nothing extra. See the Debian
        backend for why this exists at all.
        """
        return None

    def optional_package_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        """Installs packages whose absence must not abort the install.

        An installation image carries a subset of its distribution's archive,
        and which subset differs per image. firejail is the live example: it is
        in the RED OS image and not on the Debian DVD. Requiring it would mean
        an administrator who ticks "песочница" gets a failed install rather
        than a medium without that one extra layer.

        The wrapping shell is what makes the failure survivable -- the runner
        raises on a non-zero exit, and `|| true` is what keeps a missing
        optional package from looking like a broken medium.
        """
        argv = self.package_install_command(mount_root, packages)
        if argv is None:
            return None
        return ["sh", "-c", f"{shlex.join(argv)} || true"]

    def desktop_packages(self, config: InstallConfig) -> list[str]:
        """Packages the restricted desktop needs, or [] for a console system.

        Deliberately a hand-picked list rather than the distribution's desktop
        task/group: `task-mate-desktop` and RED OS's `mate-desktop` group each
        pull in around a hundred packages, among them a terminal, the MATE
        control centre, a system monitor and a text editor -- precisely the
        tools the kiosk exists to withhold. Not installing them is this mode's
        strongest restriction, so the list is chosen here and not delegated.
        """
        return []

    def display_manager_files(self, config: InstallConfig) -> dict[str, str]:
        """Display-manager configuration granting the kiosk account autologin.

        The account's password is locked (see chroot_setup_commands), so this
        is not a convenience: without autologin the medium boots to a login
        prompt no one can satisfy. It replaces the getty autologin a console
        system gets."""
        return {}

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

    def esp_population_commands(
        self, layout: MediaLayout, config: InstallConfig, *, source: str = ""
    ) -> list[list[str]]:
        """Что положить на смонтированный раздел EFI до сборки загрузчика.

        `source` — смонтированный эталон дистрибутива, если он есть. Оттуда
        берутся файлы, которые пришли в пакетах: копировать их из целевого
        дерева нельзя, потому что раздел EFI уже смонтирован поверх того
        места, где они лежали.

        `cp -r` без `-a`: на FAT нет владельцев, и попытка их сохранить
        завершается ошибкой, то есть валит установку на ровном месте.
        """
        if not source:
            return []
        return [
            ["sh", "-c",
             f"[ -d {source}/boot/efi/EFI ] && "
             f"cp -r {source}/boot/efi/. {layout.mount_root}/boot/efi/ || true"],
        ]

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

        commands += self.localisation_commands(mount_root, config)
        commands += self.service_commands(mount_root, config)

        pkg_cmd = self.package_install_command(mount_root, config.packages)
        if pkg_cmd is not None:
            commands.append(pkg_cmd)

        if config.remote_access.enabled and config.remote_access.method == "openssh":
            ssh_pkg_cmd = self.package_install_command(mount_root, [self.ssh_package])
            if ssh_pkg_cmd is not None:
                commands.append(ssh_pkg_cmd)
            commands.append(self.systemctl_enable(mount_root, self.ssh_service))

        commands.append(self.systemctl_enable(mount_root, "rsyslog"))

        commands += autoupdate.chroot_commands(mount_root, config, self)

        # After config.packages above, never before: the whitelist hides the
        # desktop entries that exist at the moment it runs, and a package
        # installed afterwards would arrive with its launchers still visible.
        commands += kiosk_mode.chroot_commands(mount_root, config, self)

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
        loc = config.localisation
        hostname = config.hostname or "portable-os"
        files = {
            "etc/hostname": f"{hostname}\n",
            "etc/hosts": f"127.0.0.1\tlocalhost\n127.0.1.1\t{hostname}\n",
            # Язык, раскладка и часовой пояс. До 26.08.2026 они были зашиты в
            # код и попадали только в файл для валидатора, но не на носитель:
            # система ставилась с en_US.UTF-8 и UTC независимо от того, что
            # написано в конфигурации.
            "etc/locale.conf": f"LANG={loc.locale}\n",
            "etc/vconsole.conf": f"KEYMAP={loc.keymap}\n",
            "etc/X11/xorg.conf.d/00-poa-keyboard.conf": self._keyboard_conf(config),
            "etc/fstab": self._fstab(layout, config),
            "etc/crypttab": self._crypttab(layout, config),
            "etc/rsyslog.d/10-poa.conf": (
                "# Portable OS admin: mirror everything into a file on the separate\n"
                "# log volume, which the administrator can open with their own key\n"
                "# without the user's LUKS passphrase (see admin/logs.py).\n"
                "*.*\t-/var/log/poa-system.log\n"
            ),
        }

        if kiosk_mode.is_enabled(config):
            # A kiosk logs in through the display manager instead, and its
            # getty units are masked -- writing a console autologin override
            # here would leave a text login the whole mode exists to remove.
            files.update(self.display_manager_files(config))
        else:
            files["etc/systemd/system/getty@tty1.service.d/autologin.conf"] = (
                "# The account's password is deliberately locked; the LUKS passphrase is\n"
                "# the single front-door secret, so the console logs the user in directly.\n"
                "[Service]\n"
                "ExecStart=\n"
                f"ExecStart=-/sbin/agetty --autologin {account.username} --noclear %I $TERM\n"
            )
        return files

    def localisation_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Часовой пояс и генерация локали внутри целевой системы.

        Файлами это не сделать: `/etc/localtime` — символическая ссылка на
        файл из tzdata, а локаль надо ещё собрать, иначе LANG указывает на
        то, чего в системе нет, и всё откатывается к C.

        `localectl` и `timedatectl` здесь неприменимы: они разговаривают с
        systemd, а внутри chroot его нет.
        """
        loc = config.localisation
        commands = [
            self.chroot(
                mount_root, "ln", "-sf", f"/usr/share/zoneinfo/{loc.timezone}", "/etc/localtime"
            ),
        ]
        # /etc/adjtime решает, считать ли аппаратные часы идущими по UTC.
        # Важно для носителя, который приносят на машину с Windows: там часы
        # обычно стоят по местному времени.
        clock = "UTC" if loc.hardware_clock_utc else "LOCAL"
        commands.append(
            self.chroot(mount_root, "sh", "-c", f"printf '0.0 0 0.0\\n0\\n{clock}\\n' > /etc/adjtime")
        )
        commands += self.locale_generation_commands(mount_root, config)
        return commands

    def locale_generation_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Как это семейство собирает локаль. По умолчанию — никак.

        В RHEL-семействе локали приходят готовыми в glibc-langpack-*, а в
        Debian их надо собрать locale-gen; поэтому шаг переопределяется в
        бэкенде, а не пишется здесь общим.
        """
        return []

    def _keyboard_conf(self, config: InstallConfig) -> str:
        """Раскладка графического сеанса.

        Отдельно от консольной: под X11 и Wayland действует не KEYMAP из
        vconsole.conf, а настройка X-сервера, и система с русской консолью и
        английским рабочим столом — обычный результат того, что задали только
        первое.
        """
        loc = config.localisation
        layouts = loc.x11_layouts or loc.keymap
        options = (
            f'        Option "XkbOptions" "{loc.x11_options}"\n' if loc.x11_options else ""
        )
        return (
            "# generated by portable-os-admin\n"
            'Section "InputClass"\n'
            '    Identifier "poa-keyboard"\n'
            '    MatchIsKeyboard "on"\n'
            f'    Option "XkbLayout" "{layouts}"\n'
            f"{options}"
            "EndSection\n"
        )

    def scripted_files(
        self, config: InstallConfig, layout: MediaLayout | None = None
    ) -> dict[str, tuple[str, int | None]]:
        """Relative path -> (content, mode) for files that need an explicit mode.

        Separate from config_files() because that one writes plain
        configuration and leaves the mode to the default. A script written 0644
        is a script that never runs, and the failure is silent: the medium
        installs, boots, and simply never applies the kiosk whitelist or takes
        an update.

        Nothing here depends on the medium -- unlike config_files(), which
        addresses partitions by UUID -- so `layout` is optional and the
        distribution build writes these into the reference root without one.
        """
        files: dict[str, tuple[str, int | None]] = {}
        files.update(kiosk_mode.config_files(config))
        files.update(autoupdate.config_files(config, self))
        return files

    def _fstab(self, layout: MediaLayout, config: InstallConfig) -> str:
        kiosk = kiosk_mode.is_enabled(config)
        # The exchange volume is the one place a user of the portable system can
        # put a file of their own, so in kiosk mode it is also the one place a
        # brought-in binary would be run from.
        exchange_options = (
            f"{kiosk_mode.HARDENED_OPTS},nofail" if kiosk else "defaults,nofail"
        )
        # Nothing in /var/log is ever meant to be executed, on any profile, but
        # it is only hardened in kiosk mode -- a console system's administrator
        # may legitimately want to run something from there while debugging.
        log_options = f"{kiosk_mode.HARDENED_OPTS},nofail" if kiosk else "defaults,nofail"
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
            (layout.logs_mapper_path, LOG_MOUNTPOINT, "ext4", log_options, "0", "2"),
            (layout.exchange_mapper_path, EXCHANGE_MOUNTPOINT, "ext4", exchange_options, "0", "2"),
        ]
        # The kiosk takes execution away from everything the user can write to.
        # This is the one restriction of the mode that no desktop setting can
        # undo -- it is the kernel refusing execve on a file the user supplied.
        rows += kiosk_mode.hardening_fstab_rows() if kiosk else []
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
