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

from ...config.schema import ExtraUserConfig, InstallConfig
from .. import revocation
from .. import desktop_roles
from .. import kiosk as kiosk_mode
from .. import machine_binding
from ..disk import MediaLayout

# Пути и сроки переехали в `constants.py`: их берут генераторы текста
# (`backends/text/*`), и импортировать ради них класс бэкенда значило бы
# замкнуть круг. Здесь они остаются видимыми -- за эти имена держатся
# установщик и тесты, и сужать набор имён рефакторинг не вправе.
from .constants import (
    BOOT_TRIES,
    EXCHANGE_MOUNTPOINT,
    GRUB_ENV_DIR,
    LOG_MOUNTPOINT,
    PLYMOUTH_THEME_NAME,
    RECOVERY_DIR,
    RECOVERY_INITRAMFS_DIR,
    SERVICE_DEVICE_TIMEOUT,
    SERVICE_KEY_DIR,
    SERVICE_KEY_PATH,
)
from .text import plymouth as plymouth_text
from .text import tables as tables_text

__all__ = [
    "EXCHANGE_MOUNTPOINT",
    "InstallBackend",
    "LOG_MOUNTPOINT",
    "PLYMOUTH_THEME_NAME",
    "RECOVERY_DIR",
    "RECOVERY_INITRAMFS_DIR",
    "SERVICE_DEVICE_TIMEOUT",
    "SERVICE_KEY_DIR",
    "SERVICE_KEY_PATH",
    "host_arch",
    "maybe_eatmydata",
]


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
    #: Понимает ли семейство `keyscript=` в crypttab. Это интерфейс
    #: cryptsetup-initramfs, то есть Debian; в RHEL том открывает наш хук
    #: dracut, а systemd-cryptsetup такой опции не знает. Разделено потому,
    #: что crypttab один на оба семейства, и опция, написанная для одного,
    #: уезжала на носитель другого.
    crypttab_keyscript: bool = True
    #: Где initramfs семейства ищет crypttab корня -- туда его кладёт второй
    #: initrd носителя (Э2). None: семейство открывает корень иначе
    #: (dracut -- по `rd.luks.*` из командной строки или нашим хуком).
    initramfs_crypttab_path: str | None = None
    #: Non-empty means this family supports the kiosk profile at all; a kiosk
    #: configuration for a family whose value is empty is refused rather than
    #: half-built.
    display_manager_service: str = ""

    def display_manager_for(self, config: InstallConfig) -> str:
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

    #: Сетевые репозитории сборки (этап Э22): подключаются **после** того, как
    #: базовая система поставлена с образа, -- образ быстрее и не требует сети
    #: для начала, а всё дальнейшее, включая обновление до свежих версий, идёт
    #: уже с ними. Пусто -- сборка только с образа.
    network_repos: tuple = ()

    def bind_network(self, config: InstallConfig, image_mountpoint: str, mount_root: str = "") -> None:
        """Подключает сетевые репозитории сборки: `mirror` или официальные из образа."""
        self.network_repos = ()

    def system_upgrade_command(self, mount_root: str) -> list[str] | None:
        """Обновление всего уже поставленного до свежих версий из сети."""
        return None

    def old_kernels_command(self, mount_root: str) -> list[str] | None:
        """Удаление ядер, оставшихся от образа после обновления.

        LiveCD грузит одно ядро; прежнее -- сотни мегабайт модулей в каждом
        выпуске и на каждом носителе без всякой пользы: отката на него нет,
        откат -- это прежний выпуск целиком.
        """
        return None

    def company_mirror(self, config: InstallConfig) -> str:
        """`mirror`, если это репозиторий компании; пусто, если официальный.

        Поле `mirror` заполняется официальным адресом семейства по умолчанию
        (`default_mirror`), поэтому «репозиторий компании» -- только адрес,
        отличный от него. Официальный при сборке берётся из самого образа: он
        точнее (у РЕД ОС 8 образ -- выпуск 8.0c, а адрес по умолчанию -- 8.0).
        """
        def norm(url: str) -> str:
            return url.strip().rstrip("/").split("://", 1)[-1]

        if not config.mirror:
            return ""
        default = self.default_mirror(config.release, config.arch)
        return "" if norm(config.mirror) == norm(default) else config.mirror.strip().rstrip("/")

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

    #: Куда внутри собираемой системы кладутся принесённые пакеты. Внутрь, а не
    #: рядом: путь должен означать одно и то же по обе стороны chroot -- на этом
    #: уже обжигались с образом, чей file://-адрес снаружи существовал, а
    #: внутри нет.
    EXTRA_PACKAGE_DIR = "var/tmp/poa-extra-packages"

    def local_package_install_command(self, mount_root: str,
                                      filenames: list[str]) -> list[str] | None:
        """argv installing administrator-supplied package files into the target.

        Exists because a closed network is the normal case for this tool, and
        what an organisation needs there -- a VPN client, a remote-desktop
        client, an accounting terminal -- is exactly what no distribution
        repository carries. Without this the administrator's only option is to
        finish the medium and then install by hand on every one of them.

        Installed at *distribution build* time rather than at deployment:
        dependencies are resolved against the image, once, and every medium
        made from that distribution then holds the same set. Doing it per
        medium would resolve dependencies against whatever is available at that
        moment and make two media from one distribution differ.

        Resolving dependencies is the whole point, so this must be the package
        manager's install command and not `dpkg -i`/`rpm -i`: those place the
        file and leave the dependency unmet, which on Debian wedges apt for
        everything that follows (this project has already lost a 105-minute run
        to exactly that).
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

    def optional_packages(self, config: InstallConfig) -> list[str]:
        """Пакеты, без которых носитель собирается, но работает не полностью.

        Отличаются от `boot_packages` тем, что их отсутствие в образе не должно
        останавливать установку, и от firejail -- тем, что они не зависят от
        настроек: нужны любому носителю этого семейства. У общего бэкенда таких
        нет; называет их тот, у кого они есть.
        """
        return []

    #: Роли рабочей среды, которые у этого семейства называются иначе.
    #:
    #: `{"mate": {"display-manager": "lightdm"}}` читается как «менеджер входа у
    #: нас называется lightdm», а пустая строка -- как «этой роли у нас нет».
    #: Всё, что не названо здесь, берётся из общего описания в
    #: `install/desktop_roles.py`, и это главное: правка, найденная на одной
    #: системе, попадает в обе разом. До 02.09.2026 списки лежали в бэкендах
    #: целиком и совпадали на десять пакетов из двенадцати -- ровно та форма
    #: дублирования, которой потерялся пакет схем GSettings.
    DESKTOP_PACKAGE_OVERRIDES: dict[str, dict[str, str]] = {}

    #: Пакеты, которым роли нет: существуют у одного семейства и не имеют пары
    #: у другого. Список короткий намеренно -- растущий означает, что роль
    #: всё-таки есть и её надо назвать.
    DESKTOP_EXTRA_PACKAGES: dict[str, tuple[str, ...]] = {}

    def desktop_packages(self, config: InstallConfig) -> list[str]:
        """Packages the restricted desktop needs, or [] for a console system.

        Deliberately a hand-picked list rather than the distribution's desktop
        task/group: `task-mate-desktop` and RED OS's `mate-desktop` group each
        pull in around a hundred packages, among them a terminal, the MATE
        control centre, a system monitor and a text editor -- precisely the
        tools the kiosk exists to withhold. Not installing them is this mode's
        strongest restriction, so the list is chosen here and not delegated.

        Сам набор общий для семейств и собирается по ролям; бэкенду остаётся
        графическая часть, которая у дистрибутивов действительно своя --
        драйверы, прошивки, шрифты, -- и короткий список исключений.
        """
        if not kiosk_mode.is_enabled(config):
            return []
        desktop = kiosk_mode.desktop_of(config)
        packages = list(self.GRAPHICS_PACKAGES)
        packages += desktop_roles.packages_for(
            desktop,
            overrides=self.DESKTOP_PACKAGE_OVERRIDES,
            extras=self.DESKTOP_EXTRA_PACKAGES,
            blank_screen=config.kiosk.screen_blank_minutes > 0,
        )
        from ..access import files as access_files
        if access_files.is_enabled(config):
            packages += list(self.ACCESS_PACKAGES)
        return packages

    #: Пакеты доступа (Э8): OpenVPN, FreeRDP, GTK-привязки для окон, VTE (SSH),
    #: GtkVnc (VNC), криптография клиента. Имена у семейств свои -- Э0.
    ACCESS_PACKAGES: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Как этот дистрибутив называется и где лежит
    # ------------------------------------------------------------------
    #
    # Всё, что интерфейс должен знать о семействе, чтобы предложить его
    # администратору: подпись, чем проверено, какие выпуски существуют и куда
    # смотреть за пакетами. До 02.09.2026 это лежало словарями в самом мастере
    # конфигурации -- и третий дистрибутив потребовал бы правок в экране, а не
    # только в бэкенде. Здесь же ответ даёт тот, кто про дистрибутив и знает.

    #: Как семейство называется администратору.
    TITLE: str = ""

    #: Чем проверено -- строка под выбором. Не реклама, а происхождение:
    #: сказано, против какого репозитория проверялись имена пакетов.
    PROVENANCE: str = ""

    #: Выпуски, которые бэкенд умеет ставить, новейший первым.
    RELEASES: tuple[str, ...] = ()

    #: Выпуск по умолчанию для новой конфигурации.
    DEFAULT_RELEASE: str = ""

    def default_mirror(self, release: str = "", arch: str = "") -> str:
        """Откуда брать пакеты, если администратор не назвал своё зеркало.

        Аргументы нужны не всем: у Debian адрес один на все выпуски, у РЕД ОС
        он включает и выпуск, и архитектуру. Метод, а не строка, именно
        поэтому -- семейство само решает, из чего складывается его адрес.
        """
        return ""

    #: Чем спросить у менеджера пакетов, какие файлы принёс пакет.
    #:
    #: Выполняется уже внутри целевой системы, поэтому без `--root`: команда
    #: получает имя пакета аргументом и печатает пути по одному в строке.
    #:
    #: Нужно это затем, что список доступных пользователю приложений с
    #: 02.09.2026 не вводится руками, а выводится из пакетов, которые поставил
    #: администратор: что поставлено, то и доступно. Спросить менеджер пакетов
    #: -- единственный способ ответить точно: имя пакета и имя его
    #: desktop-записи совпадают далеко не всегда, а один пакет нередко ставит
    #: несколько запускателей сразу.
    PACKAGE_FILES_COMMAND: tuple[str, ...] = ()

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
        unlocking logic derived from the /etc/crypttab written below.

        Образ обязан быть **универсальным**, а не собранным под машину, где
        идёт сборка. Требование общее для обоих семейств, потому что общая у
        них причина: носитель переносной и поедет на машину, которой здесь
        никто не видел.

        Цена ошибки измерена: собранный по умолчанию образ РЕД ОС занял 21 МБ
        и не содержал ни `usb_storage`, ни `uas` — драйверов того самого
        накопителя, с которого он должен грузиться. Ядро не видело флешку,
        раздел LUKS не появлялся, и загрузка три минуты ждала устройство,
        после чего уходила в аварийный режим. По симптому — «нет устройства с
        таким UUID» — причину не угадать.

        Как это делается, у семейств разное (`--no-hostonly` у dracut,
        `MODULES=most` у initramfs-tools), поэтому команду даёт бэкенд. А вот
        настройку, которая заставляет оставаться универсальным и при
        последующих пересборках **на самом носителе**, кладёт
        `generic_initramfs_files`: без неё первое же автообновление на чужой
        машине сделало бы носитель незагружаемым на всех остальных.
        """

    def unlock_kernel_parameters(self, layout: MediaLayout, config: InstallConfig | None = None) -> list[str]:
        """Чем сказать initramfs, что корень зашифрован.

        Требование общее: носитель не загрузится, если образ не знает, что
        перед монтированием корня нужно расшифровать том. А вот способ у
        семейств разный, и разница не стилистическая.

        В RHEL-семействе это параметры ядра `rd.luks.*`: так пишет anaconda, и
        так же работает штатная установка. В Debian ту же роль играет
        cryptsetup-initramfs, который читает `/etc/crypttab` при сборке
        образа, поэтому параметры не нужны — и добавлять их там нечего.

        Возвращать пустой список — законный ответ, а не заглушка.
        """
        return []

    def kernel_cmdline(self, config: InstallConfig, layout: MediaLayout | None = None) -> str:
        """Командная строка ядра, кроме корня и режима монтирования.

        Собирается в одном месте на оба семейства: расходиться ей нельзя,
        потому что параметр, забытый в одной из копий, проявится только на
        живой загрузке носителя — и по симптому его не найти.
        """
        parts: list[str] = []
        if layout is not None:
            parts += self.unlock_kernel_parameters(layout, config)
        # Обособленность и защита памяти -- раньше того, что задал администратор:
        # так его собственные параметры видны в конце строки, где их и ищут.
        parts += kiosk_mode.HARDENING_KERNEL_PARAMETERS
        if config.boot.kernel_parameters.strip():
            parts.append(config.boot.kernel_parameters.strip())
        if config.boot.serial_console:
            # Порядок консолей -- не оформление. Ядро пишет во все названные,
            # но главной (`/dev/console`) делает **последнюю**: туда systemd и
            # plymouth отправляют запрос пароля LUKS. Когда для стенда в
            # параметры вписывали один `console=ttyS0,115200`, главной
            # становился порт, и на живой машине запрос уходил в него --
            # человек у экрана вводил пароль вслепую, без единой звёздочки.
            # Здесь `tty0` стоит последним и после параметров администратора,
            # поэтому экран остаётся главным при любом их содержимом.
            #
            # `plymouth.ignore-serial-consoles` -- обязательная пара к порту.
            # Увидев `console=ttyS*`, plymouth переходит в текстовый режим:
            # вместо нашей темы -- встроенный экран с растровым шрифтом без
            # кириллицы, а в подробном режиме запрос пароля не появлялся на
            # экране вовсе, пока не нажать Esc. Найдено живой загрузкой
            # 15.09.2026; параметр описан самим plymouth именно для этого.
            parts += ["console=ttyS0,115200", "console=tty0", "plymouth.ignore-serial-consoles"]
        # Графическая заставка включается всегда, а не вместе с `quiet`, -- и в
        # подробном режиме тоже.
        #
        # Это не оформление: ею носитель разговаривает с человеком до загрузки
        # системы -- спрашивает пароль, объясняет, что машина незнакома,
        # принимает код восстановления. Консоль ядра для этого не годится, она
        # не знает кириллицы и показывает русский текст блоками.
        #
        # Раньше параметр приписывался только при `boot.quiet`, то есть
        # графика пропадала ровно тогда, когда администратор хотел видеть
        # подробности загрузки, -- а это две независимые вещи. `quiet` убирает
        # сообщения ядра, заставка рисует экран пользователю.
        parts.append(self.GRAPHICAL_BOOT_PARAMETER)
        if config.boot.verbose:
            # Подробная загрузка главнее `quiet`, но заставку не снимает: без
            # неё носитель спрашивал бы пароль, привязку и код восстановления
            # консолью без кириллицы и без эха. Сообщения служб, ради которых
            # режим включают, видны по Esc поверх заставки и в журнале тома
            # журналов.
            parts.append("systemd.show_status=1")
        elif config.boot.quiet:
            parts.append("quiet")
        return " ".join(parts)

    #: Параметр, включающий графическую заставку загрузки. Он же решает, как
    #: выглядит запрос пароля LUKS: с ним plymouth рисует поле и показывает
    #: точки по мере ввода, без него запрос остаётся текстовым и **вовсе без
    #: эха** -- человек за носителем не видит, набралось ли хоть что-то.
    #:
    #: Имя параметра у семейств разное, и это не мелочь: `splash` -- Debian,
    #: RHEL-семейство слушает `rhgb`. Здесь стояло `quiet splash` для обоих,
    #: поэтому на РЕД ОС графическая загрузка не включалась никогда, а вместе
    #: с ней не включался и видимый ввод пароля.
    GRAPHICAL_BOOT_PARAMETER = "splash"

    #: Чем plymouth рисует наш экран. Общего плагина у семейств нет.
    #:
    #: Тема одна на всех -- цвета, шрифт, положение текста, -- а рисует её
    #: разный код: в РЕД ОС есть `two-step` и нет `script`, в Debian ровно
    #: наоборот (проверено по plymouth 24.004.60 Debian 13: в пакете лежат
    #: details, script, text и tribar). Собирать носитель из сети этот проект
    #: не умеет принципиально, поэтому «поставить недостающий плагин» -- не
    #: вариант, и облик приходится описывать дважды: ключами для `two-step` и
    #: скриптом для `script`.
    #:
    #: Что общего -- остаётся общим: имя темы, чёрный фон, белый текст, шрифт
    #: с кириллицей и то, какие вопросы носитель задаёт человеку.
    PLYMOUTH_PLUGIN = "two-step"

    #: Шрифт консоли, знающий кириллицу. Имена у семейств разные.
    #:
    #: Это запасной путь, а не основной: с 31.08.2026 человек видит
    #: графический экран plymouth, и там кириллица читается. Но plymouth
    #: поднимается не всегда -- его выключают параметром ядра, он может не
    #: встать на незнакомой видеокарте, а аварийный режим рисует консолью в
    #: любом случае. Носитель, который в этот момент показывает вместо
    #: объяснения сплошные блоки, бесполезен ровно тогда, когда нужен больше
    #: всего.
    #:
    #: Шрифт консоли по умолчанию кириллицы не знает: он собран под латиницу,
    #: и русский текст идёт прямоугольниками. Найдено прогоном 28.08.2026.
    CONSOLE_FONT = ""

    #: Строки `/etc/default/grub`, специфичные для семейства.
    GRUB_EXTRA_DEFAULTS: tuple[str, ...] = ()

    def grub_defaults(self, config: InstallConfig, layout: MediaLayout | None = None) -> str:
        """`/etc/default/grub` — один шаблон на оба семейства.

        Таймаут меню здесь не мелочь: носитель может оказаться в машине без
        клавиатуры, и меню, ждущее нажатия бесконечно, там означает «не
        загрузится никогда».

        os-prober выключен намеренно: иначе он просканирует диски машины
        администратора, видимые через привязанный /dev, и добавит в меню
        записи для систем, которых там, куда поедет носитель, не будет.
        """
        lines = [
            "# generated by portable-os-admin",
            f"GRUB_TIMEOUT={config.boot.menu_timeout_seconds}",
            f'GRUB_CMDLINE_LINUX="{self.kernel_cmdline(config, layout)}"',
            'GRUB_CMDLINE_LINUX_DEFAULT=""',
            "GRUB_TERMINAL_OUTPUT=console",
            "GRUB_DISABLE_OS_PROBER=true",
            *self.GRUB_EXTRA_DEFAULTS,
        ]
        return "\n".join(lines) + "\n"

    def relabel_commands(self, mount_root: str) -> list[list[str]]:
        """Расстановка меток мандатного контроля по всему дереву.

        Нужна там, где система разворачивается **не своими средствами**: dnf
        и debootstrap запускаются с машины администратора, а на ней своего
        мандатного контроля может не быть вовсе. Тогда файлы попадают в
        систему без меток, и при первой загрузке она замирает, не дойдя даже
        до служебных файловых систем.

        Место — сборка дистрибутива, а не развёртывание: метки хранятся в
        расширенных атрибутах файлов, squashfs их сохраняет, копирование
        тоже. Значит, размечать достаточно один раз на дистрибутив, а не на
        каждый носитель.

        Пусто означает «этому семейству не нужно» — в Debian мандатный
        контроль устроен иначе и меток файлам не требует.
        """
        return []

    def restore_labels_command(
        self, mount_root: str, paths: tuple[str, ...] = ("/etc",), upper: str = ""
    ) -> list[str] | None:
        """Метки для файлов, дописанных уже на носитель.

        Развёртывание пишет то, что уникально для носителя — fstab, crypttab,
        ключи, — и эти файлы появляются после разметки эталона.

        `upper` — путь к верхнему слою overlay, каким он виден внутри chroot.
        Задаётся, когда корень собран из образа: метить сквозь overlay нельзя,
        см. семейство, которое это умеет.
        """
        return None

    BOOT_OK_SCRIPT = "usr/local/sbin/poa-boot-ok"
    BOOT_OK_UNIT = "etc/systemd/system/poa-boot-ok.service"

    def boot_ok_files(self) -> dict[str, tuple[str, int | None]]:
        """«Загрузка удалась» (Э4): слот -- проверенный, min_serial -- выше.

        Срабатывает, когда система дошла до multi-user: значит, ядро, initramfs,
        образ и слой носителя этого слота работают. До этого момента GRUB
        считает попытки нового слота и при неудачах возвращается к прежнему.
        """
        env = f"/boot/efi/{GRUB_ENV_DIR}/grubenv"
        script = f"""#!/bin/sh
# generated by portable-os-admin
set -u
slot=$(sed -n 's/.*poa\\.slot=\\([ab]\\).*/\\1/p' /proc/cmdline)
serial=$(sed -n 's/.*poa\\.serial=\\([0-9]*\\).*/\\1/p' /proc/cmdline)
[ -n "$slot" ] || exit 0
if [ -f {env} ]; then
    {self.GRUB_EDITENV} {env} set poa_good="$slot"
    {self.GRUB_EDITENV} {env} unset poa_try poa_tries
fi
# Антиоткат: после успешной загрузки старше этого выпуска не загрузиться.
min=$(cat /var/lib/poa/min_serial 2>/dev/null || echo 0)
if [ -n "$serial" ] && [ "$serial" -gt "$min" ] 2>/dev/null; then
    mkdir -p /var/lib/poa
    echo "$serial" > /var/lib/poa/min_serial.tmp && mv /var/lib/poa/min_serial.tmp /var/lib/poa/min_serial
fi
logger -t poa-boot-ok "слот $slot подтверждён, выпуск $serial"
"""
        unit = """[Unit]
Description=Подтверждение слота загрузки (слоты A/B)
After=multi-user.target local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/poa-boot-ok

[Install]
WantedBy=multi-user.target
"""
        return {self.BOOT_OK_SCRIPT: (script, 0o755), self.BOOT_OK_UNIT: (unit, 0o644)}

    def generic_initramfs_files(self) -> dict[str, tuple[str, int | None]]:
        """Настройка, закрепляющая универсальность образа на самом носителе."""
        return {}

    @abstractmethod
    def bootloader_commands(
        self, layout: MediaLayout, config: InstallConfig, *, overlay_root: bool = False
    ) -> list[list[str]]:
        """argv list (chroot) installing the bootloader onto the ESP.

        `overlay_root` -- система на носителе хранится сжатым образом, и
        корень в этот момент собран overlay. Тогда генератор конфигурации
        дистрибутива непригоден, и конфигурация пишется своя: см.
        `grub_config_script`.
        """

    #: Заголовок меню: то, что человек видит при загрузке носителя.
    GRUB_MENU_TITLE = "Portable OS GNU/Linux"
    #: Утилита переменных GRUB у семейства.
    GRUB_EDITENV = "grub-editenv"

    def overlay_root_arguments(self, layout: MediaLayout) -> str:
        """Чем в командной строке ядра описан корень, собранный из образа.

        По умолчанию — обычное `root=<устройство>`: у initramfs-tools корень
        монтирует сам initramfs, а наш скрипт подменяет его после. Семейству,
        где монтированием заведует systemd внутри initramfs, любое значение
        `root=` мешает, и оно переопределяет метод — см. `overlay.ROOT_FLAG`.
        """
        return f"root={layout.mapper_path}"

    def grub_config_script(self, layout: MediaLayout, config: InstallConfig, target: str) -> str:
        """Скрипт, который пишет `grub.cfg` на целевой системе.

        Штатный генератор (`update-grub`, `grub2-mkconfig`) для носителя с
        системой из образа не годится, и это не придирка к инструменту.
        Первым же делом он выясняет, на каком устройстве лежит корень:
        `GRUB_DEVICE="`grub-probe --target=device /`"` -- присваивание
        безусловное, подсказать нельзя. А корень в этот момент -- overlay,
        собранный из образа и слоя изменений, и устройства у него нет:
        `grub-probe` отвечает «failed to get canonical path of 'poa-overlay'»
        и установка останавливается на пятнадцатом шаге из восемнадцати.
        Проверено записью на настоящую флешку 09.09.2026.

        Своя конфигурация здесь не беднее сгенерированной, а честнее: у
        носителя ровно один пункт меню, и всё, что в нём стоит, известно
        отсюда -- раздел `/boot` по UUID, отображение LUKS по имени,
        командная строка целиком. Единственное, чего программа не знает, --
        как называется ядро в этом образе, поэтому имена ядра и initramfs
        скрипт находит на месте.

        Пути внутри `grub.cfg` считаются от того раздела, который grub
        сделает своим корнем, а это `/boot`: там ядро лежит как `/vmlinuz-*`,
        без ведущего `/boot`.
        """
        cmdline = self.kernel_cmdline(config, layout)
        root = self.overlay_root_arguments(layout)
        return f"""set -eu
# generated by portable-os-admin
# Слоты A/B (Э4): меню одно на оба слота и при обновлении не переписывается.
# В каталоге слота -- vmlinuz, initrd.img и slot.cfg (номер выпуска и корневой
# хеш); выбор слота -- переменные на ESP, их GRUB умеет и читать, и писать.
[ -f /boot/slot-a/vmlinuz ] || {{ echo "в слоте A нет ядра" >&2; exit 1; }}
mkdir -p /boot/efi/{GRUB_ENV_DIR}
[ -f /boot/efi/{GRUB_ENV_DIR}/grubenv ] || {self.GRUB_EDITENV} /boot/efi/{GRUB_ENV_DIR}/grubenv create
{self.GRUB_EDITENV} /boot/efi/{GRUB_ENV_DIR}/grubenv set poa_good=a
mkdir -p "$(dirname {target})"
cat > {target} <<'CFG'
# generated by portable-os-admin
set default=0
set timeout={config.boot.menu_timeout_seconds}
insmod part_gpt
insmod ext2
insmod fat
search --no-floppy --fs-uuid --set=root {layout.boot_uuid}
search --no-floppy --fs-uuid --set=poa_esp {layout.esp_uuid}
set poa_env=($poa_esp)/{GRUB_ENV_DIR}/grubenv
load_env -f $poa_env poa_good poa_try poa_tries
if [ -z "$poa_good" ]; then set poa_good=a; fi
set poa_slot=$poa_good
# Новый слот пробуется {BOOT_TRIES} раза; не подтвердился -- обратно к проверенному.
if [ -n "$poa_try" ]; then
    if [ "$poa_tries" = "3" ]; then set poa_tries=2; set poa_slot=$poa_try
    elif [ "$poa_tries" = "2" ]; then set poa_tries=1; set poa_slot=$poa_try
    elif [ "$poa_tries" = "1" ]; then set poa_tries=0; set poa_slot=$poa_try
    else set poa_try=; set poa_tries=
    fi
    save_env -f $poa_env poa_try poa_tries
fi
source /slot-$poa_slot/slot.cfg
export poa_slot poa_roothash poa_serial

menuentry '{self.GRUB_MENU_TITLE}' {{
    linux /slot-$poa_slot/vmlinuz {root} ro poa.slot=$poa_slot poa.roothash=$poa_roothash poa.serial=$poa_serial {cmdline}
    initrd /slot-$poa_slot/initrd.img /poa-medium.img
}}
CFG
echo "poa-boot: меню слотов A/B написано"
"""

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

    def chroot_setup_commands(
        self, mount_root: str, config: InstallConfig, media_id: str = ""
    ) -> list[list[str]]:
        """Implements the OS-account-and-service half of UC-4's "настраивает
        содержимое системного раздела" step. The OS-level login password is
        intentionally locked (`passwd -l`) rather than set here: the LUKS
        passphrase supplied at install time is this project's single
        front-door secret (see README, раздел «Архитектура»). Because the account has
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
        commands += self.extra_user_commands(mount_root, config)
        commands += self.firewall_commands(mount_root, config)

        pkg_cmd = self.package_install_command(mount_root, config.packages)
        if pkg_cmd is not None:
            commands.append(pkg_cmd)

        if config.remote_access.enabled and config.remote_access.method == "openssh":
            ssh_pkg_cmd = self.package_install_command(mount_root, [self.ssh_package])
            if ssh_pkg_cmd is not None:
                commands.append(ssh_pkg_cmd)
            commands.append(self.systemctl_enable(mount_root, self.ssh_service))

        commands.append(self.systemctl_enable(mount_root, "rsyslog"))
        commands.append(self.systemctl_enable(mount_root, "poa-boot-ok.service"))

        # Службу отзыва здесь не включаем: она включается ссылкой при записи
        # носителя, рядом с записью её файлов. Команды chroot выполняются
        # только на пути установки, и включение здесь означало бы, что носитель,
        # собранный развёртыванием дистрибутива, уезжает без автозапуска.

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
    def console_font_files(self, config: InstallConfig) -> dict[str, str]:
        """Что ещё нужно семейству, чтобы шрифт попал в initramfs.

        У RHEL не нужно ничего: dracut читает `vconsole.conf` и кладёт
        названный там шрифт сам. У Debian свой путь -- см. переопределение.
        """
        return {}

    @staticmethod
    def needs_sudo(config: InstallConfig) -> bool:
        """Просит ли эту систему хоть кто-нибудь о правах через sudo.

        Вопрос не праздный. У пользователя киоска `sudo=false` по определению
        режима, и тогда пакет в системе — лишняя программа, дающая права; а в
        РЕД ОС он тянет за собой ещё и редактор, потому что требует
        `/usr/bin/vi` и dnf закрывает это требование `vim-minimal`. То есть
        «нечего запускать» ломалось не о наш список, а о зависимость.

        Обратная ошибка тоже была, и в другом семействе: в Debian sudo не
        ставился никогда (в его индексе приоритет optional), а `usermod -aG
        sudo` выполнялся -- администратор просил права для дополнительной
        учётной записи и получал членство в группе, за которой нет программы.
        """
        if config.account.sudo:
            return True
        for raw in config.extra_users:
            user = ExtraUserConfig(**raw) if isinstance(raw, dict) else raw
            if getattr(user, "sudo", False):
                return True
        return False

    def _vconsole_conf(self, config: InstallConfig) -> str:
        """Раскладка и шрифт консоли.

        Шрифт называется только если семейство знает, какой у него есть:
        пустая строка `FONT=` заставила бы systemd-vconsole-setup искать
        несуществующее и оставить консоль как есть, а это ровно тот отказ,
        который не виден до загрузки.
        """
        loc = config.localisation
        lines = [f"KEYMAP={loc.keymap}"]
        if self.CONSOLE_FONT:
            lines.append(f"FONT={self.CONSOLE_FONT}")
        return "\n".join(lines) + "\n"

    def _plymouth_theme(self) -> str:
        """Описание темы для плагина, который есть в образе.

        Сам текст -- в `text/plymouth.py`: облик носителя один на оба
        семейства, и здесь остаётся только выбор плагина, которым он
        рисуется.
        """
        return plymouth_text.theme(self.PLYMOUTH_PLUGIN)

    def _plymouth_theme_script(self) -> str:
        return plymouth_text.theme_for_script()

    def plymouth_script(self) -> str:
        """Экран для плагина `script`. Текст -- в `text/plymouth.py`."""
        return plymouth_text.script()

    def boot_config_files(self, config: InstallConfig) -> dict[str, str]:
        """Настройки, которые сборка initramfs уносит внутрь: шрифт и раскладка
        консоли, тема plymouth. Общие для всех носителей, поэтому пишутся и
        при сборке общего initramfs (Э2) -- без них он говорил бы квадратами
        (живая загрузка 02.10.2026)."""
        files = {
            "etc/vconsole.conf": self._vconsole_conf(config),
            **self.console_font_files(config),
            # Тема plymouth: ею носитель разговаривает с человеком до загрузки
            # системы. Выбирается здесь, а не `plymouth-set-default-theme`:
            # утилита есть не во всех образах, а файл читается всегда.
            "etc/plymouth/plymouthd.conf": (
                "# generated by portable-os-admin\n"
                "[Daemon]\n"
                f"Theme={PLYMOUTH_THEME_NAME}\n"
                "ShowDelay=0\n"
            ),
            f"usr/share/plymouth/themes/{PLYMOUTH_THEME_NAME}/{PLYMOUTH_THEME_NAME}.plymouth":
                self._plymouth_theme(),
        }
        # Скрипт темы -- только там, где рисует `script`. Класть его рядом с
        # темой `two-step` было бы безвредно, но лживо: файл, который никто не
        # читает, при следующем разборе выглядит как рабочий.
        if self.PLYMOUTH_PLUGIN == "script":
            files[
                f"usr/share/plymouth/themes/{PLYMOUTH_THEME_NAME}/{PLYMOUTH_THEME_NAME}.script"
            ] = self.plymouth_script()
        return files

    def config_files(self, config: InstallConfig, layout: MediaLayout, image=None) -> dict[str, str]:
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
            # KEYMAP и FONT вместе: раскладка и то, чем она рисуется.
            #
            # Шрифт называется здесь не только ради системы -- dracut кладёт в
            # initramfs ровно тот шрифт, что назван в этом файле, и без строки
            # FONT образ остаётся без шрифта вовсе. Файл пишется до сборки
            # initramfs (см. installer: config_files идут раньше
            # initramfs_command), иначе называть было бы нечего.
            **self.boot_config_files(config),
            "etc/X11/xorg.conf.d/00-poa-keyboard.conf": self._keyboard_conf(config),
            "etc/fstab": self._fstab(layout, config),
            "etc/crypttab": self._crypttab(layout, config),
            **self.network_files(config),
            **self.security_files(config),
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

    def network_files(self, config: InstallConfig) -> dict[str, str]:
        """Настройка сети, если задан статический адрес.

        При `dhcp` не пишется ничего: NetworkManager сам поднимет все
        интерфейсы, и лишний файл только помешал бы. Для переносного носителя
        это правильное умолчание — он не знает, в какой сети окажется.
        """
        net = config.network
        if net.method != "static" or not net.address:
            return {}
        lines = [
            "# generated by portable-os-admin",
            "[connection]",
            "id=poa-static",
            "type=ethernet",
        ]
        if net.interface:
            lines.append(f"interface-name={net.interface}")
        lines += ["", "[ipv4]", "method=manual", f"address1={net.address}"]
        if net.gateway:
            lines[-1] = f"address1={net.address},{net.gateway}"
        if net.nameservers:
            lines.append(f"dns={net.nameservers.replace(',', ';')};")
        lines += ["", "[ipv6]", "method=ignore", ""]
        return {"etc/NetworkManager/system-connections/poa-static.nmconnection": "\n".join(lines)}

    def security_files(self, config: InstallConfig) -> dict[str, str]:
        """Мандатный контроль доступа. По умолчанию — ничего.

        Переопределяется семейством, у которого он есть: в Debian это
        AppArmor и режим здесь не задаётся, в RHEL — SELinux.
        """
        return {}

    def extra_user_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Дополнительные учётные записи сверх основной.

        Пароль ни одной из них не задаётся: единственный секрет носителя —
        фраза LUKS, и второй способ входа менял бы модель угроз. Вход по ключу
        SSH — другое дело, он ключом и защищён.
        """
        commands: list[list[str]] = []
        for raw in config.extra_users:
            user = ExtraUserConfig(**raw) if isinstance(raw, dict) else raw
            if not user.username:
                continue
            argv = [mount_root, "useradd", "-m", "-s", user.shell]
            if user.full_name:
                argv += ["-c", user.full_name]
            if user.groups:
                argv += ["-G", user.groups]
            commands.append(self.chroot(*argv, user.username))
            commands.append(self.chroot(mount_root, "passwd", "-l", user.username))
            if user.sudo:
                commands.append(
                    self.chroot(mount_root, "usermod", "-aG", self.sudo_group, user.username)
                )
            if user.ssh_keys:
                home = f"/home/{user.username}"
                keys = "\n".join(user.ssh_keys)
                commands.append(
                    self.chroot(
                        mount_root, "sh", "-c",
                        f"mkdir -p {home}/.ssh && printf '%s\\n' {shlex.quote(keys)} > "
                        f"{home}/.ssh/authorized_keys && chmod 700 {home}/.ssh && "
                        f"chmod 600 {home}/.ssh/authorized_keys && "
                        f"chown -R {user.username}: {home}/.ssh",
                    )
                )
        return commands

    def overlay_boot_files(self, layout: MediaLayout) -> dict:
        """Файлы, которые учат initramfs собирать корень из образа и слоя.

        Пусто означает «это семейство пока не умеет такой носитель», и
        развёртывание в таком виде для него не предлагается — молча собрать
        незагружающийся носитель хуже, чем отказаться.
        """
        return {}

    #: Приходит ли загрузчик готовым в пакетах, а не собирается на месте.
    #: В RHEL-семействе — да, и там дистрибутив без него бесполезен.
    bootloader_comes_from_packages: bool = False

    #: Служба брандмауэра этого семейства. Пусто — семейство её не имеет, и
    #: настройка брандмауэра тихо пропускается вместо отказа установки.
    firewall_service: str = ""

    def firewall_commands(self, mount_root: str, config: InstallConfig) -> list[list[str]]:
        """Брандмауэр и мандатный контроль доступа."""
        commands: list[list[str]] = []
        if self.firewall_service:
            if config.firewall.enabled:
                commands.append(self.systemctl_enable(mount_root, self.firewall_service))
            else:
                commands.append(
                    self.chroot(
                        mount_root, "env", "SYSTEMD_OFFLINE=1",
                        "systemctl", "disable", self.firewall_service,
                    )
                )
        return commands

    def _keyboard_conf(self, config: InstallConfig) -> str:
        """Раскладка графического сеанса.

        Отдельно от консольной: в графическом сеансе действует не KEYMAP из
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
        self, config: InstallConfig, layout: MediaLayout | None = None, image=None,
        overlay_root: bool = False,
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
        files.update(self.generic_initramfs_files())
        files.update(self.boot_ok_files())
        files.update(kiosk_mode.config_files(config, image))
        # Доступ (Э8) -- в образ: код клиента и юниты одинаковы у всех носителей
        # и должны обновляться вместе с образом.
        from ..access import files as access_files
        files.update(access_files.image_files(config))
        # Отзыв -- только при записи носителя: скрипт называет идентификатор
        # этого носителя и его раздел. В эталонном корне дистрибутива ни того,
        # ни другого ещё нет, и общий на всех скрипт спрашивал бы ресурс об
        # одном и том же идентификаторе со всех флешек сразу.
        if layout is not None and getattr(layout, "mapper_name", "") \
                and not getattr(layout, "generic", False):
            files.update(
                revocation.config_files(
                    config,
                    # Идентификатор берётся из раскладки, а не собирается
                    # обратно из имени отображения. Прежнее
                    # `mapper_name.rsplit("-", 1)[-1]` верно резало имя, пока
                    # ID был из восьми знаков; с приставкой администратора
                    # (`poa-51839da0-63dd0905`) оно отдавало только собственную
                    # часть, носитель спрашивал сервер о несуществующем
                    # идентификаторе и получал 403 -- живой прогон 16.09.2026.
                    # Запасной путь -- для старых раскладок без поля: отсекать
                    # приставку `poa-` целиком, а не резать по дефису.
                    media_id=(
                        getattr(layout, "media_id", "")
                        or layout.mapper_name.removeprefix("poa-")
                    ),
                    luks_uuid=layout.luks_uuid,
                    # Отзыв уничтожает носитель целиком, а не один том, поэтому
                    # скрипту нужны все части раскладки: служебные тома -- это
                    # отдельные контейнеры со своими ключами, а загрузочные
                    # разделы оставляют носитель загрузочным.
                    logs_luks_uuid=layout.logs_luks_uuid,
                    exchange_luks_uuid=layout.exchange_luks_uuid,
                    esp_uuid=layout.esp_uuid,
                    boot_uuid=layout.boot_uuid,
                )
            )
        files.update(self.machine_binding_files(config, layout))
        # Хуки, собирающие корень из образа и слоя изменений. Пишутся только
        # при развёртывании (есть раскладка) и только если корень носителя
        # **действительно** собран overlay поверх образа.
        #
        # Именно действительно, а не «администратор так хотел»: раньше здесь
        # стоял `config.disk.system_image` -- предпочтение, -- а установщик
        # решает иначе, ещё и по наличию образа в дистрибутиве. Установка с
        # дистрибутива, собранного деревом, получала скрипты overlay на
        # обычный корень; при загрузке они уносили корень из $rootmnt, не
        # находили образа и оставляли систему без точки монтирования. Носитель
        # доходил до вопроса о привязке и после перезагрузки не грузился вовсе
        # (13.09.2026, Debian + MATE).
        if layout is not None and overlay_root:
            files.update(self.overlay_boot_files(layout))
        return files

    def machine_binding_files(
        self, config: InstallConfig, layout: MediaLayout | None
    ) -> dict[str, tuple[str, int | None]]:
        """Скрипты привязки к машине -- на корень, откуда их берёт initramfs.

        Идентификатор носителя нужен только скрипту регистрации, и берётся он
        из имени тома: media_id -- его вторая половина. Без раскладки (сборка
        дистрибутива) регистрация не пишется, потому что до развёртывания
        носителя ещё нет и привязывать нечего.
        """
        if not machine_binding.is_enabled(config):
            return {}
        files: dict[str, tuple[str, int | None]] = {
            machine_binding.KEY_SCRIPT_PATH: (machine_binding.key_script(config), 0o755),
        }
        if layout is not None and layout.mapper_name:
            # Полный ID, а не хвост имени отображения: с приставкой
            # администратора в ID появился дефис (см. `MediaLayout.media_id`).
            media_id = getattr(layout, "media_id", "") or layout.mapper_name.removeprefix("poa-")
            files[machine_binding.REGISTER_SCRIPT_PATH] = (
                machine_binding.register_script(config, media_id), 0o755
            )
        return files

    def _fstab(self, layout: MediaLayout, config: InstallConfig) -> str:
        """Таблица монтирования. Текст -- в `text/tables.py`."""
        return tables_text.fstab(layout, config)

    def _crypttab(self, layout: MediaLayout, config: InstallConfig) -> str:
        """Таблица томов под LUKS. Текст -- в `text/tables.py`.

        Единственное, чем семейства здесь различаются, -- открывает ли
        корень keyscript привязки к машине: у РЕД ОС это делает свой хук
        dracut, и `keyscript` там не понимают вовсе.
        """
        return tables_text.crypttab(layout, config, keyscript=self.crypttab_keyscript)
