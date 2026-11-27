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
  releases. The employee kiosk masks the service during image assembly.
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

import shlex
from urllib.parse import urlsplit

from ...config.schema import InstallConfig
from ...errors import InstallError
from .. import kiosk as kiosk_mode
from .. import machine_binding
from ..medium_initrd import SOURCE_ENV
from ..disk import MediaLayout
from .. import overlay as overlay_mode
from .. import repo_metadata
from .base import RECOVERY_DIR, RECOVERY_INITRAMFS_DIR, InstallBackend

# NOT VERIFIED ON REAL HARDWARE: the kernel/bootloader half of this backend has
# never been executed against a real RedOS host. It follows RHEL-family
# conventions and the package names exist in RedOS's own 8.0 repository
# metadata, but grub2-install/dracut behaviour on RedOS specifically is
# unconfirmed -- unlike the Debian backend, which has been run end to end.


class RedOSInstallBackend(InstallBackend):
    name = "redos"
    ssh_service = "sshd"
    sudo_group = "wheel"
    # `keyscript=` -- интерфейс cryptsetup-initramfs, которого в RHEL нет.
    # Здесь том открывает хук dracut из модуля poa-machine, и опция в crypttab
    # была бы либо проигнорирована, либо отвергнута systemd-cryptsetup.
    crypttab_keyscript = False
    # RED OS's image carries sddm and gdm but no lightdm. sddm is the lighter
    # of the two and needs no session bus of its own to autologin.
    display_manager_service = "sddm"

    EXTRA_TOOLS = ("dnf",)

    #: `rpm -ql` печатает содержимое установленного пакета -- то, чем в этом
    #: семействе отвечают на вопрос «какие запускатели принёс этот пакет».
    PACKAGE_FILES_COMMAND = ("rpm", "-ql")

    TITLE = "РЕД ОС (dnf --installroot)"
    PROVENANCE = "Проверено против repo1.red-soft.ru (выпуски 7.3 и 8.0)."
    CERTIFIED_RELEASE = "8.0c"
    RELEASES = (CERTIFIED_RELEASE, "8.0", "7.3")
    DEFAULT_RELEASE = CERTIFIED_RELEASE
    CERTIFIED_REPO_HOSTS = ("repo1.red-soft.ru", "mirror.yandex.ru")
    CERTIFIED_KEY = "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"
    _image_repos: tuple[repo_metadata.Repo, ...] | None = None
    _certified_repos: tuple[tuple[str, str, str], ...] | None = None

    def default_mirror(self, release: str = "", arch: str = "") -> str:
        """Адрес РЕД ОС включает и выпуск, и архитектуру.

        Раскладка репозитория -- `<выпуск>/<арх>/os`, проверена на 7.3 и 8.0.
        Пустые аргументы заменяются умолчаниями, чтобы вызов без них давал
        рабочий адрес, а не обрубок пути.
        """
        release = release or self.DEFAULT_RELEASE
        arch = self.ARCH_NAMES.get(arch or "amd64", "x86_64")
        return f"https://repo1.red-soft.ru/redos/{release}/{arch}/os"

    def prepare_package_sources(
        self, config: InstallConfig, *, image_mountpoint: str = "", image_release: str = "", network_source: str = ""
    ) -> None:
        """Проверяет редакцию и источники до развёртывания корня."""
        arch = self.normalize_arch(config.arch)
        self._image_repos = repo_metadata.discover_repos(image_mountpoint, arch) if image_mountpoint else ()
        self._certified_repos = None
        prefix = f"/redos/{self.CERTIFIED_RELEASE}/"
        certified = self.CERTIFIED_RELEASE in (config.release, image_release) or any(
            urlsplit(url).path.startswith(prefix)
            for url in [network_source, *(repo.baseurl for repo in self._image_repos)]
        )
        if not certified:
            return

        base_path = f"{prefix}{arch}/"
        channels = {urlsplit(repo.baseurl).path.rstrip("/"): repo for repo in self._image_repos}
        if base_path + "os" not in channels:
            raise InstallError("для РЕД ОС 8 Сертифицированной редакции нужен её доверенный установочный образ")

        self._certified_repos = ()
        if not config.network_packages:
            return

        mirror = config.mirror.strip().rstrip("/")
        # Сохранённый по умолчанию адрес 8.0 не должен менять редакцию выбранного ISO.
        if not mirror or mirror in {self.default_mirror("8.0", arch), self.default_mirror("8.0")}:
            mirror = self.default_mirror(self.CERTIFIED_RELEASE, arch)
        allowed_mirrors = {f"https://{host}{base_path}os" for host in self.CERTIFIED_REPO_HOSTS}
        if mirror not in allowed_mirrors:
            raise InstallError("для РЕД ОС 8 Сертифицированной редакции укажите официальное HTTPS-зеркало 8.0c")

        repos = []
        for channel in ("os", "updates"):
            repo = channels.get(base_path + channel)
            if repo is None or repo.gpgkey != self.CERTIFIED_KEY:
                raise InstallError("установочный образ РЕД ОС 8 не содержит настройки Base и Updates с ключом РЕД СОФТ")
            repos.append((f"poa-net-{repo.repo_id}", mirror.rsplit("/", 1)[0] + "/" + channel, repo.gpgkey))
        self._certified_repos = tuple(repos)

    firewall_service = "firewalld"
    bootloader_comes_from_packages = True
    #: RHEL-семейство слушает `rhgb`, а не `splash`.
    GRAPHICAL_BOOT_PARAMETER = "rhgb"

    #: Кириллица, латиница, греческий и иврит в одном файле -- то, что в этом
    #: семействе называется полным шрифтом консоли. Есть в kbd-misc образа
    #: РЕД ОС 8 (`LatArCyrHeb-16.psfu.gz`), и dracut кладёт в initramfs именно
    #: названный в vconsole.conf.
    CONSOLE_FONT = "LatArCyrHeb-16"

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
        # Сами шрифты консоли лежат отдельно от kbd: в собранном корне не было
        # ни одного, так что `setfont` (он в initramfs есть) кормить было
        # нечем. Проверено осмотром собранного корня 01.09.2026; kbd-misc
        # несёт 252 файла шрифтов, среди них кириллические.
        #
        # Кладёт он их в /usr/lib/kbd/consolefonts, а не в /usr/share/kbd/ --
        # проверено сборкой того же дня: 139 файлов, включая LatArCyrHeb-16.
        # Разница важна тому, кто пойдёт искать их руками.
        "kbd-misc",
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

    #: Чем РЕД ОС отличается от общего описания среды (install/desktop_roles.py).
    #:
    #: Одна строка -- столько и есть настоящих различий. До 02.09.2026 здесь
    #: лежали наборы целиком, и с Debian они совпадали десятью пакетами из
    #: двенадцати у MATE.
    DESKTOP_PACKAGE_OVERRIDES = {
        # Роли те же, что у Debian; имена совпадают, кроме апплета сети.
        "mate": {"network-applet": "network-manager-applet"},
    }



    # ------------------------------------------------------------------
    # Unattended updates, run on the medium itself
    # ------------------------------------------------------------------
    def package_cleanup_command(self, mount_root: str) -> list[str] | None:
        """Empties dnf's caches off the medium.

        `--installroot` rather than a chroot, to match how every other dnf call
        in this backend addresses the target.
        """
        return ["dnf", "--installroot", mount_root, "-y", "clean", "all"]

    def display_manager_files(self, config: InstallConfig) -> dict[str, str]:
        """SDDM autologin.

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
                # distribution's: the name stays the same on every release.
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
            "--setopt=poa-image.gpgcheck=0" if self._certified_repos is not None else "--nogpgcheck",
            "--forcearch", self.normalize_arch(config.arch),
            "install", *self.core_package_names(config),
        ]

    def core_package_names(self, config: "InstallConfig | None" = None) -> tuple[str, ...]:
        """Базовый набор минус то, чему в этой системе места нет.

        Кроме постоянного списка отсеивается sudo, когда о правах никто не
        просит. Смысл не в одном пакете: sudo требует `/usr/bin/vi`, и dnf
        закрывает это требование `vim-minimal`, то есть вместе с ним в систему
        приезжает редактор -- ровно то, чего определение минимальной базовой
        системы не допускает. Проверено сборкой 01.09.2026: вычеркнуть
        редактор списком не вышло, он вернулся зависимостью.

        `config` необязателен: без него набор считается как раньше, и это
        поведение для мест, где конфигурации под рукой нет.
        """
        unwanted = set(self.UNWANTED_PACKAGES)
        if config is not None and not self.needs_sudo(config):
            unwanted.add("sudo")
        return tuple(
            name for name in self._core_package_names()
            if name not in unwanted
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
            "    # module uses. cryptsetup перечислен явно: полагаться на модуль\n"
            "    # crypt тут было ошибкой -- в systemd-режиме он кладёт\n"
            "    # `systemd-cryptsetup`, а не утилиту. Найдено прогоном\n"
            "    # 28.08.2026; до него ни одна команда этого модуля не могла\n"
            "    # выполниться, и молчал он об этом так же, как хук привязки.\n"
            # awk -- для таблицы готовых строк запроса: модуль ищет в ней
            # строку по слоту и виду, а не считает base32 сам.
            "    inst_multiple cryptsetup openssl od stty sha256sum awk grep\n"
            # Графический экран загрузки собирается здесь, потому что модуль
            # plymouth сам его не доносит. Проверено осмотром initramfs
            # 29.08.2026: `two-step.so` он копирует, а `label.so` -- нет, и
            # без него графический режим не может показать ни строчки, так
            # что plymouth молча откатывается в текстовый. Экран при этом
            # выглядит работающим -- он просто рисуется консольным шрифтом,
            # который кириллицы не знает, и весь русский текст идёт блоками.
            '    for _poa_plugin in /usr/lib64/plymouth/label.so /usr/lib/plymouth/label.so; do\n'
            '        [ -e "$_poa_plugin" ] && inst_simple "$_poa_plugin"\n'
            "    done\n"
            # Шрифт -- по той же причине: в initramfs их нет вовсе, а plymouth
            # без шрифта рисует прямоугольники. DejaVu берётся потому, что он
            # есть в образе РЕД ОС и знает кириллицу; без него графика была бы
            # ровно так же нема, как консоль.
            '    for _poa_font in /usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf \\\n'
            '                     /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf; do\n'
            '        [ -e "$_poa_font" ] && inst_simple "$_poa_font"\n'
            "    done\n"
            # Одного файла шрифта мало. plymouth ищет шрифт через Pango, а тот
            # -- через fontconfig, по имени «DejaVu Sans». Без правил и кэша
            # fontconfig имя не разрешается ни во что, текст не рисуется вовсе,
            # и plymouth уходит в текстовый режим -- туда, где кириллицы нет.
            # Осмотр initramfs 29.08.2026: Pango на месте, `/etc/fonts` пуст.
            '    for _poa_fc in /etc/fonts/fonts.conf /etc/fonts/conf.d; do\n'
            '        [ -e "$_poa_fc" ] && inst_multiple -o "$_poa_fc"\n'
            "    done\n"
            '    [ -d /var/cache/fontconfig ] && inst_multiple -o /var/cache/fontconfig/*\n'
            "    inst_hook pre-mount 99 \"$moddir/poa-recovery-hook.sh\"\n"
            f"    mkdir -p \"${{initdir}}{RECOVERY_INITRAMFS_DIR}\"\n"
            f"    cp -a /{RECOVERY_DIR}/. \"${{initdir}}{RECOVERY_INITRAMFS_DIR}/\" 2>/dev/null || true\n"
            f"    chmod 0755 \"${{initdir}}{RECOVERY_INITRAMFS_DIR}/recovery_module.sh\" 2>/dev/null || true\n"
            "}\n"
        )
        hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            + SOURCE_ENV +
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
            # cryptsetup перечислен явно, и это не перестраховка. Модуль crypt
            # в systemd-режиме кладёт в образ `systemd-cryptsetup`, а саму
            # утилиту -- нет: найдено прогоном 28.08.2026, когда верная фраза
            # трижды подряд «не подошла». Каждый вызов падал с command not
            # found, а `if cryptsetup ...; then ... else` честно уходил в else,
            # так что ошибка выглядела как неверный пароль и ничем себя не
            # выдавала.
            "    inst_multiple cryptsetup sha256sum stty cut grep\n"
            # initqueue/settled, а не pre-mount, и это выяснилось прогоном
            # 28.08.2026: носитель не загрузился вовсе. dracut ждёт появления
            # корневого устройства заданием initqueue/finished/devexists-<uuid
            # корня>, а корень появляется только после расшифровки. pre-mount
            # же идёт ПОСЛЕ initqueue -- то есть хук, который единственный мог
            # открыть том, ждал шага, до которого его собственное бездействие
            # не пускало. Через две минуты initqueue уходил в
            # dracut-emergency с сообщением про несуществующий UUID.
            #
            # Тот же симптом уже ловили 26.08.2026, когда в командную строку
            # не попадал rd.luks.uuid. Привязка сняла эти параметры намеренно
            # (см. unlock_kernel_parameters) и вернула старый дефект: снимать
            # штатный механизм можно только вместе с переносом своего туда,
            # где он успевает сработать.
            #
            # settled -- после того как udev устаканился, то есть раздел LUKS
            # уже есть, и до истечения таймаута ожидания корня.
            '    inst_hook initqueue/settled 50 "$moddir/poa-machine-unlock.sh"\n'
            # Путь наружу обязан лежать там же. Модуль восстановления сам
            # висит на pre-mount, а при включённой привязке до pre-mount не
            # доходят: initqueue не пропускает дальше, пока тома нет. Значит
            # обещание «не удалось открыть -- объясним, что делать» держится
            # только если объяснять тоже в initqueue, сразу после попытки.
            # Ставится отсюда, а не из модуля восстановления, потому что без
            # привязки том открывает systemd-cryptsetup, и хук, сработавший
            # раньше ввода пароля, спрашивал бы код восстановления у того, кто
            # ещё не ошибся ни разу.
            '    inst_hook initqueue/settled 99 "$moddir/poa-machine-rescue.sh"\n'
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
        # Полный ID, а не хвост имени отображения: с приставкой администратора
        # в ID появился дефис (см. `MediaLayout.media_id`).
        media_id = getattr(layout, "media_id", "") or layout.mapper_name.removeprefix("poa-")
        register_hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            "# Регистрация идёт после того, как том открыт, и до передачи\n"
            "# управления системе: заголовок LUKS правится, пока это ещё можно\n"
            "# сделать, не мешая работающей системе.\n"
            f". {RECOVERY_INITRAMFS_DIR}/env 2>/dev/null || true\n"
            "# Фраза дочитана -- дальше она не нужна никому, и оставлять её\n"
            "# нельзя: systemd переносит /run из initramfs в работающую систему,\n"
            "# то есть файл дожил бы до конца сеанса. Любой, кто получил бы root\n"
            "# в киоске, прочитал бы парольную фразу носителя, а из неё выводятся\n"
            "# ключи всех его машинных слотов. Через trap, а не строкой в конце:\n"
            "# выходов у регистрации несколько, и молчаливый -- тоже.\n"
            "trap 'rm -f /run/poa-passphrase' EXIT INT TERM\n"
            "POA_PASSPHRASE=$(cat /run/poa-passphrase 2>/dev/null || true)\n"
            "export POA_PASSPHRASE\n"
            f"{machine_binding.register_script(config, media_id)}"
        )
        rescue_hook = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            + SOURCE_ENV +
            "# Том всё ещё закрыт после попыток разблокировки -- значит фраза\n"
            "# не подошла, и дальше разговор ведёт модуль восстановления.\n"
            f"[ -b /dev/mapper/{layout.mapper_name} ] && exit 0\n"
            f"[ -x {RECOVERY_INITRAMFS_DIR}/recovery_module.sh ] || exit 0\n"
            f"{RECOVERY_INITRAMFS_DIR}/recovery_module.sh </dev/console >/dev/console 2>&1\n"
        )
        return {
            "usr/lib/dracut/modules.d/90poa-machine/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/90poa-machine/poa-machine-unlock.sh": (unlock_hook, 0o755),
            "usr/lib/dracut/modules.d/90poa-machine/poa-machine-rescue.sh": (rescue_hook, 0o755),
            "usr/lib/dracut/modules.d/90poa-machine/poa-machine-register.sh": (register_hook, 0o755),
            "etc/dracut.conf.d/90-poa-machine.conf": (
                "# generated by portable-os-admin\n"
                'add_dracutmodules+=" poa-machine "\n',
                0o644,
            ),
        }

    def sources_list(self, config: InstallConfig, release: str, image_mountpoint: str = "") -> str | None:
        """Ничего: готовая система обновляется образом целиком (LIVE-2.0, Э9),
        а не пакетами. Файлы репозиториев, которые ставит пакет выпуска
        (`redos-release`), остаются как есть -- dnf в киоске не запускается, и
        переписывать чужие верные файлы незачем."""
        return None

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
            # Для сертифицированной сборки исключение действует только на доверенный ISO.
            arguments.append("--nogpgcheck" if self._certified_repos is None else "--setopt=poa-image.gpgcheck=0")
        if self.install_release:
            arguments += ["--releasever", self.install_release]
        # Сетевые репозитории сборки (Э22). dnf запускается снаружи, поэтому
        # ключ указывается путём в целевом корне: файл ключа туда положил
        # пакет выпуска при развёртывании базовой системы. `--repo=` каждого --
        # потому что `--repo=poa-image` выше отключает всё, что не названо.
        for repo_id, url, gpgkey in self.network_repos:
            arguments += [f"--repofrompath={repo_id},{url}", f"--repo={repo_id}"]
            if self._certified_repos is not None:
                arguments += [f"--setopt={repo_id}.skip_if_unavailable=False", f"--setopt={repo_id}.sslverify=1"]
            elif repo_id in ("poa-mirror", "poa-mirror-updates"):
                arguments.append(f"--setopt={repo_id}.skip_if_unavailable=False")
            if gpgkey:
                arguments += [f"--setopt={repo_id}.gpgcheck=1", f"--setopt={repo_id}.gpgkey={gpgkey}"]
            if self._repo_ca_bundle:
                # УЦ машины + корни дистрибутива с ISO + проверенные ими
                # промежуточные (install/repo_trust.py).
                arguments += [f"--setopt={repo_id}.sslcacert={self._repo_ca_bundle}"]
        return arguments

    #: Пакеты доступа (Э8), имена сверены по образам (Э0).
    ACCESS_PACKAGES = ("openvpn", "freerdp", "python3-gobject", "gtk3", "gtk-vnc2", "gvnc", "webkit2gtk4.1",
                       "python3-cryptography")

    #: Политика Sequoia для dnf сборки, если подключены сетевые репозитории.
    #:
    #: rpm 4.20 на машине администратора (Debian 13) проверяет подписи библиотекой
    #: Sequoia со строгой политикой и **отвергает ключ RED SOFT**: его самоподпись
    #: сделана SHA-1 («No binding signature»), импорт падает с code 2, и сборка
    #: из сети стоит (найдено живой сборкой 01.10.2026). Сама РЕД ОС этот ключ
    #: принимает. Политика ниже принимает SHA-1 только для процесса dnf сборки --
    #: системная политика хоста не меняется. Проверено: подлинный пакет проходит,
    #: подменённый отвергается. Доверие к ключу -- ровно как у самой РЕД ОС.
    SEQUOIA_POLICY = (
        "[hash_algorithms]\n"
        'sha1.second_preimage_resistance = "always"\n'
        'sha1.collision_resistance = "always"\n'
    )
    _sequoia_policy_path: str = ""
    _repo_ca_bundle: str = ""

    def _dnf(self, mount_root: str, *command: str) -> list[str]:
        argv = ["dnf", "--installroot", mount_root, "-y", *self._repo_arguments(), *command]
        if self.network_repos and self._sequoia_policy_path:
            argv = ["env", f"RPM_SEQUOIA_CRYPTO_POLICY={self._sequoia_policy_path}", *argv]
        return argv

    def bind_network(self, config: InstallConfig, image_mountpoint: str, mount_root: str = "") -> None:
        """Официальные репозитории из `redos-release` образа или `mirror` компании.

        Вызывается после развёртывания базовой системы: ключ подписи к этому
        моменту лежит в целевом корне, и пути к нему переписываются под корень.
        """
        if not config.network_packages:
            self.network_repos = ()
            return
        arch = self.install_arch or self.normalize_arch(config.arch)
        root = mount_root

        def rooted(key: str) -> str:
            if key.startswith("file://") and root:
                return "file://" + root.rstrip("/") + key[len("file://"):]
            return key

        if not self._sequoia_policy_path:
            import tempfile
            fd, self._sequoia_policy_path = tempfile.mkstemp(prefix="poa-sequoia-", suffix=".config")
            with open(fd, "w", encoding="ascii") as handle:
                handle.write(self.SEQUOIA_POLICY)
        if self._certified_repos is not None:
            self.network_repos = tuple((repo_id, url, rooted(key)) for repo_id, url, key in self._certified_repos)
            self._bind_repo_trust(image_mountpoint)
            return

        official = self._image_repos
        if official is None:
            official = repo_metadata.discover_repos(image_mountpoint, arch) if image_mountpoint else ()
        mirror = self.company_mirror(config)
        if mirror:
            # Зеркало компании держит те же пакеты, подписанные тем же ключом
            # дистрибутива, -- поэтому ключ берётся у официальных репозиториев.
            # В `mirror` у РЕД ОС -- полный адрес до `<выпуск>/<арх>/os`
            # (`default_mirror`); обновления лежат рядом, в `updates`.
            key = rooted(official[0].gpgkey) if official else ""
            repos = [("poa-mirror", mirror, key)]
            if mirror.endswith("/os"):
                repos.append(("poa-mirror-updates", mirror[:-len("/os")] + "/updates", key))
            self.network_repos = tuple(repos)
            self._bind_repo_trust(image_mountpoint)
            return
        self.network_repos = tuple(
            (f"poa-net-{repo.repo_id}", repo.baseurl, rooted(repo.gpgkey)) for repo in official
        )
        self._bind_repo_trust(image_mountpoint)

    def _bind_repo_trust(self, image_mountpoint: str) -> None:
        """Набор УЦ для TLS сетевых репозиториев -- см. `install/repo_trust.py`."""
        from .. import repo_trust

        urls = [f"{url.rstrip('/')}/repodata/repomd.xml" for _id, url, _key in self.network_repos]
        self._repo_ca_bundle = repo_trust.build_bundle(urls, image_mountpoint) if urls else ""

    def system_upgrade_command(self, mount_root: str) -> list[str] | None:
        if not self.network_repos:
            return None
        return self._dnf(mount_root, "upgrade")

    def old_kernels_command(self, mount_root: str) -> list[str] | None:
        """Пакеты, которым принадлежат каталоги модулей, кроме самого нового ядра.

        Не `dnf remove --oldinstallonly`: он оставляет «работающее» ядро (в
        chroot это ядро хоста), а в РЕД ОС installonly -- только мета-пакет
        `kernel-lt`; `kernel-lt-core` и `-modules` с 430 МБ модулей остаются
        (проверено на сборке 02.10.2026). Зависящее от удаляемых dnf снимает
        сам. Одно ядро -- не ошибка: удалять нечего.
        """
        if not self.network_repos:
            return None
        root = shlex.quote(mount_root)
        # Имена берутся только из успешного `rpm -qf`: текст ошибки («не
        # принадлежит ни одному пакету»), попав в dnf remove, совпадал с
        # чужими пакетами -- так проверка на стенде чуть не сняла grub2.
        # Бесхозное, что остаётся от старых ядер (modules.dep и т.п.), -- прочь.
        env = (["env", f"RPM_SEQUOIA_CRYPTO_POLICY={self._sequoia_policy_path}"]
               if self._sequoia_policy_path else [])
        return [*env, "sh", "-c",
                f"cd {root}/usr/lib/modules || exit 0; "
                "latest=$(ls | sort -V | tail -n 1); old=; "
                'for d in *; do [ "$d" = "$latest" ] && continue; '
                f"p=$(rpm --root {root} -qf --qf '%{{NAME}}-%{{VERSION}}-%{{RELEASE}}.%{{ARCH}}\\n' "
                '/usr/lib/modules/"$d" 2>/dev/null) && old="$old $p"; done; '
                f"if [ -n \"$old\" ]; then dnf --installroot {root} -y --disablerepo='*' remove $old || exit 1; fi; "
                'for d in *; do [ "$d" = "$latest" ] || rm -rf -- "$d"; done']

    def package_install_command(self, mount_root: str, packages: list[str]) -> list[str] | None:
        if not packages:
            return None
        return self._dnf(mount_root, "install", *packages)

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
        return self._dnf(mount_root, "install", *paths)

    #: Пакеты, которые убираются из состава, даже если их назвал сам образ.
    #:
    #: dracut-config-rescue кладёт на носитель второй initramfs — замер на
    #: собранном носителе: 138 МБ при 974 МБ раздела /boot, то есть больше
    #: седьмой части. Для переносного носителя он бесполезен по смыслу:
    #: систему, которая не загружается, чинят не с неё же, а с другого
    #: носителя или из режима восстановления, который у нас свой.
    #:
    #: Остальные четыре — прямое нарушение определения минимальной базовой
    #: системы, приехавшее из группы `core` самого образа: mc, nano и
    #: vim-minimal числятся там mandatory, htop — default. Найдено осмотром
    #: собранного корня 01.09.2026, причём в тот же день, когда из состава
    #: убирали caja: файловый менеджер выносили через дверь, а он стоял в
    #: соседней комнате.
    #:
    #: Опаснее всех mc: это сразу файловый менеджер, редактор и командная
    #: строка, то есть готовый трамплин для всякого, кто найдёт хоть один
    #: способ выполнить программу. Тремя другими закрывается остаток
    #: определения: редактора и системного монитора пользователю не положено.
    #:
    #: Ни один из четырёх ничем не требуется — проверено `rpm -q
    #: --whatrequires` по собранному корню, — поэтому удаление ничего не
    #: тянет за собой. `less` оставлен намеренно: его требует man-db, а сам он
    #: ни файлами не управляет, ни текст не правит.
    #:
    #: **`vim-minimal` из этого списка уходит не весь.** Проверено сборкой
    #: 01.09.2026: из состава он вычёркивается, но возвращается зависимостью —
    #: `sudo` требует `/usr/bin/vi`, и dnf закрывает это требование им же.
    #: Значит редактор в системе останется, пока в ней есть sudo, и убрать его
    #: списком нельзя: вопрос не в списке, а в том, нужен ли киоску sudo
    #: вообще. Имя оставлено здесь, чтобы оно не приезжало ещё и явным
    #: пунктом группы `core`.
    UNWANTED_PACKAGES = (
        "dracut-config-rescue",
        "mc",
        "nano",
        "vim-minimal",
        "htop",
    )

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
            # Корень -- образ слота под dm-verity (Э3b); в семействе Fedora
            # veritysetup -- отдельный пакет, в образ сам не приходит.
            "veritysetup",
            # attr (setfattr) -- метка SELinux корня на tmpfs верхнего слоя в
            # initramfs. Без пакета `inst_multiple -o` молча пропускал утилиту,
            # и `/` получал unlabeled_t (журнал носителя: «метка корня не
            # поставлена», живая проверка 03.10.2026).
            "attr",
            # openssl, because the recovery module derives the recovery
            # passphrase with scrypt and nothing else in a boot
            # environment can do that. The initramfs hook copies it in.
            "openssl",
            # curl -- им проверка отзыва спрашивает ресурс. Его отсутствие --
            # самый тихий отказ из возможных: носитель исправен на вид,
            # проверка не срабатывает ни разу.
            "curl",
            # notify-send -- им служба отзыва и шлюз сеанса говорят
            # пользователю, что связи с сервером авторизации нет. У РЕД ОС
            # программа лежит в самом `libnotify`, отдельного пакета нет.
            "libnotify",
            # Корневые сертификаты для HTTPS до сервера отзыва. У РЕД ОС их
            # тянет openssl-libs (сверено по образу 15.09.2026), но названы
            # явно: у Debian они приходили лишь рекомендацией и на носитель не
            # попали, и держаться на чужой зависимости здесь нельзя.
            "ca-certificates",
            # Графический экран загрузки -- то, чем носитель разговаривает с
            # человеком до запуска системы: спрашивает пароль, сообщает, что
            # компьютер незнаком, принимает код восстановления. Консоль ядра
            # для этого не годится: она не знает кириллицы и показывает
            # русский текст сплошными блоками.
            #
            # Плагины перечислены поимённо. Сам `plymouth` ставит только
            # `details`, `text` и `tribar`, а тема на них не построится:
            # `two-step` рисует фон и картинки, `label` -- текст, и без него
            # экран остаётся немым. Проверено осмотром собранного носителя
            # 29.08.2026 -- нужных плагинов там не оказалось.
            "plymouth",
            "plymouth-plugin-two-step",
            "plymouth-plugin-label",
            self.syslog_package,
        ]

    def initramfs_command(self, mount_root: str) -> list[str]:
        """Пересборка initramfs с явным путём вывода, по ядру за раз.

        `--regenerate-all` отдавал выбор каталога самому dracut, а тот смотрит
        на то, что смонтировано: если `/boot/efi` -- точка монтирования, он
        пишет в `/boot/efi/<machine-id>/<версия>/initrd` (ветка с
        `mountpoint -q /boot/efi` в самом dracut). При сборке дистрибутива ESP
        не смонтирован, и образ ложился в `/boot`; при установке на носитель
        смонтирован -- и прогон 12.09.2026 умер на «Can't write to
        /boot/efi/769aebd…/6.12.21-1.red80.x86_64: Directory does not exist».

        Явный путь снимает зависимость от окружения: оба пути кладут образ
        туда, откуда его берёт запись загрузчика, -- `/boot/initramfs-<версия>.img`.

        Флаги те же и по тем же причинам: без `--no-hostonly` dracut соберёт
        образ под машину, на которой идёт сборка (chroot в чужой ВМ), а
        `--no-hostonly-cmdline` не даёт зафиксировать в образе командную
        строку окружения сборки -- параметры носителя приходят из записи
        загрузчика.
        """
        return self.chroot(
            mount_root, "sh", "-c",
            "set -e; "
            "for moddir in /lib/modules/*/; do "
            "kver=${moddir%/}; kver=${kver##*/}; "
            '[ -d "/lib/modules/$kver/kernel" ] || continue; '
            "dracut --force --no-hostonly --no-hostonly-cmdline "
            '"/boot/initramfs-$kver.img" "$kver"; '
            "done",
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

    def restore_labels_command(
        self, mount_root: str, paths: tuple[str, ...] = ("/etc",), upper: str = ""
    ) -> list[str] | None:
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
        # Сквозь overlay метить нельзя, и это стоило целого дня 12.09.2026.
        #
        # `setfiles`, запущенный по собранному корню, завершается **с кодом
        # ноль и не метит ничего**: файловую систему `overlay` он молча
        # пропускает. Поэтому ни один файл, дописанный развёртыванием, метки не
        # получал -- ни fstab, ни crypttab, ни настройка автовхода SDDM, -- и
        # носитель доходил до экрана входа, где и оставался: SDDM не мог
        # прочитать собственную конфигурацию.
        #
        # Лечится обходом overlay: метки ставятся прямо по верхнему слою, он
        # лежит на обычном ext4. `-r` говорит setfiles, что путь надо сверять с
        # политикой, отбросив этот префикс, -- иначе `/mnt/upper/etc/fstab` не
        # совпал бы ни с одним правилом. Проверено на носителе: fstab и
        # настройка SDDM получают `etc_t`.
        if upper:
            targets = " ".join(f"{upper}{path}" for path in paths)
            return self.chroot(
                mount_root, "sh", "-c",
                f"setfiles -F -r {upper} {self.FILE_CONTEXTS} {targets}"
                + ' || echo "poa: setfiles не смог расставить метки по слою изменений"',
            )

        # Отказ виден, а не проглочен. До 12.09.2026 здесь стояло
        # `2>/dev/null || true`, и это скрыло настоящий дефект: на носителе с
        # overlay ни один дописанный файл метки не получил -- ни fstab, ни
        # crypttab, ни настройка автовхода SDDM, -- а в журнале установки не
        # было ни строчки. Носитель доходил до экрана входа и дальше не шёл.
        #
        # Установку это по-прежнему не срывает: метки нужны системе, но
        # оборванное на этом месте развёртывание оставило бы носитель заведомо
        # непригодным, тогда как без части меток он хотя бы диагностируем.
        return self.chroot(
            mount_root, "sh", "-c",
            f"setfiles -F {self.FILE_CONTEXTS} " + " ".join(paths)
            + ' || echo "poa: setfiles не смог расставить метки — служба SDDM'
            ' и монтирование могут отказать"',
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
    GRUB_EDITENV = "grub2-editenv"

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

        Хуков два. `cmdline` объявляет, что корень вида `poa:<устройство>`
        законен и его берёт на себя этот модуль; `mount` собирает корень.
        Первая версия обходилась одним хуком `pre-pivot` и подменяла уже
        смонтированный корень — так же, как на Debian. На dracut это не
        работает, и молча: systemd монтирует контейнер, следом модуль
        `rootfs-block` размонтирует чужое монтирование, монтирует сам,
        проверяет корень на пригодность (в контейнере нет ни /proc, ни
        /sbin/init) и уходит в аварийную оболочку с «Can't mount root
        filesystem». Проверено загрузкой РЕД ОС + KDE 10.09.2026: до хука
        `pre-pivot` дело не доходило вовсе. Подробности у
        `overlay.ROOT_PREFIX`.

        Модули ядра перечисляются явно: dracut кладёт в образ то, что нужно
        найденной конфигурации, а overlay, squashfs и loop в неё не входят —
        без них скрипт не соберёт ничего.

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
            "    instmods overlay squashfs loop dm-verity\n"
            "}\n"
            "install() {\n"
            "    # setfattr -- метка корня на tmpfs верхнего слоя (LIVE-2.0).\n"
            "    # Обязательно, без -o: необязательная установка молча пропускала\n"
            "    # утилиту, если пакета attr не было.\n"
            "    inst_multiple setfattr\n"
            "    # Корень -- образ слота под dm-verity (Э3b).\n"
            "    inst_multiple veritysetup sed\n"
            '    inst_hook cmdline 30 "$moddir/poa-overlay-parse.sh"\n'
            '    inst_hook mount 90 "$moddir/poa-overlay-hook.sh"\n'
            "}\n"
        )
        value = overlay_mode.ROOT_VALUE
        flag = overlay_mode.ROOT_FLAG
        parse = (
            "#!/bin/sh\n"
            "# generated by portable-os-admin\n"
            "#\n"
            "# Корня в командной строке ядра нет: там стоит только признак\n"
            "# " + flag + "=1. Здесь корень объявляется -- для dracut и только для\n"
            "# него. Запись в /etc/cmdline.d видят последующие шаги dracut (они\n"
            "# читают её через getarg), а systemd -- нет: он смотрит в /proc/cmdline,\n"
            "# и монтировать ему по-прежнему нечего.\n"
            "type getarg > /dev/null 2>&1 || . /lib/dracut-lib.sh\n"
            'if getargbool 0 ' + flag + '; then\n'
            "    root=" + value + "\n"
            "    rootok=1\n"
            '    echo "root=' + value + '" > /etc/cmdline.d/99-poa-overlay.conf\n'
            "    # Ждать том обязательно: зашифрованный том появляется позже, а без\n"
            "    # ожидания очередь initramfs закончится раньше и хук монтирования\n"
            "    # не найдёт ничего.\n"
            "    " + SOURCE_ENV +
            f'    wait_for_dev "/dev/mapper/{layout.mapper_name}"\n'
            "fi\n"
        )
        body = overlay_mode.boot_script(
            layout.mapper_name, overlay_mode.ROOTMNT_DRACUT, mount_container=True, selinux=True
        )
        # Хук монтирования вызывается на любом носителе, где включён модуль, а
        # его первая строка -- проверка, что корень наш. Чужой корень (носитель
        # без образа, ремонтная загрузка) хук пропускает молча.
        guard = (
            "type getargbool > /dev/null 2>&1 || . /lib/dracut-lib.sh\n"
            "# `return`, а не `exit`: dracut исполняет хуки через `.`, в своей же\n"
            "# оболочке, и `exit` увёл бы в аварийный режим всю загрузку. `exit`\n"
            "# оставлен запасным на случай запуска скрипта самим по себе.\n"
            f"getargbool 0 {flag} || {{ return 0 2>/dev/null || exit 0; }}\n"
            + SOURCE_ENV
        )
        hook = body.replace(
            "# Здесь смонтированный контейнер подменяется собранным overlay.\n",
            "# Здесь смонтированный контейнер подменяется собранным overlay.\n" + guard,
            1,
        )
        return {
            "usr/lib/dracut/modules.d/50poa-overlay/module-setup.sh": (module_setup, 0o755),
            "usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-parse.sh": (parse, 0o755),
            "usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-hook.sh": (hook, 0o755),
            "etc/dracut.conf.d/50-poa-overlay.conf": (
                "# generated by portable-os-admin\n"
                "# dracut не включает модуль просто потому, что он есть.\n"
                'add_dracutmodules+=" poa-overlay "\n',
                None,
            ),
        }

    def overlay_root_arguments(self, layout: MediaLayout) -> str:
        """У dracut в командной строке корня нет вовсе — только признак.

        Любое `root=` здесь монтирует systemd, ещё до того как что-то сможет
        сделать наш хук, и корнем оказывается контейнер, который системой не
        является. Признак `rd.poa.overlay=1` systemd не трогает, а корень
        объявляет сам модуль. См. `overlay.ROOT_FLAG` и `overlay_boot_files`.
        """
        return f"{overlay_mode.ROOT_FLAG}=1"

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
        # `0`, а не `saved`: сохранённый выбор GRUB читает из `grubenv` рядом с
        # `grub.cfg`, то есть с раздела EFI, -- а наш `grubenv` лежит на
        # POA_BOOT, и на ESP его никто не кладёт. Загрузка 12.09.2026 показала
        # это первой же строкой на экране: «error: file `/EFI/redos/grubenv'
        # not found». Отказ не фатальный, GRUB берёт первую запись, но на
        # носителе-киоске запись ровно одна, а сообщение об ошибке человек
        # видит перед запросом пароля. Debian по той же причине ставит `0`.
        "GRUB_DEFAULT=0",
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

    def bootloader_commands(
        self, layout: MediaLayout, config: InstallConfig, *, overlay_root: bool = False
    ) -> list[list[str]]:
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
            # Меню: своё, когда корень собран из образа. `grub2-mkconfig`
            # спотыкается о то же, что и `update-grub` в Debian, -- он
            # спрашивает `grub-probe`, на каком устройстве корень, а у
            # overlay устройства нет. См. `grub_config_script`.
            self.chroot(root, "sh", "-c", self.grub_config_script(
                layout, config, f"/boot/efi/{self.EFI_VENDOR_DIR}/grub.cfg"))
            if overlay_root
            else self.chroot(root, "grub2-mkconfig", "-o", f"/boot/efi/{self.EFI_VENDOR_DIR}/grub.cfg"),
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
