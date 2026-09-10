"""Debian/Ubuntu backend, using debootstrap ("Программные интерфейсы-4":
automated install driven by the install configuration).

debootstrap alone produces a root filesystem with no kernel, no initramfs and
no bootloader -- it is a base *system*, not a bootable one. `boot_packages`
below is what turns it into something a machine can actually start.
"""
from __future__ import annotations

import shlex

from ...config.schema import InstallConfig
from .. import kiosk as kiosk_mode
from ..disk import MediaLayout
from .. import overlay as overlay_mode
from .. import machine_binding
from .base import RECOVERY_DIR, RECOVERY_INITRAMFS_DIR, InstallBackend, maybe_eatmydata


class DebianInstallBackend(InstallBackend):
    name = "debian"

    #: `dpkg -L` -- тот же вопрос к менеджеру пакетов Debian: что лежит в
    #: установленном пакете.
    PACKAGE_FILES_COMMAND = ("dpkg", "-L")

    TITLE = "Debian (стандартный, debootstrap)"
    PROVENANCE = "Проверено против deb.debian.org."
    #: Новейший первым. `trixie` -- то, на чём этот бэкенд прогонялся
    #: полностью, от сборки до рабочего стола.
    RELEASES = ("trixie", "bookworm", "bullseye")
    DEFAULT_RELEASE = "trixie"

    def default_mirror(self, release: str = "", arch: str = "") -> str:
        # Один адрес на все выпуски и архитектуры: suite и arch Debian
        # выбирает внутри репозитория сам.
        return "https://deb.debian.org/debian"
    ssh_package = "openssh-server"
    ssh_service = "ssh"
    sudo_group = "sudo"
    display_manager_service = "lightdm"

    #: Экран загрузки рисует `script`, а не `two-step`.
    #:
    #: Общего плагина у семейств нет, и это не недоделка дистрибутивов, а
    #: факт: в plymouth 24.004.60 Debian 13 лежат details, script, text и
    #: tribar -- `two-step`, на котором написана тема РЕД ОС, отсутствует.
    #: Отдельного пакета с темами на DVD тоже нет. Поэтому облик задаётся один
    #: раз в базовом классе, а семейство отвечает только за то, чем рисовать.
    PLYMOUTH_PLUGIN = "script"

    #: Шрифт консоли с кириллицей. Имя из набора console-setup, где шрифты
    #: называются `<набор символов>-<гарнитура><размер>`: CyrSlav -- славянская
    #: кириллица, то есть русский. Проверено по console-setup-linux 1.242
    #: Debian 13: 745 файлов шрифтов, среди них CyrSlav-Terminus16.
    CONSOLE_FONT = "CyrSlav-Terminus16"

    EXTRA_TOOLS = ("debootstrap",)

    #: Чего не должно быть в базовой системе, хотя дистрибутив кладёт это сам.
    #:
    #: debootstrap ставит всё с приоритетами required и important, а nano и
    #: vim-tiny числятся important — то есть редактор приезжает в систему,
    #: которой по определению редактор не положен. Проверено по индексу
    #: образа Debian 13, а не по памяти.
    #:
    #: mc и htop здесь не нужны: в Debian у них приоритет optional, и
    #: debootstrap их не ставит вовсе. В РЕД ОС оба лежат в группе `core`
    #: образа, поэтому там список длиннее — расхождение не в подходе, а в
    #: том, что каждое семейство кладёт в базу.
    #:
    #: `less` оставлен намеренно, как и у РЕД ОС: он important, его тянет
    #: man-db, и сам он ни файлами не управляет, ни текст не правит.
    UNWANTED_PACKAGES = ("nano", "vim-tiny")

    ARCH_NAMES = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }

    #: Every name here was checked against debian-13.6.0-amd64-DVD-1.iso's own
    #: package index rather than recalled: an image is a subset of the archive,
    #: and a name that exists on deb.debian.org but not on the DVD would fail
    #: the install forty minutes in. `firejail` and `chromium`, for instance,
    #: are *not* on that DVD, which is why firejail is installed by a separate
    #: command that is allowed to fail.
    #: X and everything needed to drive whatever hardware the medium is
    #: eventually plugged into. Shared by both desktops.
    #:
    #: Both driver metapackages, deliberately: they are the bulk of the
    #: install, and the alternative -- only the driver for the card in the
    #: administrator's own machine -- gives a medium that shows a black screen
    #: on the next machine it is carried to, which is the one thing a portable
    #: system may not do.
    GRAPHICS_PACKAGES = (
        "xserver-xorg",
        "xserver-xorg-video-all",
        "xserver-xorg-input-all",
        "x11-xserver-utils",
        "firmware-linux-free",
        "dbus-x11",
        "fonts-dejavu",
        "network-manager",
        # dconf-cli carries `dconf update`, which compiles MATE's lockdown
        # database; harmless under KDE and cheap either way.
        "dconf-cli",
        "dconf-gsettings-backend",
        "desktop-file-utils",
    )

    #: Чем Debian отличается от общего описания среды (install/desktop_roles.py).
    #:
    #: Четыре строки на две среды -- столько и есть настоящих различий. До
    #: 02.09.2026 здесь лежали оба набора целиком, полсотни строк вместе с
    #: объяснениями, и те же объяснения слово в слово лежали в бэкенде РЕД ОС.
    DESKTOP_PACKAGE_OVERRIDES = {
        "mate": {
            # В образе Debian sddm есть, но lightdm легче и не требует своей
            # шины для автовхода.
            "display-manager": "lightdm",
            "network-applet": "network-manager-gnome",
        },
        # kwin в Debian разделён по протоколам; сеанс здесь X11, а вариант
        # Wayland добавляется отдельно -- см. WAYLAND_PACKAGES.
        "kde": {"compositor": "kwin-x11", "icons": "breeze"},
    }

    #: Пакеты, которым роли нет.
    #:
    #: Оболочка lightdm поставляется отдельно от самого менеджера входа: без
    #: неё он поднимется и покажет пустой экран. Пакет тем оформления GTK нужен
    #: Plasma затем, чтобы приложения на GTK -- а разрешённые приложения чаще
    #: всего именно такие -- не выглядели чужими в сеансе KDE.
    DESKTOP_EXTRA_PACKAGES = {
        "mate": ("lightdm-gtk-greeter",),
        "kde": ("kde-config-gtk-style",),
    }

    #: Сеанс Plasma под Wayland: композитор и мост для приложений, умеющих
    #: только X -- а это большинство того, что киоск разрешает.
    WAYLAND_PACKAGES = ("kwin-wayland", "xwayland")

    def display_manager_files(self, config: InstallConfig) -> dict[str, str]:
        """Autologin, because the account has no usable password.

        Without it the medium boots to a login prompt nobody can satisfy --
        the single most damaging way this profile can fail, since the result is
        a stick that is finished, encrypted, and unusable.
        """
        username = config.account.username
        if kiosk_mode.desktop_of(config) == kiosk_mode.DESKTOP_KDE:
            return {
                "etc/sddm.conf.d/50-poa-kiosk.conf": (
                    "# generated by portable-os-admin\n"
                    "[Autologin]\n"
                    f"User={username}\n"
                    # This program's own session entry, not the distribution's.
                    # "plasma" means X11 under Plasma 5 and Wayland under
                    # Plasma 6, so naming it would pick a different protocol --
                    # and a different set of restrictions -- per distribution.
                    f"Session={kiosk_mode.SESSION_NAME}\n"
                    # The account cannot log in by hand, so logging out must
                    # come straight back rather than stop at a greeter.
                    "Relogin=true\n"
                    "\n"
                    "[Users]\n"
                    "HideUsers=*\n"
                )
            }
        return {
            "etc/lightdm/lightdm.conf.d/50-poa-kiosk.conf": (
                "# generated by portable-os-admin\n"
                "[Seat:*]\n"
                f"autologin-user={username}\n"
                # No countdown the user could interrupt to reach a prompt.
                "autologin-user-timeout=0\n"
                # This program's own session entry -- see the SDDM branch.
                f"autologin-session={kiosk_mode.SESSION_NAME}\n"
                f"user-session={kiosk_mode.SESSION_NAME}\n"
                "greeter-hide-users=true\n"
                "allow-guest=false\n"
            )
        }

    def bootstrap_command(
        self, mount_root: str, config: InstallConfig, source_url: str, release: str
    ) -> list[str]:
        # A Debian installation image is already a valid apt repository, so
        # debootstrap reads it directly over file:// -- nothing has to be
        # extracted and nothing is downloaded.
        argv = [
            "debootstrap",
            f"--arch={self.normalize_arch(config.arch)}",
        ]
        # То же, что UNWANTED_PACKAGES у РЕД ОС, и по той же причине: редактор
        # в базовой системе прямо нарушает её определение. Разница лишь в том,
        # где это перехватывается: у RHEL список базовых пакетов читается из
        # comps и фильтруется, а debootstrap ставит всё с приоритетами
        # required и important, и вырезать оттуда можно только --exclude.
        #
        # Не поставить дешевле, чем поставить и потом удалить: удаление стоит
        # ещё одного прохода apt по цепочке зависимостей внутри chroot.
        if self.UNWANTED_PACKAGES:
            argv.append(f"--exclude={','.join(self.UNWANTED_PACKAGES)}")
        if source_url.startswith("file:"):
            # Measured on debian-13.6.0-amd64-DVD-1.iso: the image carries
            # dists/<suite>/Release but neither Release.gpg nor InRelease, so
            # debootstrap's signature check cannot succeed and the whole
            # bootstrap dies with "Failed getting release signature file".
            # Without this flag installing from a local image never works --
            # which is the project's only supported source.
            #
            # Nothing is trusted that was not already trusted: the image file
            # the administrator picked is the trust anchor (Debian signs the
            # image checksums, not the repository tree inside it), and it is
            # mounted read-only from local storage, not fetched over a network.
            # The RedOS backend passes --nogpgcheck for the same reason; this
            # only removes the asymmetry between the two.
            argv.append("--no-check-gpg")
        argv += [release, mount_root, source_url]
        return maybe_eatmydata(argv)

    def install_time_sources(
        self, release: str, source_url: str, components: tuple[str, ...]
    ) -> tuple[str, str] | None:
        """Where apt gets packages from while the medium is being built.

        Written by this program rather than left to debootstrap. Measured, not
        assumed: after a bootstrap from a local image the target's
        /etc/apt/sources.list is **empty** -- debootstrap fills it in for an
        http mirror and not for a file:// one -- so every apt-get install in
        the chroot afterwards failed with "Unable to locate package
        linux-image-amd64", forty minutes into the run.

        [trusted=yes] for a local image, because an installation image carries
        dists/<suite>/Release but no signature over it, and apt drops an
        unsigned repository entirely rather than warning about it. The trust
        anchor is the image file the administrator chose; a network mirror is
        signed and gets no such marking.

        Components come from the image's own Release file, so an image built
        with a different set does not need a change here.
        """
        parts = " ".join(components) if components else "main"
        if source_url.startswith("file:"):
            return "etc/apt/sources.list", (
                "# temporary, for the duration of the installation\n"
                f"deb [trusted=yes] {source_url} {release} {parts}\n"
            )
        return "etc/apt/sources.list", (
            "# temporary, for the duration of the installation\n"
            f"deb {source_url} {release} {parts}\n"
        )

    def overlay_boot_files(self, layout) -> dict:
        """initramfs-tools: хук сборки и скрипт, собирающий корень.

        `local-bottom` — точка, где корень уже смонтирован в `$rootmnt`:
        именно там его и надо подменить на overlay. Хук нужен потому, что
        initramfs-tools не кладёт в образ модули, которых не требует
        обнаруженная конфигурация: overlay, squashfs и loop туда не попадут
        сами, а без них скрипт не соберёт ничего.
        """
        hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            'PREREQ=""\n'
            'prereqs() { echo "$PREREQ"; }\n'
            'case "$1" in prereqs) prereqs; exit 0;; esac\n'
            ". /usr/share/initramfs-tools/hook-functions\n"
            "\n"
            "# Без этих модулей скрипт local-bottom не соберёт корень, и\n"
            "# носитель не загрузится.\n"
            "manual_add_modules overlay\n"
            "manual_add_modules squashfs\n"
            "manual_add_modules loop\n"
        )
        script = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            'PREREQ=""\n'
            'prereqs() { echo "$PREREQ"; }\n'
            'case "$1" in prereqs) prereqs; exit 0;; esac\n'
            "\n"
            + overlay_mode.boot_script(layout.mapper_name).split("set -u\n", 1)[1]
        )
        return {
            "etc/initramfs-tools/hooks/poa-overlay": (hook, 0o755),
            "etc/initramfs-tools/scripts/local-bottom/poa-overlay": (script, 0o755),
        }

    def console_font_files(self, config: InstallConfig) -> dict[str, str]:
        """Чтобы шрифт с кириллицей оказался в initramfs, а не только в системе.

        В Debian это делается не через `vconsole.conf`: тот файл читает
        systemd уже в работающей системе. В initramfs шрифт кладёт хук
        `keymap` из initramfs-tools, и он выходит сразу, если не выполнены два
        условия -- `KEYMAP=y` в его настройках и установленный `setupcon`
        («setupcon is missing. Please install the 'console-setup' package»).
        Прочитано в самом хуке, а не предположено.

        `setupcon` берёт, что класть, из `/etc/default/console-setup`: набор
        символов и гарнитуру с размером. Имя шрифта здесь не пишется целиком,
        оно из них складывается -- CyrSlav + Terminus + 16.
        """
        codeset, face_size = self.CONSOLE_FONT.split("-", 1)
        face = face_size.rstrip("0123456789x")
        size = face_size[len(face):]
        return {
            "etc/default/console-setup": (
                "# generated by portable-os-admin\n"
                "ACTIVE_CONSOLES=\"/dev/tty[1-6]\"\n"
                "CHARMAP=\"UTF-8\"\n"
                f"CODESET=\"{codeset}\"\n"
                f"FONTFACE=\"{face}\"\n"
                f"FONTSIZE=\"{size}\"\n"
            ),
            # Без этой строки хук keymap выходит первым же условием, и в
            # образе не оказывается ни раскладки, ни шрифта.
            "etc/initramfs-tools/conf.d/poa-console": (
                "# generated by portable-os-admin\n"
                "# Раскладка и шрифт консоли -- в initramfs: там носитель\n"
                "# спрашивает пароль, и там же он объясняет, что случилось.\n"
                "KEYMAP=y\n"
            ),
        }

    def machine_binding_files(self, config: InstallConfig, layout=None) -> dict:
        """Скрипты привязки плюс то, чем Debian их запускает.

        Ключ подставляет `keyscript` из crypttab -- это штатный механизм
        cryptsetup-initramfs, и он работал. А вот регистрацию машины на этом
        семействе не запускал никто: у РЕД ОС её вызывает свой dracut-хук
        (`pre-pivot`), а здесь соответствующего не было вовсе.

        Найдено прогоном Debian + KDE 01.09.2026: том открылся, рабочий стол
        поднялся, а машинные keyslot'ы остались пустыми -- то есть привязка,
        включённая в конфигурации, не действовала совсем. Фраза открывала бы
        носитель на любой машине, и заметить это можно было только заглянув в
        заголовок LUKS.

        Хук ставится в `local-bottom`: он выполняется после того, как корень
        разблокирован и смонтирован, но ещё в initramfs -- то есть ровно там
        же по смыслу, где `pre-pivot` у dracut. Заголовок правится, пока это
        не мешает работающей системе.
        """
        files = super().machine_binding_files(config, layout)
        if not files or layout is None or not layout.mapper_name:
            return files

        media_id = layout.mapper_name.rsplit("-", 1)[-1]
        # Хук сборки: кладёт в образ сам скрипт регистрации и cryptsetup,
        # которым тот пишет keyslot. `copy_exec` -- потому что cryptsetup
        # тянет библиотеки, и копирования одного файла мало.
        hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            'PREREQ=""\n'
            'prereqs() { echo "$PREREQ"; }\n'
            'case "$1" in prereqs) prereqs; exit 0;; esac\n'
            ". /usr/share/initramfs-tools/hook-functions\n"
            "\n"
            "copy_exec /sbin/cryptsetup /sbin/cryptsetup 2>/dev/null || "
            "copy_exec /usr/sbin/cryptsetup /usr/sbin/cryptsetup 2>/dev/null || true\n"
            f'mkdir -p "${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}"\n'
            f'cp -a /{machine_binding.REGISTER_SCRIPT_PATH} '
            f'"${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}/poa-machine-register" 2>/dev/null || true\n'
            f'chmod 0755 "${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}/poa-machine-register" '
            "2>/dev/null || true\n"
        )
        # Загрузочный хук: запускает регистрацию, отдав ей фразу, которую
        # keyscript оставил в /run. Второй раз спрашивать пароль было бы
        # похоже на сбой -- это уже решено на РЕД ОС, и здесь так же.
        script = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            'PREREQ="cryptroot"\n'
            'prereqs() { echo "$PREREQ"; }\n'
            'case "$1" in prereqs) prereqs; exit 0;; esac\n'
            "\n"
            "# Тома нет -- значит открыть его не удалось, и регистрировать\n"
            "# нечего: разговор ведёт модуль восстановления.\n"
            f"[ -b /dev/mapper/{layout.mapper_name} ] || exit 0\n"
            f"[ -x {RECOVERY_INITRAMFS_DIR}/poa-machine-register ] || exit 0\n"
            f"POA_LUKS_UUID={layout.luks_uuid}\n"
            "export POA_LUKS_UUID\n"
            "POA_PASSPHRASE=$(cat /run/poa-passphrase 2>/dev/null || true)\n"
            "export POA_PASSPHRASE\n"
            f"{RECOVERY_INITRAMFS_DIR}/poa-machine-register </dev/console >/dev/console 2>&1\n"
        )
        files["etc/initramfs-tools/hooks/poa-machine"] = (hook, 0o755)
        files["etc/initramfs-tools/scripts/local-bottom/poa-machine"] = (script, 0o755)
        return files

    def recovery_boot_files(self, layout, config) -> dict:
        """initramfs-tools integration for the recovery module.

        Two files. The hook runs on this machine while the initramfs is being
        built and copies in what the flow needs: openssl (for the scrypt
        derivation), plus the module, its salt and its settings. The boot
        script runs on the medium, declares cryptroot as its prerequisite so it
        is ordered after it, and takes over only when the root mapping is still
        absent -- that is, when the user has used up the attempts crypttab
        allows.

        busybox supplies od, stty and sha256sum, so only openssl has to be
        added; cryptsetup-initramfs already brings cryptsetup.
        """
        mapper = layout.mapper_name
        hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            "PREREQ=\"\"\n"
            "prereqs() { echo \"$PREREQ\"; }\n"
            "case \"$1\" in prereqs) prereqs; exit 0;; esac\n"
            ". /usr/share/initramfs-tools/hook-functions\n"
            "\n"
            "# The recovery flow derives the passphrase with scrypt, which only\n"
            "# openssl can do here; busybox covers the rest.\n"
            "#\n"
            "# A failure here must not end the hook: the file copies below are\n"
            "# what put the recovery flow into the image at all, and skipping\n"
            "# them would produce a medium with no recovery and no complaint.\n"
            "if ! copy_exec /usr/bin/openssl /usr/bin/openssl; then\n"
            "    echo \"poa-recovery: openssl не скопирован, восстановление работать не будет\" >&2\n"
            "fi\n"
            f"mkdir -p \"${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}\"\n"
            f"cp -a /{RECOVERY_DIR}/. \"${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}/\" 2>/dev/null || true\n"
            f"chmod 0755 \"${{DESTDIR}}{RECOVERY_INITRAMFS_DIR}/recovery_module.sh\" 2>/dev/null || true\n"
        )
        script = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            "PREREQ=\"cryptroot\"\n"
            "prereqs() { echo \"$PREREQ\"; }\n"
            "case \"$1\" in prereqs) prereqs; exit 0;; esac\n"
            "\n"
            "# cryptroot has already asked for the passphrase as many times as\n"
            "# crypttab allows. If it succeeded there is nothing to do here.\n"
            f"[ -b /dev/mapper/{mapper} ] && exit 0\n"
            f"[ -x {RECOVERY_INITRAMFS_DIR}/recovery_module.sh ] || exit 0\n"
            f"{RECOVERY_INITRAMFS_DIR}/recovery_module.sh </dev/console >/dev/console 2>&1\n"
        )
        return {
            "etc/initramfs-tools/hooks/poa-recovery": (hook, 0o755),
            "etc/initramfs-tools/scripts/local-top/poa-recovery": (script, 0o755),
        }

    def sources_list(self, config: InstallConfig, release: str, image_mountpoint: str = "") -> str:
        """What the installed system should use for packages afterwards.

        Never the image: it is mounted here, on the administrator's machine, and
        will not exist on the medium once it is unplugged. An installed system
        pointing at a dead file:// path fails every apt command with a confusing
        error, so the mirror is written when one is configured and a plain
        explanation when there is none."""
        if config.mirror:
            return (
                "# generated by portable-os-admin\n"
                f"deb {config.mirror} {release} main\n"
                f"deb {config.mirror} {release}-updates main\n"
            )
        return (
            "# generated by portable-os-admin\n"
            "# Система установлена с локального образа; сетевой репозиторий не задан.\n"
            f"# Чтобы устанавливать пакеты, добавьте сюда адрес репозитория, например:\n"
            f"#   deb https://deb.debian.org/debian {release} main\n"
        )

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        """Installs packages inside the target, through eatmydata where possible.

        The wrapper is what makes a long install bearable, and it has to run
        *inside* the chroot to work at all. `maybe_eatmydata` prefixes the
        host's copy, which is right for debootstrap but useless for anything
        run in the target when the architectures differ: measured on this
        project's own arm64→amd64 run, every package logged

            ld.so: object 'libeatmydata.so' from LD_PRELOAD cannot be
            preloaded ... ignored

        because an arm64 library cannot be preloaded into an amd64 process. So
        the accelerator was silently doing nothing on exactly the runs that
        need it most, and the install sat in D-state waiting on fsync -- CPU
        idle, load average 5, everything blocked on a USB stick behind dm-crypt.

        Resolved in the target, at run time, rather than decided here: the
        package may not be installed yet the first time this runs, so the shell
        checks and falls back. `force-unsafe-io` in temporary_files() covers
        dpkg's own writes; this covers apt and the maintainer scripts, which
        dpkg's setting does not reach.
        """
        if not packages:
            return None
        quoted = " ".join(shlex.quote(name) for name in packages)
        script = (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            'if [ -x /usr/bin/eatmydata ]; then run="eatmydata"; else run=""; fi; '
            f"exec $run apt-get install -y --no-install-recommends {quoted}"
        )
        return self.chroot(mount_root, "sh", "-c", script)

    def local_package_install_command(self, mount_root: str,
                                      filenames: list[str]) -> list[str] | None:
        """`apt-get install ./file.deb` -- deliberately apt and not dpkg.

        apt reads the .deb's dependencies and pulls them from the sources
        configured in the target (the installation image), which is what makes
        a carried-in package usable at all. The leading `./` is required: apt
        treats a bare name as a package name from the archive.
        """
        if not filenames:
            return None
        paths = " ".join(
            shlex.quote(f"/{self.EXTRA_PACKAGE_DIR}/{name}") for name in filenames
        )
        script = (
            "set -e; "
            "export DEBIAN_FRONTEND=noninteractive; "
            'if [ -x /usr/bin/eatmydata ]; then run="eatmydata"; else run=""; fi; '
            f"exec $run apt-get install -y --no-install-recommends {paths}"
        )
        return self.chroot(mount_root, "sh", "-c", script)

    def package_refresh_command(self, mount_root: str) -> list[str]:
        return self.chroot(mount_root, "apt-get", "update")

    def package_cleanup_command(self, mount_root: str) -> list[str] | None:
        """Empties the download cache and the package index off the medium.

        `apt-get clean` removes the .deb files; the lists under
        /var/lib/apt/lists are the index of a repository that no longer exists
        for this medium anyway -- it was the installation image, mounted on the
        administrator's machine and gone once the stick is unplugged. Keeping
        either only costs space on the medium.
        """
        script = (
            "apt-get clean >/dev/null 2>&1 || true; "
            "rm -rf /var/lib/apt/lists/* >/dev/null 2>&1 || true; "
            "exit 0"
        )
        return self.chroot(mount_root, "sh", "-c", script)

    def firmware_install_command(self, mount_root: str, image_mountpoint: str) -> list[str] | None:
        """Installs the non-free device firmware carried on the Debian image.

        This is not a nicety. A Debian installation image publishes only `main`
        and `contrib` in its index -- checked on debian-13.6.0-amd64-DVD-1.iso,
        whose Release names exactly those two -- so `firmware-iwlwifi`,
        `firmware-realtek`, `firmware-amd-graphics` and the rest are simply not
        installable by name, and a medium built from it comes up on unfamiliar
        hardware with no wireless and, on AMD graphics, no acceleration.

        The image does carry them: 36 .deb files under /firmware, outside any
        repository index. They are installed straight from there with dpkg,
        which works because the installer bind-mounts the image inside the
        target at the same path it has outside (see installer.py).

        Three deliberate decisions:

        - each package is installed on its own and failure is ignored. Firmware
          is an improvement to hardware support, never a precondition for
          booting, so one bad package must not end an hour-long install.
        - `--no-triggers`, with the triggers run **once** at the end. Without
          it every firmware package fires initramfs-tools' trigger and the
          initramfs is rebuilt from scratch -- roughly thirty times in a row.
          Observed on this project's own run: the step sat there with
          `initramfs-tools.postinst triggered update-initramfs` between
          packages, each rebuild costing minutes under emulation. The initramfs
          this program actually ships is built later, deliberately, once the
          crypttab and the recovery hooks are in place.
        - `*-installer_*` packages are skipped. Those (b43-installer and
          friends) are not firmware but scripts that *download* firmware during
          configuration, which in the isolated network this project is built
          for means hanging on a fetch that cannot succeed.
        """
        if not image_mountpoint:
            return None
        firmware_dir = f"{image_mountpoint}/firmware"
        script = (
            f'dir="{firmware_dir}"; '
            'if [ ! -d "$dir" ]; then '
            'echo "poa: на образе нет каталога прошивок, шаг пропущен"; exit 0; fi; '
            'installed=0; skipped=0; failed=0; '
            'for deb in "$dir"/*.deb; do '
            '[ -e "$deb" ] || continue; '
            'case "$deb" in *-installer_*) skipped=$((skipped+1)); continue;; esac; '
            'if dpkg --no-triggers -i "$deb" >/dev/null 2>&1; then installed=$((installed+1)); '
            'else failed=$((failed+1)); fi; '
            'done; '
            # Once, after all of them -- not once per package.
            'dpkg --triggers-only --pending >/dev/null 2>&1 || true; '
            # And then let apt resolve what dpkg could not.
            #
            # `dpkg -i` installs a package without satisfying its dependencies,
            # leaving it half-installed -- and a single half-installed package
            # makes every later apt command refuse to run with "Unmet
            # dependencies". Found the hard way: `intel-microcode` ships in the
            # image's /firmware directory and depends on `iucode-tool`, which
            # does *not*, so the install died two steps later, 105 minutes in,
            # while installing the kiosk's own applications.
            #
            # The dependency is available -- it is in the image's package index,
            # just not in that directory -- so apt fetches it from the
            # repository the install is already using.
            'env DEBIAN_FRONTEND=noninteractive apt-get -y --fix-broken install '
            '>/dev/null 2>&1 || true; '
            'broken=$(dpkg -l | awk \'$1 !~ /^ii/ && $1 ~ /^[a-z]/\' | wc -l); '
            'echo "poa: прошивки — установлено $installed, пропущено $skipped, '
            'с ошибкой $failed, осталось ненастроенных $broken"; '
            'exit 0'
        )
        return self.chroot(mount_root, "sh", "-c", script)

    # ------------------------------------------------------------------
    # Unattended updates, run on the medium itself
    # ------------------------------------------------------------------
    def update_refresh_command(self) -> list[str]:
        return ["apt-get", "update"]

    def update_upgrade_command(self, *, include_kernel: bool) -> list[str]:
        return [
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "-y",
            # Nobody is at the keyboard to answer a config-file prompt, and a
            # dpkg waiting on one blocks until the timeout kills it partway
            # through. Keeping the medium's own files is the right default
            # here: this project wrote several of them (fstab, crypttab, the
            # kiosk lockdown), and letting a package replace those would undo
            # the install.
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            "upgrade",
        ]

    def update_hold_commands(self, *, include_kernel: bool) -> list[str]:
        if include_kernel:
            # Explicitly unheld, so a medium whose configuration is changed
            # from "не трогать ядро" to "обновлять всё" actually starts
            # receiving kernel updates instead of staying pinned forever.
            return [
                'log "ядро включено в обновление"',
                "apt-mark unhold 'linux-image-*' 'grub-efi-*' >/dev/null 2>&1",
            ]
        return [
            'log "ядро и загрузчик исключены из обновления"',
            "apt-mark hold 'linux-image-*' 'grub-efi-*' >/dev/null 2>&1",
        ]

    def temporary_files(self) -> dict[str, tuple[str, int | None]]:
        return {
            # Keeps apt from starting the daemons it installs. Without it,
            # installing openssh-server tries to launch sshd inside the chroot
            # on the administrator's own machine.
            "usr/sbin/policy-rc.d": ("#!/bin/sh\nexit 101\n", 0o755),
            # dpkg fsync()s every unpacked file by default. On USB flash that
            # dominates the install time -- measured in hours rather than
            # minutes on a slow stick. debian-installer disables it for exactly
            # this reason and restores it afterwards, which is what the
            # temporary_files mechanism does here.
            "etc/dpkg/dpkg.cfg.d/poa-force-unsafe-io": ("force-unsafe-io\n", None),
        }

    def boot_packages(self, config: InstallConfig) -> list[str]:
        arch = self.normalize_arch(config.arch)
        packages = [
            # First, deliberately: it is installed in the same transaction as
            # the kernel, and every package command after that one picks it up
            # (see package_install_command). On a cross-architecture build the
            # desktop step alone is several hundred packages of fsync.
            "eatmydata",
            f"linux-image-{arch}",
            f"grub-efi-{arch}",
            "initramfs-tools",
            "cryptsetup",
            # cryptsetup-initramfs is the package that actually puts the LUKS
            # unlocking logic into the initramfs; without it the medium boots
            # to a kernel that cannot find its own encrypted root.
            "cryptsetup-initramfs",
            # openssl, because the recovery module derives the recovery
            # passphrase with scrypt and nothing else in a boot
            # environment can do that. The initramfs hook copies it in.
            "openssl",
            # Экран загрузки. До 01.09.2026 его на Debian не было вовсе -- не
            # «не собирался», а не из чего было собирать: plymouth в наборе не
            # значился. Носитель спрашивал пароль консолью ядра, а она не
            # знает кириллицы и показывает русский текст блоками.
            "plymouth",
            # label-pango.so, которым рисуется текст. Штатный хук plymouth
            # копирует его в initramfs сам -- но только если пакет есть:
            # иначе он честно пишет предупреждение и оставляет тему немой.
            "plymouth-label",
            # fc-match и fc-cache, которыми тот же хук выбирает и кладёт
            # шрифт. Без fontconfig он не находит ничего, и текст рисуется
            # прямоугольниками -- ровно то, на чём стоял РЕД ОС до 31.08.2026.
            # Полагаться на то, что fontconfig придёт зависимостью Xorg,
            # нельзя: экран загрузки нужен и консольному профилю, где Xorg нет.
            "fontconfig",
            # `locale-gen`, которым собирается локаль. В Debian её надо
            # именно собрать -- в образе её нет, -- и до 01.09.2026 сборка
            # звала `locale-gen`, не поставив пакет, который его приносит.
            # Прогон Debian + KDE встал ровно здесь: «chroot: не удалось
            # выполнить команду `locale-gen`: No such file or directory».
            #
            # Дефект жил незамеченным, потому что Debian не собирали с
            # 21.08.2026: у РЕД ОС локали приходят готовыми пакетами
            # glibc-langpack-*, и шага сборки там нет вовсе.
            "locales",
            # Шрифты консоли и `setupcon`, которым они попадают в initramfs.
            # В Debian шрифты лежат не в kbd (там их нет вовсе -- проверено по
            # kbd 2.7.1), а в console-setup-linux, который приходит
            # зависимостью console-setup.
            "console-setup",
            self.syslog_package,
        ]
        # sudo -- только если о правах просят. В индексе Debian у него
        # приоритет optional, то есть debootstrap его не ставит, и до
        # 01.09.2026 не ставили и мы: `usermod -aG sudo` выполнялся, а самой
        # программы в системе не было. Администратор просил права для
        # дополнительной учётной записи и получал членство в группе, за
        # которой ничего нет.
        if self.needs_sudo(config):
            packages.append("sudo")
        return packages

    def locale_generation_commands(self, mount_root, config) -> list[list[str]]:
        """В Debian локаль надо собрать: в образе её нет.

        Без этого LANG указывает на локаль, которой в системе не существует, и
        всё молча откатывается к C — то есть заданный язык просто не работает,
        а сообщения об этом нет.
        """
        locale = config.localisation.locale
        return [
            self.chroot(
                mount_root, "sh", "-c",
                f"grep -q '^{locale} ' /etc/locale.gen 2>/dev/null || "
                f"printf '%s UTF-8\\n' '{locale}' >> /etc/locale.gen",
            ),
            self.chroot(mount_root, "locale-gen"),
            # В Debian systemd читает /etc/default/locale, а не только
            # /etc/locale.conf.
            self.chroot(
                mount_root, "sh", "-c", f"printf 'LANG=%s\\n' '{locale}' > /etc/default/locale"
            ),
        ]

    def initramfs_command(self, mount_root: str) -> list[str]:
        # Универсальность здесь задаётся не ключом команды, а настройкой
        # MODULES=most, которую кладёт generic_initramfs_files: initramfs-tools
        # читает её при каждой пересборке, в том числе на самом носителе.
        return self.chroot(mount_root, "update-initramfs", "-u", "-k", "all")

    def generic_initramfs_files(self) -> dict[str, tuple[str, int | None]]:
        return {
            "etc/initramfs-tools/conf.d/poa-generic.conf": (
                "# generated by portable-os-admin\n"
                "# Носитель переносной: образ должен содержать драйверы для\n"
                "# машины, которой мы не видели, а не для той, где он собран.\n"
                "MODULES=most\n",
                None,
            )
        }

    def bootloader_commands(
        self, layout: MediaLayout, config: InstallConfig, *, overlay_root: bool = False
    ) -> list[list[str]]:
        # Меню пишется своё, когда корень собран из образа: `update-grub`
        # первым делом спрашивает `grub-probe`, на каком устройстве лежит
        # корень, а у overlay устройства нет. См. `grub_config_script`.
        write_menu = (
            ["sh", "-c", self.grub_config_script(layout, config, "/boot/grub/grub.cfg")]
            if overlay_root
            else ["update-grub"]
        )
        return [
            self.chroot(
                layout.mount_root,
                "grub-install",
                f"--target={self.grub_target(config.arch)}",
                "--efi-directory=/boot/efi",
                "--boot-directory=/boot",
                # --removable writes EFI/BOOT/BOOTAA64.EFI (the path firmware
                # looks for on a removable disk), and --no-nvram keeps
                # grub-install from registering a boot entry in the *admin
                # machine's* firmware for a stick that is about to be unplugged.
                "--removable",
                "--no-nvram",
                "--recheck",
            ),
            self.chroot(layout.mount_root, *write_menu),
        ]

    #: В Debian загрузчик собирается на месте, поэтому «сохранённого» пункта
    #: меню и BLS здесь нет.
    GRUB_EXTRA_DEFAULTS = (
        "GRUB_DEFAULT=0",
        'GRUB_DISTRIBUTOR="Portable OS"',
    )

    def config_files(self, config: InstallConfig, layout: MediaLayout, image=None) -> dict[str, str]:
        files = super().config_files(config, layout, image)
        files["etc/default/grub"] = self.grub_defaults(config, layout)
        return files
