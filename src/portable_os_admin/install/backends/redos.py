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
from .. import machine_binding
from ..disk import MediaLayout
from .. import overlay as overlay_mode
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
    firewall_service = "firewalld"
    bootloader_comes_from_packages = True
    #: RHEL-семейство слушает `rhgb`, а не `splash`.
    GRAPHICAL_BOOT_PARAMETER = "rhgb"

    ARCH_NAMES = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    GRUB_TARGETS = {"x86_64": "x86_64-efi", "aarch64": "arm64-efi"}

    #: RHEL-family EFI package suffixes, which do not match the arch name
    EFI_SUFFIX = {"x86_64": "x64", "aarch64": "aa64"}

    #: The base system, named package by package instead of as the `@core`
    #: comps group.
    #:
    #: `@core` is a real group in the image's comps.xml and reads correctly --
    #: but resolving it makes dnf **segfault** on this project's build host
    #: (Debian trixie, python3-dnf, arm64). Reproduced down to the cause:
    #: installing a single package from the same repository works, `repoquery`
    #: against it works, and dropping `--forcearch` changes nothing -- only the
    #: group does it, and it dies right after the metadata is read.
    #:
    #: Nothing here can fix a crash in dnf's group handling, so the group is
    #: not used. Everything below is a leaf that pulls the rest in by
    #: dependency: measured, the list resolves to 221 packages -- a working
    #: base system.
    CORE_PACKAGES = (
        "basesystem",
        "filesystem",
        "setup",
        "redos-release",
        "bash",
        "coreutils",
        "systemd",
        "rpm",
        "dnf",
        "shadow-utils",
        "passwd",
        "util-linux",
        "iproute",
        "NetworkManager",
        "sudo",
        "kbd",
    )

    #: Checked name by name against redos-8's own repodata, not recalled. The
    #: distribution's `mate-desktop` comps group is deliberately not used: it
    #: lists 103 packages including mate-terminal, mate-control-center,
    #: mate-system-monitor, caja-open-terminal and gparted -- every one of them
    #: a way out of a kiosk. RED OS's own kiosk mode blocks those tools after
    #: installing them; not installing them is the stronger position.
    #: X plus **every** video and input driver the image carries, and the
    #: full firmware set. Named individually rather than via the `base-x`
    #: group, which also pulls nvidia-kmod, VirtualBox-kmod and open-vm-tools.
    #:
    #: All fourteen xorg-x11-drv-* packages of the image are here on purpose:
    #: the medium is meant to boot on hardware nobody has seen yet, and a
    #: missing driver there means a black screen with no way to fix it.
    GRAPHICS_PACKAGES = (
        "xorg-x11-server-Xorg",
        # No xorg-x11-xinit: that is startx, for bringing X up by hand. The
        # display manager starts the server itself.
        "xorg-x11-xauth",
        "xorg-x11-drv-amdgpu",
        "xorg-x11-drv-ati",
        "xorg-x11-drv-evdev",
        "xorg-x11-drv-fbdev",
        "xorg-x11-drv-intel",
        "xorg-x11-drv-libinput",
        "xorg-x11-drv-nouveau",
        "xorg-x11-drv-openchrome",
        "xorg-x11-drv-qxl",
        "xorg-x11-drv-vesa",
        "xorg-x11-drv-vmware",
        "xorg-x11-drv-wacom",
        "mesa-dri-drivers",
        # The whole firmware set, plus the GPU-specific ones. RED OS packages
        # these in its own repository, so unlike Debian there is nothing to
        # install by hand off the image.
        "linux-firmware",
        "amd-gpu-firmware",
        "intel-gpu-firmware",
        "nvidia-gpu-firmware",
        "iwlwifi-dvm-firmware",
        "iwlwifi-mvm-firmware",
        "dbus-x11",
        "dconf",
        "desktop-file-utils",
        "polkit",
        "NetworkManager",
        "dejavu-sans-fonts",
    )

    #: Minimal Plasma. RED OS 8 ships KDE as its own desktop -- the image
    #: carries redos-kde-theme-cert, the certified edition's theme -- so this
    #: is the distribution's native environment rather than an add-on.
    #: `konsole5` (RED OS's name for the terminal) and the settings centre are
    #: deliberately absent.
    KDE_PACKAGES = (
        "plasma-desktop",
        "plasma-workspace",
        "kwin",
        "sddm",
        "plasma-nm",
        "kde-settings-plasma",
        "breeze-icon-theme",
    )

    KIOSK_PACKAGES = (
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
        "network-manager-applet",
    )

    # ------------------------------------------------------------------
    # Unattended updates, run on the medium itself
    # ------------------------------------------------------------------
    def package_cleanup_command(self, mount_root: str) -> list[str] | None:
        """Empties dnf's caches off the medium.

        `--installroot` rather than a chroot, to match how every other dnf call
        in this backend addresses the target.
        """
        return ["dnf", "--installroot", mount_root, "-y", "clean", "all"]

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
        packages = list(self.GRAPHICS_PACKAGES)
        if kiosk_mode.desktop_of(config) == kiosk_mode.DESKTOP_KDE:
            return packages + list(self.KDE_PACKAGES)
        packages += list(self.KIOSK_PACKAGES)
        if config.kiosk.screen_blank_minutes > 0:
            packages.append("mate-screensaver")
        return packages

    def display_manager_files(self, config: InstallConfig) -> dict[str, str]:
        """SDDM autologin, both desktops.

        `Relogin=true` matters here in a way it does not on a normal desktop:
        the kiosk account cannot log in by hand -- its password is locked -- so
        without it, logging out would leave a greeter nobody can get past.
        """
        return {
            "etc/sddm.conf.d/50-poa-kiosk.conf": (
                "# generated by portable-os-admin\n"
                "[Autologin]\n"
                f"User={config.account.username}\n"
                # This program's own session entry rather than the
                # distribution's: "plasma" is the X11 session on Plasma 5 and
                # the Wayland one on Plasma 6, so naming it would pick a
                # different protocol -- and a different set of restrictions --
                # depending on which distribution this runs against.
                f"Session={kiosk_mode.SESSION_NAME}\n"
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
        # directly.
        #
        # `--repo=poa-image` already restricts the transaction to that one
        # repository -- it enables what it names and disables everything else.
        # Passing `--disablerepo=*` alongside it is not merely redundant, dnf
        # refuses the combination outright:
        #
        #     argument --repo/--repoid: not allowed with argument --disablerepo
        #
        # Which is what the first real RED OS run died on, immediately. The
        # backend had never been executed before that point, so nothing had
        # ever put these two flags in front of dnf together.
        return [
            "dnf",
            "--installroot", mount_root,
            "--releasever", release,
            "-y",
            f"--repofrompath=poa-image,{source_url}",
            "--repo=poa-image",
            "--nogpgcheck",
            "--forcearch", self.normalize_arch(config.arch),
            "install", *self.core_package_names(),
        ]

    def core_package_names(self) -> tuple[str, ...]:
        return tuple(
            name for name in self._core_package_names()
            if name not in self.UNWANTED_PACKAGES
        )

    def _core_package_names(self) -> tuple[str, ...]:
        """The base system: from the image where possible, from here otherwise.

        The image's own comps.xml is the authority -- it is what that release
        of that distribution says its base system is. Falling back to the list
        below only matters for an image with no comps or one this program
        could not parse, and even then a working system is likelier than a
        refusal.
        """
        return self.image_core_packages or self.CORE_PACKAGES

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
        files = {
            "usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/91poa-recovery/poa-recovery-hook.sh": (hook, 0o755),
            "etc/dracut.conf.d/91-poa-recovery.conf": (
                "# generated by portable-os-admin\n"
                "# dracut does not enable a module just because it is present.\n"
                'add_dracutmodules+=" poa-recovery "\n',
                0o644,
            ),
        }
        files.update(self._machine_binding_dracut(layout, config))
        return files

    def _machine_binding_dracut(self, layout, config) -> dict:
        """Модуль dracut, открывающий том с учётом привязки к машине.

        Отдельный модуль, а не хук внутри poa-recovery: они делают разное и в
        разное время. Этот открывает том вместо systemd-cryptsetup (см.
        unlock_kernel_parameters о том, почему вместо, а не после), тот
        вступает, когда открыть не удалось.

        Приоритет 50 против 99 у восстановления: сначала попытка открыть,
        потом разговор о том, что она не удалась.
        """
        if not machine_binding.is_enabled(config):
            return {}
        module_setup = (
            "#!/bin/bash\n"
            "# generated by portable-os-admin\n"
            "check() { return 0; }\n"
            "depends() { echo crypt; }\n"
            "install() {\n"
            "    inst_multiple sha256sum stty cut grep\n"
            '    inst_hook pre-mount 50 "$moddir/poa-machine-unlock.sh"\n'
            '    inst_hook pre-pivot 50 "$moddir/poa-machine-register.sh"\n'
            f'    mkdir -p "${{initdir}}{RECOVERY_INITRAMFS_DIR}"\n'
            f'    cp -a /{machine_binding.KEY_SCRIPT_PATH} "${{initdir}}{RECOVERY_INITRAMFS_DIR}/" 2>/dev/null || true\n'
            f'    cp -a /{machine_binding.REGISTER_SCRIPT_PATH} "${{initdir}}{RECOVERY_INITRAMFS_DIR}/" 2>/dev/null || true\n'
            "}\n"
        )
        unlock_hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            f". {RECOVERY_INITRAMFS_DIR}/env 2>/dev/null || true\n"
            f"{machine_binding.dracut_unlock_script(config, layout.mapper_name)}"
        )
        media_id = layout.mapper_name.rsplit("-", 1)[-1]
        register_hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            "# Регистрация идёт после того, как том открыт, и до передачи\n"
            "# управления системе: заголовок LUKS правится, пока это ещё можно\n"
            "# сделать, не мешая работающей системе.\n"
            f". {RECOVERY_INITRAMFS_DIR}/env 2>/dev/null || true\n"
            "POA_PASSPHRASE=$(cat /run/poa-passphrase 2>/dev/null || true)\n"
            "export POA_PASSPHRASE\n"
            f"{machine_binding.register_script(config, media_id)}"
        )
        return {
            "usr/lib/dracut/modules.d/90poa-machine/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/90poa-machine/poa-machine-unlock.sh": (unlock_hook, 0o755),
            "usr/lib/dracut/modules.d/90poa-machine/poa-machine-register.sh": (register_hook, 0o755),
            "etc/dracut.conf.d/90-poa-machine.conf": (
                "# generated by portable-os-admin\n"
                'add_dracutmodules+=" poa-machine "\n',
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
        # No --disablerepo here either: dnf rejects it together with --repo,
        # and --repo already disables everything it does not name. This one
        # would have broken *every* package install, not just the bootstrap.
        arguments = [f"--repofrompath=poa-image,{self.install_source_url}", "--repo=poa-image"]
        if self.install_arch:
            # The same --forcearch the bootstrap uses, and for the same reason:
            # dnf runs on the *host* and installs into a root of another
            # architecture. Without it every package after the base system is
            # refused with "does not have a compatible architecture" -- which
            # is what the first RED OS build hit immediately after successfully
            # unpacking a base system made of those very packages.
            arguments += ["--forcearch", self.install_arch]
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

    def local_package_install_command(self, mount_root: str,
                                      filenames: list[str]) -> list[str] | None:
        """`dnf install /path/to/file.rpm` -- dnf, а не rpm -i.

        dnf читает зависимости принесённого пакета и доставляет их из образа;
        `rpm -i` на первой же недостающей зависимости откажет, а с --nodeps
        поставит нерабочее. Путь указывается внутри целевой системы: dnf
        запускается снаружи с --installroot, поэтому файл должен лежать под
        ним и быть виден по тому же имени.
        """
        if not filenames:
            return None
        paths = [f"{mount_root}/{self.EXTRA_PACKAGE_DIR}/{name}" for name in filenames]
        return ["dnf", "--installroot", mount_root, "-y", *self._repo_arguments(),
                "install", *paths]

    #: Пакеты, которые убираются из состава, даже если их назвал сам образ.
    #:
    #: dracut-config-rescue кладёт на носитель второй initramfs — замер на
    #: собранном носителе: 138 МБ при 974 МБ раздела /boot, то есть больше
    #: седьмой части. Для переносного носителя он бесполезен по смыслу:
    #: систему, которая не загружается, чинят не с неё же, а с другого
    #: носителя или из режима восстановления, который у нас свой.
    UNWANTED_PACKAGES = ("dracut-config-rescue",)

    def boot_packages(self, config: InstallConfig) -> list[str]:
        arch = self.normalize_arch(config.arch)
        suffix = self.EFI_SUFFIX[arch]
        return [
            "kernel",
            f"grub2-efi-{suffix}",
            # Модули grub нужны не для сборки образа (её тут нет — загрузчик
            # приходит готовым), а самому grub во время работы: их он
            # подгружает с раздела EFI по ходу загрузки.
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
        return self.chroot(
            mount_root, "dracut", "--force", "--regenerate-all",
            # Без этого dracut собирает образ под машину, на которой идёт
            # сборка. Здесь это chroot внутри чужой ВМ, и в образ попадают её
            # драйверы, а не те, что нужны носителю.
            "--no-hostonly",
            # И командную строку тоже не «фиксировать по месту»: параметры
            # носителя приходят из записи загрузчика, а не из окружения сборки.
            "--no-hostonly-cmdline",
        )

    #: Где политика targeted держит описание меток.
    FILE_CONTEXTS = "/etc/selinux/targeted/contexts/files/file_contexts"

    def relabel_commands(self, mount_root: str) -> list[list[str]]:
        return [
            # `-F` — переписывать даже там, где метка уже есть: после
            # установки с чужой машины они либо отсутствуют, либо неверны.
            self.chroot(mount_root, "sh", "-c",
                        f"setfiles -F {self.FILE_CONTEXTS} / 2>/dev/null || true"),
        ]

    def restore_labels_command(self, mount_root: str, paths: tuple[str, ...] = ("/etc",)) -> list[str] | None:
        """Метки для файлов, дописанных на носитель после разметки эталона.

        `setfiles`, а не `restorecon`, и это не вкусовщина: restorecon
        разговаривает с работающим SELinux, а внутри chroot его нет — команда
        молча ничего не делает. Проверено на носителе: у файлов из
        дистрибутива метки были, у fstab и настройки автовхода — нет, и SDDM
        не смог прочитать собственную конфигурацию:

            avc: denied { getattr } comm="sddm"
                 path="/etc/sddm.conf.d/50-poa-kiosk.conf"
                 tcontext=system_u:object_r:unlabeled_t:s0

        setfiles берёт правила из файла политики и работает без ядра — тем же
        способом, что и разметка эталона при сборке.
        """
        return self.chroot(
            mount_root, "sh", "-c",
            f"setfiles -F {self.FILE_CONTEXTS} " + " ".join(paths) + " 2>/dev/null || true",
        )

    def generic_initramfs_files(self) -> dict[str, tuple[str, int | None]]:
        return {
            "etc/dracut.conf.d/50-poa-generic.conf": (
                "# generated by portable-os-admin\n"
                "# Носитель переносной: образ должен содержать драйверы для\n"
                "# машины, которой мы не видели, а не для той, где он собран.\n"
                "hostonly=no\n"
                "hostonly_cmdline=no\n",
                None,
            )
        }

    #: Каталог этого дистрибутива на разделе EFI. Туда пакеты кладут grub, и
    #: оттуда же он читает свою конфигурацию: путь вшит в сам grubx64.efi.
    EFI_VENDOR_DIR = "EFI/redos"

    def firewall_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """firewalld плюс открытые порты.

        `firewall-offline-cmd` — то, чем это делается, когда служба не
        запущена, а внутри chroot она запущена быть и не может. Обычный
        `firewall-cmd` там просто не с чем разговаривать.
        """
        commands = super().firewall_commands(mount_root, config)
        if not config.firewall.enabled:
            return commands
        for port in config.firewall.open_ports:
            commands.append(
                self.chroot(mount_root, "firewall-offline-cmd", f"--add-port={port}")
            )
        return commands

    def overlay_boot_files(self, layout: MediaLayout) -> dict:
        """dracut-модуль, собирающий корень из образа и слоя изменений.

        Хук `pre-pivot` — точка, где корень уже смонтирован в `$NEWROOT` и его
        ещё можно подменить. Модули ядра перечисляются явно: dracut кладёт в
        образ то, что нужно найденной конфигурации, а overlay, squashfs и loop
        в неё не входят — без них скрипт не соберёт ничего.

        Модуль надо ещё и включить: наличия каталога dracut недостаточно, и
        забыть эту строку значит получить носитель без всякой жалобы при
        сборке.
        """
        module_setup = (
            "#!/bin/bash\n"
            "# generated by portable-os-admin\n"
            "check() { return 0; }\n"
            "depends() { echo crypt; }\n"
            "installkernel() {\n"
            "    instmods overlay squashfs loop\n"
            "}\n"
            "install() {\n"
            '    inst_hook pre-pivot 50 "$moddir/poa-overlay-hook.sh"\n'
            "}\n"
        )
        hook = overlay_mode.boot_script(layout.mapper_name, overlay_mode.ROOTMNT_DRACUT)
        return {
            "usr/lib/dracut/modules.d/50poa-overlay/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-hook.sh": (hook, 0o755),
            "etc/dracut.conf.d/50-poa-overlay.conf": (
                "# generated by portable-os-admin\n"
                "# dracut не включает модуль просто потому, что он есть.\n"
                'add_dracutmodules+=" poa-overlay "\n',
                None,
            ),
        }

    def unlock_kernel_parameters(self, layout: MediaLayout, config: InstallConfig | None = None) -> list[str]:
        """`rd.luks.*` — то, чем dracut узнаёт про зашифрованный корень.

        `rd.luks.name` задаёт имя тома: fstab и crypttab носителя ссылаются на
        /dev/mapper/poa-<media_id>, и без него имя оказалось бы другим.

        При включённой привязке к машине этих параметров **нет**, и это не
        упущение. С ними systemd-cryptsetup запрашивает пароль сразу, как
        только появляется устройство, — задолго до любого нашего хука, и
        подставить ключ машины было бы уже некуда. Перехватить его порядком
        хуков нельзя: dracut документирует, что в systemd-режиме приоритеты
        хуков не соблюдаются. Поэтому том открывает наш модуль, а не
        systemd-cryptsetup.
        """
        if not layout.luks_uuid:
            return []
        if config is not None and machine_binding.is_enabled(config):
            return []
        return [
            f"rd.luks.uuid={layout.luks_uuid}",
            f"rd.luks.name={layout.luks_uuid}={layout.mapper_name}",
        ]

    def _append_kernel_parameters(self, root: str, config: InstallConfig, layout: MediaLayout) -> str:
        """Команда, дописывающая параметры ядра в записи загрузчика.

        Главное здесь — не пользовательские параметры, а `rd.luks.*`.

        В RHEL-семействе указание расшифровать том приходит именно параметром
        ядра: так пишет anaconda, и так это работает у самого дистрибутива. В
        Debian ту же роль играет cryptsetup-initramfs, который читает crypttab
        при сборке образа, — поэтому там носитель загружался, а здесь нет.
        Без них initramfs молча ждёт появления устройства, которого без
        расшифровки не бывает, и через две минуты уходит в аварийный режим с
        сообщением про несуществующий UUID — по нему причину не угадать.

        `rd.luks.name` задаёт имя тома: fstab и crypttab носителя ссылаются на
        /dev/mapper/poa-<media_id>, и без этого имя оказалось бы другим.
        """
        extra = self.kernel_cmdline(config, layout)
        if not extra:
            return "true"
        marker = extra.split()[0]
        return (
            "for f in {root}/boot/loader/entries/*.conf; do "
            "[ -f \"$f\" ] || continue; "
            "grep -q '{marker}' \"$f\" || sed -i 's|^options .*|& {extra}|' \"$f\"; "
            "done 2>/dev/null || true"
        ).format(root=root, marker=marker, extra=extra)

    #: BLS: варианты загрузки описываются файлами в /boot/loader/entries, и
    #: grub2-mkconfig перезаписывает их строку `options` из GRUB_CMDLINE_LINUX.
    #: Отсюда и требование держать параметры расшифровки именно там.
    GRUB_EXTRA_DEFAULTS = (
        "GRUB_DEFAULT=saved",
        'GRUB_DISTRIBUTOR="$(sed \'s, release .*$,,g\' /etc/system-release)"',
        "GRUB_DISABLE_RECOVERY=true",
        "GRUB_ENABLE_BLSCFG=true",
    )

    def config_files(self, config: InstallConfig, layout: MediaLayout, image=None) -> dict[str, str]:
        files = super().config_files(config, layout, image)
        files["etc/default/grub"] = self.grub_defaults(config, layout)
        return files

    def security_files(self, config: InstallConfig) -> dict[str, str]:
        """Режим SELinux.

        Файл читает ядро при загрузке, поэтому задать режим можно только так:
        `setenforce` внутри chroot ничего не решает — там нет ядра целевой
        системы.

        Метки файлам здесь не проставляются: этим занимается
        `relabel_commands` при сборке дистрибутива. `/.autorelabel`
        рассматривался и не годится — он срабатывает после старта systemd, а
        без меток systemd не стартует вовсе и замирает на служебных файловых
        системах.
        """
        mode = config.firewall.selinux
        files = {
            "etc/selinux/config": (
                "# generated by portable-os-admin\n"
                f"SELINUX={mode}\n"
                "SELINUXTYPE=targeted\n"
            )
        }
        return files

    def bootloader_commands(self, layout: MediaLayout, config: InstallConfig) -> list[list[str]]:
        """Загрузчик ставится копированием, а не grub2-install.

        В RHEL-семействе `grub2-install` для EFI отключён патчем Red Hat и
        отвечает буквально: «This utility should not be used for EFI platforms
        because it does not support UEFI Secure Boot». И это не придирка:
        собранный на месте grubx64.efi окажется неподписанным, а носитель
        задуман переносным — на машине с включённым Secure Boot он не
        загрузится вовсе.

        Правильный способ здесь — тот, которым пользуется сам дистрибутив:
        подписанные shim и grub приходят готовыми в пакетах shim-x64 и
        grub2-efi-x64, а установка сводится к тому, чтобы положить их по
        пути, который прошивка ищет на съёмном носителе:
        `EFI/BOOT/BOOTX64.EFI`. Shim там уже лежит — его кладёт сам пакет, — а
        рядом с ним нужен grubx64.efi, потому что shim ищет его в своём
        каталоге.

        NVRAM не трогается: запись в переменные прошивки привязала бы носитель
        к одной машине, а он ездит между ними.
        """
        root = layout.mount_root
        efi = f"{root}/boot/efi"
        vendor = f"{efi}/{self.EFI_VENDOR_DIR}"
        return [
            # Пути в записях загрузчика приводятся к раскладке носителя.
            #
            # RHEL-семейство описывает варианты загрузки файлами BLS в
            # /boot/loader/entries, и пути в них считаются от той файловой
            # системы, на которой лежит /boot. По умолчанию у РЕД ОС
            # отдельного /boot нет (проверено по anaconda.conf образа:
            # разметка по умолчанию — только /, /home и swap), поэтому
            # kernel-install при сборке дистрибутива записал `/boot/vmlinuz-…`
            # — и для той раскладки это верно.
            #
            # На носителе /boot обязан быть отдельным и незашифрованным, иначе
            # прошивке нечего загружать. Внутри этого раздела путь начинается
            # с его корня, и запись должна говорить `/vmlinuz-…`. Иначе GRUB
            # ищет `POA_BOOT:/boot/vmlinuz-…`, которого там нет, и отвечает
            # «file not found» с последующим «you need to load the kernel
            # first» — по этим сообщениям причину не угадать.
            ["sh", "-c",
             f"sed -i -e 's|^linux /boot/|linux /|' -e 's|^initrd /boot/|initrd /|' "
             f"{root}/boot/loader/entries/*.conf 2>/dev/null || true"],
            ["mkdir", "-p", f"{efi}/EFI/BOOT"],
            # Shim ищет grubx64.efi рядом с собой, а пакет кладёт его в
            # каталог дистрибутива.
            ["sh", "-c", f"cp -f {vendor}/grubx64.efi {efi}/EFI/BOOT/grubx64.efi"],
            # Если shim в EFI/BOOT почему-то не оказалось (пакет не поставлен
            # или файл вычищен), берём его из каталога дистрибутива: без
            # BOOTX64.EFI прошивка съёмный носитель просто не увидит.
            ["sh", "-c",
             f"[ -f {efi}/EFI/BOOT/BOOTX64.EFI ] || cp -f {vendor}/shimx64.efi {efi}/EFI/BOOT/BOOTX64.EFI"],
            ["sh", "-c", f"[ -f {vendor}/mmx64.efi ] && cp -f {vendor}/mmx64.efi {efi}/EFI/BOOT/ || true"],
            # Конфигурация пишется туда, откуда её читает grub: путь вшит в
            # двоичный файл при сборке пакета, и от того, откуда grub
            # запустили, он не зависит.
            # Путь внутри chroot: раздел EFI смонтирован в /boot/efi, поэтому
            # каталог дистрибутива — /boot/efi/EFI/redos, а не /boot/EFI/redos.
            # На этом я и ошибся: grub2-mkconfig отвечает «нет такого файла»
            # про свой временный grub.cfg.new, и по сообщению не догадаться,
            # что не найден каталог назначения.
            self.chroot(root, "grub2-mkconfig", "-o", f"/boot/efi/{self.EFI_VENDOR_DIR}/grub.cfg"),
            # После генерации, а не до: grub2-mkconfig перезаписывает строку
            # `options` в записях BLS, и всё, что дописано раньше, теряется.
            #
            # Дописывается только если генератор этого не сделал сам. Первая
            # версия дописывала всегда — и параметры оказывались в строке
            # дважды. Для rd.luks.name это не безобидно: dracut склеивал два
            # имени через пробел и пытался запустить службу
            # `systemd-cryptsetup@poa-xxx poa-xxx`, которой нет.
            ["sh", "-c", self._append_kernel_parameters(root, config, layout)],
        ]
