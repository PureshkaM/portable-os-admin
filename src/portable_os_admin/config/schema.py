"""Install-configuration schema (the course project's "Kickstart-конфигурация").

Field groups mirror "Конфигурация.Состав": disk/partition parameters, LUKS
parameters, the user account, system tasks, remote-access parameters, and the
boot/recovery attempt limit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

def supported_target_os() -> "tuple[str, ...]":
    """Семейства, которые программа умеет ставить, -- по реестру бэкендов.

    Список выводится, а не записывается: добавить дистрибутив должно быть
    можно одним классом бэкенда, не разыскивая по коду места, где семейства
    перечислены ещё раз. Импорт локальный -- бэкенды сами читают схему.
    """
    from ..install.backends import BACKENDS

    return tuple(BACKENDS)
#: Accepted values for InstallConfig.arch. Both spellings of each architecture
#: are allowed because the two families name them differently (Debian says
#: amd64/arm64, RHEL says x86_64/aarch64) and users copy whichever they saw.
SUPPORTED_ARCH = ("amd64", "x86_64", "arm64", "aarch64")


#: Field paths spelled the way the interface names them. Anything missing from
#: here is shown by its path, so a new field appears in the change list as soon
#: as it exists rather than silently going unreported.
FIELD_LABELS = {
    "name": "Имя конфигурации",
    "target_os": "Целевая ОС",
    "release": "Релиз",
    "mirror": "Зеркало репозитория",
    "arch": "Архитектура носителя",
    "profile": "Режим системы",
    "packages": "Предустановленные пакеты",
    "system_tasks": "Команды после установки",
    "disk.esp_partition_mb": "Размер EFI-раздела, МБ",
    "disk.boot_partition_mb": "Размер /boot, МБ",
    "disk.system_image": "Система сжатым образом со слоем изменений",
    "disk.log_partition_mb": "Размер раздела журналов, МБ",
    "disk.exchange_partition_mb": "Размер раздела обмена, МБ",
    "machine_binding.slots": "Разных машин на носитель",
    "revocation.enabled": "Отзыв и авторизация сеанса сервером",
    "revocation.url": "Адрес ресурса отзыва",
    "revocation.timeout_seconds": "Ждать ответа, секунд",
    "luks.cipher": "Шифр LUKS",
    "luks.key_size_bits": "Длина ключа LUKS, бит",
    "luks.kdf": "Функция формирования ключа",
    "luks.failed_attempts_limit": "Лимит попыток ввода пароля",
    "luks.min_passphrase_length": "Минимальная длина парольной фразы",
    "account.username": "Имя пользователя",
    "account.full_name": "Полное имя",
    "account.sudo": "Права администратора у пользователя",
    "account.shell": "Командная оболочка",
    "account.min_password_length": "Минимальная длина пароля",
    "hostname": "Имя в сети",
    "localisation.locale": "Язык системы",
    "localisation.keymap": "Раскладка консоли",
    "localisation.x11_layouts": "Раскладки рабочего стола",
    "localisation.x11_options": "Переключение раскладок",
    "localisation.timezone": "Часовой пояс",
    "localisation.hardware_clock_utc": "Аппаратные часы по UTC",
    "boot.menu_timeout_seconds": "Таймаут меню загрузчика, с",
    "boot.kernel_parameters": "Параметры ядра",
    "boot.quiet": "Тихая загрузка",
    "boot.verbose": "Подробная загрузка",
    "boot.serial_console": "Вывод загрузки в последовательный порт",
    "network.method": "Настройка сети",
    "network.address": "Адрес",
    "network.gateway": "Шлюз",
    "network.nameservers": "Серверы имён",
    "network.interface": "Интерфейс",
    "firewall.enabled": "Брандмауэр",
    "firewall.open_ports": "Открытые порты",
    "firewall.selinux": "Режим SELinux",
    "extra_users": "Дополнительные учётные записи",
    "package_sets.base": "Состав базовой системы",
    "package_sets.boot": "Загрузочные пакеты",
    "package_sets.desktop": "Пакеты рабочей среды",
    "package_sets.kiosk": "Пакеты киоска",
    "package_sets.exclude": "Исключённые пакеты",
    "services.enable": "Включаемые службы",
    "services.disable": "Выключаемые службы",
    "services.mask": "Маскируемые службы",
    "remote_access.enabled": "Удалённый доступ",
    "remote_access.method": "Метод удалённого доступа",
    "remote_access.port": "Порт удалённого доступа",
    "remote_access.params": "Параметры удалённого доступа",
    "kiosk.enabled": "Режим киоска",
    "kiosk.desktop_environment": "Рабочая среда",
    "kiosk.session_type": "Графический протокол",
    "kiosk.hidden_launchers": "Скрытые запускатели",
    "kiosk.allowed_settings": "Что можно настраивать",
    "kiosk.autostart_app": "Приложение автозапуска",
    "kiosk.screen_blank_minutes": "Гашение экрана, мин",
    "kiosk.allow_shutdown": "Выключение машины пользователем",
    "kiosk.allow_removable_media": "Съёмные носители",
    "kiosk.firejail": "Песочница firejail",
    "kiosk.firejail_options": "Параметры firejail",
    "auto_update.enabled": "Автообновление",
    "auto_update.include_kernel": "Обновлять ядро",
    "auto_update.delay_minutes": "Задержка обновления, мин",
}

#: Never reported as a change: derived from the configuration rather than
#: authored, so listing them would be noise the administrator did not cause.
_DIFF_IGNORED = {"validation_status", "validation_errors", "name"}


def _format_value(value) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "(пусто)"
    if value == "":
        return "(пусто)"
    return str(value)


def _diff_dicts(before: dict, after: dict, prefix: str = "") -> list[tuple[str, str, str]]:
    changes: list[tuple[str, str, str]] = []
    for key in after:
        path = f"{prefix}{key}"
        if path in _DIFF_IGNORED:
            continue
        old, new = before.get(key), after[key]
        if isinstance(new, dict) and isinstance(old, dict):
            changes += _diff_dicts(old, new, prefix=f"{path}.")
        elif old != new:
            changes.append((FIELD_LABELS.get(path, path), _format_value(old), _format_value(new)))
    return changes


@dataclass
class DiskLayoutConfig:
    """Partition sizes for the medium.

    `boot_partition_mb` is a *separate, unencrypted* /boot, distinct from the
    ESP. It has to exist: GRUB cannot unlock a LUKS2 container that uses the
    argon2id KDF (its cryptodisk support is PBKDF2-only), so a layout with
    /boot inside the encrypted volume produces a medium that never boots.
    Kernel and initramfs live here, and the initramfs prompts for the LUKS
    passphrase once, the normal way an encrypted-root install behaves.
    """

    esp_partition_mb: int = 512
    boot_partition_mb: int = 1024
    log_partition_mb: int = 512
    exchange_partition_mb: int = 2048
    # the LUKS/system partition consumes the remainder of the device

    #: Хранить систему сжатым образом плюс слой изменений, а не распакованным
    #: деревом.
    #:
    #: На носитель кладётся один файл -- тот самый squashfs, который уже
    #: собран на шаге сборки дистрибутива, -- и поверх него собирается
    #: overlay. Это втрое меньше байт и последовательная запись вместо
    #: миллиона мелких файлов: замер на тестовой флешке -- 2,9 МБ/с потолка
    #: против 0,49--0,88 МБ/с, которые давала раскладка деревом.
    #:
    #: Второе свойство важнее скорости: база становится доступной только для
    #: чтения, и систему можно вернуть в заведомо известное состояние,
    #: очистив один каталог. Подробности и честные границы -- в
    #: `install/overlay.py`.
    #:
    #: Выключается для сравнения и на случай, если overlay окажется причиной
    #: чего-то на конкретной машине: тогда носитель разворачивается прежним
    #: способом, распакованным деревом.
    system_image: bool = True


#: Cipher specifications cryptsetup accepts, mapped to the key sizes each one
#: actually supports. The sizes are not a UI preference -- cryptsetup rejects
#: anything else outright.
#:
#: XTS modes take a *doubled* key: the 512-bit entry is AES-256 (two 256-bit
#: keys), which is the strongest AES that exists, and the 256-bit entry is
#: AES-128. There is no larger AES to offer. Where extra strength is actually
#: available is the KDF below, which governs how expensive guessing the
#: passphrase is -- that, not the cipher key length, is what a real attack
#: goes after.
LUKS_CIPHERS = {
    "aes-xts-plain64": (512, 256),
    "serpent-xts-plain64": (512, 256),
    "twofish-xts-plain64": (512, 256),
    "aes-cbc-essiv:sha256": (256, 192, 128),
}
#: Password-based key derivation functions LUKS2 supports. argon2id is memory-
#: hard and the LUKS2 default; pbkdf2 exists for compatibility with tooling
#: that cannot read argon2 headers (notably GRUB's cryptodisk).
LUKS_KDFS = ("argon2id", "argon2i", "pbkdf2")

def known_releases() -> "dict[str, tuple[str, ...]]":
    """Выпуски, которые умеет ставить каждое семейство, новейший первым.

    Ответ даёт сам бэкенд, а не таблица рядом со схемой: про свои выпуски
    знает он. До 02.09.2026 то же самое было записано здесь и ещё раз в
    мастере конфигурации, и добавление третьего семейства означало правку в
    трёх местах, из которых интерфейс -- самое неожиданное.
    """
    from ..install.backends import BACKENDS

    return {name: cls.RELEASES for name, cls in BACKENDS.items() if cls.RELEASES}


def _known_releases_of(target_os: str) -> "tuple[str, ...]":
    return known_releases().get(target_os, ())


#: Profiles the installed system can be built as. "console" is what this
#: project produced before the kiosk work: a text-only base system with an
#: autologin console. "kiosk" is the restricted desktop -- a graphical session
#: whose menu holds only the applications the administrator allowed.
SYSTEM_PROFILES = ("console", "kiosk")

#: Desktops the kiosk profile can be built on. Both exist in the installation
#: images of both supported families -- checked against their own indexes.
SUPPORTED_DESKTOPS = ("mate", "kde")

#: Display protocols a kiosk session can run on.
SUPPORTED_SESSION_TYPES = ("x11", "wayland")


@dataclass
class KioskConfig:
    """The restricted desktop ("режим киоска").

    Modelled on the mode RED OS ships as `redos-kiosk-utils`, which this
    project cannot use: that package is published in RED SOFT's network
    repositories and is absent from the installation image, while installing
    from a local image is this tool's primary path. The restrictions below
    therefore reproduce the same result with facilities both target families
    already have -- MATE, dconf, polkit, X and the mount options -- so one
    implementation covers Debian and RED OS alike and does not depend on the
    version of the image the administrator happens to hold.

    Списка разрешённых приложений здесь нет намеренно: он выводится из
    пакетов, которые поставил администратор. Собранная система минимальна и не
    даёт пользователю ни одного приложения; прикладную часть администратор
    кладёт поверх сам, и она же становится доступной. `hidden_launchers` --
    единственная ручная правка, и она вычитает, а не добавляет.
    """

    enabled: bool = False
    #: Which desktop the restricted session is built on.
    #:
    #: "mate" mirrors what RED OS's own kiosk uses, and restricts the session
    #: through dconf: settings written system-wide and locked so the user
    #: cannot write them back.
    #:
    #: "kde" uses KDE's Kiosk framework, which is a different kind of thing and
    #: a stronger one. Its restrictions are not settings-that-are-locked but
    #: authorisation checks the applications themselves perform: `shell_access`
    #: is consulted by KAuthorized before anything opens a shell, so Konsole
    #: refuses to start and Dolphin stops offering "open terminal" -- rather
    #: than the launcher merely being hidden. It also has two things MATE has
    #: no equivalent for: `run_desktop_files`, which refuses to execute a
    #: .desktop file that did not come from a system directory, and URL
    #: restrictions, which can stop file dialogs from browsing outside a
    #: permitted path.
    desktop_environment: str = "mate"
    #: Display protocol of the session: "x11" or "wayland".
    #:
    #: Only KDE can do Wayland -- MATE has no Wayland session at all, and
    #: asking for that pairing is refused rather than quietly downgraded.
    #:
    #: Kept as an explicit choice because it changes which restrictions apply.
    #: `xorg.conf` is not read under Wayland, so the `DontVTSwitch` line that
    #: blocks Ctrl+Alt+F<n> there does nothing; what holds in both cases is the
    #: polkit refusal of org.freedesktop.login1.chvt, plus masked getty units
    #: and NAutoVTs=0. Plasma is moving to Wayland, so this is the field that
    #: keeps the mode honest as that happens rather than silently losing a
    #: restriction.
    session_type: str = "x11"
    #: Запускатели, которые администратор снял вручную.
    #:
    #: Списка разрешённого здесь нет и не должно быть: что администратор
    #: поставил пакетами, то пользователю и доступно. Наше дело -- собрать
    #: минимальную систему, в которой пользователю не доступно ни одного
    #: приложения, а прикладную часть администратор кладёт поверх сам.
    #:
    #: До 02.09.2026 тут стоял `allowed_apps` -- список имён desktop-записей,
    #: который заполнялся руками. Ошибку он давал такую, что поймать её было
    #: нечем: прогон того же дня разрешил `atril`, пакета с ним не поставил, и
    #: меню оказалось пустым; узнал бы об этом администратор от пользователя.
    #: Имя пакета и имя его записи совпадают далеко не всегда.
    #:
    #: Здесь остаётся обратное действие -- вычитание. Один пакет нередко
    #: ставит несколько запускателей (LibreOffice -- шесть), и лишние надо
    #: уметь убрать. Имена читаются из образа и показываются галочками, а
    #: снятые попадают сюда.
    hidden_launchers: list[str] = field(default_factory=list)
    #: Settings the user may change: "network", "sound", "datetime".
    #:
    #: A kiosk still has to be usable by whoever sits in front of it -- joining
    #: a wireless network, unmuting the speakers and correcting the clock are
    #: not administration, they are the difference between a working terminal
    #: and a call to the help desk. Everything else in System Settings stays
    #: shut, disks and devices first among them.
    #:
    #: Named by purpose rather than by module, because the module that serves a
    #: purpose differs between desktops: KDE's network module is
    #: `kcm_networkmanagement`, MATE's is a different program entirely. The
    #: installed set is read off the target rather than listed here, so an
    #: image carrying a different selection of modules still gets a complete
    #: list of refusals (see kiosk.settings_modules_script).
    allowed_settings: list[str] = field(default_factory=list)
    #: Desktop entry started automatically when the session opens. Empty means
    #: the user gets the (restricted) desktop itself.
    #:
    #: Проверить его против списка доступного на этапе конфигурации больше
    #: нельзя -- список складывается уже в собранной системе, -- поэтому
    #: несуществующее имя ловится там же, при установке, и попадает в журнал
    #: строкой, а не остаётся молчаливым пустым автозапуском.
    autostart_app: str = ""
    #: Minutes of inactivity before the screen blanks. 0 keeps it lit.
    #:
    #: Blanking, not locking, and the difference is forced by this project's
    #: login model rather than chosen. RED OS's kiosk offers `-t/--timelock`,
    #: a lock screen after idle time; that presupposes an account password to
    #: unlock with. Here the account password is deliberately locked
    #: (`passwd -l`) and the display manager logs the user in automatically,
    #: because the LUKS passphrase entered at boot is meant to be the single
    #: front-door secret. A lock screen would therefore demand a password that
    #: does not exist and shut the user out of the session until a reboot.
    #: So the screen goes dark and comes back on a keypress, with no prompt.
    screen_blank_minutes: int = 0
    # RED OS's kiosk also offers -i/--autohide, hiding the panel until the
    # pointer reaches the screen edge. There is deliberately no such field
    # here: MATE stores that setting per panel, under
    # /org/mate/panel/toplevels/<id>/, and <id> is generated when the session
    # first builds its panels -- which happens on the medium, after this
    # program is done. There is no path to write at install time, so the
    # option would have been a switch that quietly did nothing.
    #: Let the user power the machine off and reboot it from the panel.
    allow_shutdown: bool = True
    #: Allow mounting USB sticks and other removable media from the session.
    #: Off by default: a kiosk that mounts arbitrary media is a kiosk that
    #: runs arbitrary files.
    allow_removable_media: bool = False
    #: Run the allowed applications under firejail (RED OS: -f). Only takes
    #: effect where the package exists -- it is in the RED OS image and absent
    #: from the Debian DVD, so this stays a request rather than a guarantee.
    firejail: bool = False
    #: Extra firejail switches, e.g. "--net=none --nosound".
    firejail_options: str = ""


@dataclass
class AutoUpdateConfig:
    """Unattended updates on every boot of the finished medium.

    The point is a fleet: many identical sticks handed out to many people, with
    no way to collect them again to patch each one by hand. Each medium updates
    itself from the repository named in `InstallConfig.mirror`.

    That mirror is what makes this work at all. The installation image is
    mounted on the administrator's machine and is gone the moment the stick is
    unplugged, so a medium built from an image alone has no package source and
    nothing to update from -- which is why the validator refuses this option
    when no mirror is configured, instead of installing a service that fails
    silently on every boot for the life of the medium.
    """

    enabled: bool = False
    #: Include kernel and bootloader packages in the upgrade.
    #:
    #: On by default because the kernel is where the security fixes that matter
    #: most land, and excluding it would make the whole feature cosmetic. The
    #: trade-off is real and belongs to the administrator: a kernel upgrade
    #: rebuilds the initramfs on the medium, and while this project's recovery
    #: hooks live in /etc/initramfs-tools and are re-applied by that rebuild, a
    #: rebuild interrupted by someone pulling the stick out leaves a medium
    #: that does not boot.
    include_kernel: bool = True
    #: Minutes to wait after boot before starting. Not zero by default: the
    #: update runs on a USB stick, and competing with the desktop session for
    #: the same slow device makes the first minutes after login crawl.
    delay_minutes: int = 5


@dataclass
class BootConfig:
    """Как ведёт себя загрузчик и с чем стартует ядро."""

    #: Сколько секунд показывать меню загрузчика. `0` — не показывать.
    menu_timeout_seconds: int = 5
    #: Дополнительные параметры ядра. Добавляются к тем, что программа задаёт
    #: сама (корень, разблокировка тома): их она не отдаёт, потому что от них
    #: зависит, загрузится ли носитель вообще.
    kernel_parameters: str = ""
    #: Тихая загрузка: `quiet`. Для киоска обычно да, для отладки нет.
    quiet: bool = False
    #: Подробная загрузка: без `quiet`, со строками состояния systemd
    #: (`systemd.show_status=1`). Главнее `quiet`: оба флажка сразу означают
    #: «подробно», потому что включают его ради диагностики.
    #:
    #: Заставку (`splash`/`rhgb`) режим не снимает: ею носитель спрашивает
    #: пароль, привязку и код восстановления, и 31.08.2026 её уже отвязывали
    #: от подробностей загрузки ровно по этой причине. Появилось 15.09.2026
    #: взамен того, что для стенда вписывали руками в `kernel_parameters`.
    verbose: bool = False
    #: Дублировать вывод в последовательный порт (`ttyS0`, 115200). Только для
    #: стенда и только из экспертного режима; `tty0` при этом остаётся главной
    #: консолью, см. `InstallBackend.kernel_cmdline`.
    serial_console: bool = False


@dataclass
class NetworkConfig:
    """Сеть устанавливаемой системы.

    Пустые поля означают «как решит NetworkManager», то есть DHCP на всех
    интерфейсах. Статический адрес имеет смысл, только когда носитель
    привязан к известному месту в сети, — а он переносной, поэтому умолчание
    именно такое.
    """

    #: `dhcp` или `static`.
    method: str = "dhcp"
    #: Адрес с длиной префикса: `192.168.1.50/24`. Только при method=static.
    address: str = ""
    gateway: str = ""
    #: Серверы имён через запятую.
    nameservers: str = ""
    #: Интерфейс, к которому это относится. Пусто — первый проводной.
    interface: str = ""


@dataclass
class FirewallConfig:
    """Брандмауэр и мандатный контроль доступа."""

    #: Включать ли службу брандмауэра дистрибутива.
    enabled: bool = True
    #: Открытые порты: `22/tcp`, `5900/tcp`. Пусто — политика по умолчанию.
    open_ports: list[str] = field(default_factory=list)
    #: Режим SELinux в RHEL-семействе: `enforcing`, `permissive`, `disabled`.
    #: В Debian не действует: там AppArmor, и его режим здесь не задаётся.
    selinux: str = "enforcing"


@dataclass
class ExtraUserConfig:
    """Дополнительная учётная запись сверх основной.

    Пароль здесь не задаётся намеренно: единственный секрет носителя — фраза
    LUKS, и заводить второй способ входа значит менять модель угроз. Такая
    запись годится для служебных нужд — например, для входа по ключу.
    """

    username: str = ""
    full_name: str = ""
    sudo: bool = False
    shell: str = "/bin/bash"
    #: Дополнительные группы через запятую.
    groups: str = ""
    #: Открытые ключи SSH, по одному на строку.
    ssh_keys: list[str] = field(default_factory=list)


@dataclass
class PackageSetsConfig:
    """Чем заменить наборы пакетов, которые иначе выбирает программа.

    Пустой список означает «как решит бэкенд»: для РЕД ОС базовый состав
    читается из `comps.xml` самого образа, остальные наборы — списки,
    подобранные вручную (и подобранные не случайно: в набор графики нарочно
    не входят терминал, системный монитор и центр управления — их отсутствие
    и есть самое сильное ограничение режима киоска).

    Заданный список **заменяет** набор целиком, а не дополняет его. Дополнять
    нужно через `packages`.
    """

    #: Базовая система. Осторожно: слишком короткий список даёт корень, в
    #: котором нечему работать, и это выяснится через полчаса установки.
    base: list[str] = field(default_factory=list)
    #: Ядро, загрузчик, генератор initramfs, cryptsetup. Без них носитель не
    #: загрузится, поэтому заменять стоит только зная, чем.
    boot: list[str] = field(default_factory=list)
    #: Графическое окружение режима киоска.
    desktop: list[str] = field(default_factory=list)
    #: Дополнения самого режима киоска поверх графики.
    kiosk: list[str] = field(default_factory=list)
    #: Что выкинуть из любого набора, включая вычисленный из образа. Работает
    #: и когда набор не переопределён — это способ убрать один пакет, не
    #: переписывая весь список.
    exclude: list[str] = field(default_factory=list)


@dataclass
class ServicesConfig:
    """Службы, которые надо включить, выключить или замаскировать.

    Применяется в chroot целевой системы после установки пакетов. Маскировка
    сильнее выключения: замаскированную службу не поднимет и зависимость.
    """

    enable: list[str] = field(default_factory=list)
    disable: list[str] = field(default_factory=list)
    mask: list[str] = field(default_factory=list)


@dataclass
class LocalisationConfig:
    """Язык, раскладка и часовой пояс устанавливаемой системы.

    До 26.08.2026 всё это было зашито в код (`en_US.UTF-8`, `us`, `UTC`) и
    попадало только в файл для валидатора, а на носитель — нет. Для системы,
    которую ставят в российской организации, неверно и то и другое.

    Умолчания оставлены прежними намеренно: смена умолчания меняет поведение
    у тех, кто ничего не менял. Кому нужен другой язык — задаёт его явно.
    """

    #: Значение LANG: `ru_RU.UTF-8`, `en_US.UTF-8`. Должно быть в списке
    #: локалей, которые умеет собрать целевой дистрибутив.
    locale: str = "en_US.UTF-8"
    #: Раскладка текстовой консоли (`/etc/vconsole.conf`, KEYMAP): `us`, `ru`.
    keymap: str = "us"
    #: Раскладки графического сеанса через запятую: "us,ru". Пусто — берётся
    #: раскладка консоли.
    x11_layouts: str = ""
    #: Как переключать раскладки в графическом сеансе: `grp:alt_shift_toggle`
    #: и подобное. Пусто — переключателя нет.
    x11_options: str = ""
    #: Часовой пояс из базы tzdata: `Europe/Moscow`, `Asia/Yekaterinburg`.
    timezone: str = "UTC"
    #: Считать ли аппаратные часы идущими по UTC. Ставить false стоит только
    #: если носитель будут запускать на машине с Windows на том же железе.
    hardware_clock_utc: bool = True


@dataclass
class MachineBindingConfig:
    """Ограничение на число машин, где носитель соглашается работать.

    Смысл -- уменьшить вероятность того, что найденный или украденный носитель
    заработает на машине злоумышленника. LUKS закрывает случай «фразы нет»;
    это закрывает случай «фраза есть, а носитель уже обошёл свой лимит».

    Отпечаток машины считается по DMI материнской платы и модели процессора.
    Сетевые адреса намеренно не входят: док-станция или USB-сетевая карта
    меняли бы отпечаток, и одна и та же машина занимала бы слот за слотом,
    пока не упёрлась бы в предел -- то есть механизм срабатывал бы на своих
    вместо чужих.

    Занятые слоты -- это keyslot'ы заголовка LUKS, а не список в файле. Файл
    на зашифрованном корне был первой версией, и от неё отказались: к моменту
    такой проверки фраза уже введена и том открыт, так что снимается она
    монтированием тома с другого носителя. Ключа же, которого в заголовке нет,
    не создать ничем. Раскладка слотов -- в `install/machine_binding.py`.
    """

    #: Сколько разных машин носитель запомнит. 0 -- ограничения нет.
    #:
    #: Пять, а не три: три слота на практике заканчиваются быстрее, чем
    #: меняются рабочие места (ноутбук, стационарная машина дома, машина в
    #: командировке, подменная), и каждое исчерпание -- это обращение к
    #: администратору.
    slots: int = 5


@dataclass
class LuksConfig:
    cipher: str = "aes-xts-plain64"
    key_size_bits: int = 512
    kdf: str = "argon2id"
    failed_attempts_limit: int = 5
    #: Сколько знаков должно быть во фразе носителя, самое малое.
    #:
    #: До 28.08.2026 длина не проверялась вовсе: мастер принимал фразу из
    #: одного символа. Между тем `failed_attempts_limit` останавливает перебор
    #: только через загрузку носителя -- самый глупый способ из возможных.
    #: Тот, у кого носитель в руках, снимает образ заголовка и перебирает
    #: офлайн, где никакие лимиты не действуют, поэтому реальных множителя
    #: два: стоимость одной попытки (argon2id с 256 МиБ, ~8,4 секунды) и длина
    #: фразы. Первый уже выбран, второй до этой правки не требовался никак.
    #:
    #: Двенадцать, а не восемь: восемь -- это порог для пароля учётной записи
    #: внутри системы, где перебор идёт через живой вход и упирается в задержки
    #: PAM. Здесь перебор офлайновый, и запас нужен другой.
    min_passphrase_length: int = 12


@dataclass
class UserAccountConfig:
    username: str = "user"
    full_name: str = ""
    sudo: bool = False
    shell: str = "/bin/bash"
    min_password_length: int = 8


@dataclass
class RemoteAccessConfig:
    enabled: bool = False
    method: str = "openssh"  # "openssh" | "vpn"
    port: int = 22
    params: dict = field(default_factory=dict)


def _migrated(data: dict) -> dict:
    """Приводит конфигурацию, написанную прежней версией, к нынешним полям.

    Конфигурации живут дольше кода: у администратора их несколько, они лежат
    файлами рядом с контейнером, и правка схемы не должна означать, что
    вчерашняя работа перестала открываться. Проверено 02.09.2026 -- ровно это и
    случилось: после переименования одного поля список конфигураций упал с
    `TypeError: unexpected keyword argument 'allowed_apps'`, и экран остался
    пустым.

    Здесь же -- терпимость к незнакомым ключам вообще. Конфигурация, созданная
    более новой версией программы, должна открываться в старой хотя бы тем, что
    в ней понятно; падать на первом же неизвестном имени -- худший из способов
    сообщить о разнице версий.
    """
    kiosk = dict(data.get("kiosk") or {})

    # `allowed_apps` -> `packages`. Смысл поля изменился 02.09.2026: доступное
    # пользователю больше не перечисляется, а выводится из установленных
    # пакетов. Имена оттуда -- лучшее, что известно о намерениях
    # администратора: он перечислял приложения, которые хотел дать. Переносим
    # их в пакеты, если пакеты не заданы, и сбрасываем отметку проверки --
    # конфигурация изменилась, и подтверждать её надо заново.
    legacy_apps = kiosk.pop("allowed_apps", None)
    if legacy_apps and not data.get("packages"):
        data["packages"] = list(legacy_apps)
        data["validation_status"] = "unchecked"
        data["validation_errors"] = []

    known_kiosk = {f.name for f in fields(KioskConfig)}
    data["kiosk"] = {key: value for key, value in kiosk.items() if key in known_kiosk}

    for section, section_cls in (
        ("disk", DiskLayoutConfig),
        ("luks", LuksConfig),
        ("machine_binding", MachineBindingConfig),
        ("account", UserAccountConfig),
        ("remote_access", RemoteAccessConfig),
        ("auto_update", AutoUpdateConfig),
        ("revocation", RevocationConfig),
        ("localisation", LocalisationConfig),
        ("boot", BootConfig),
        ("network", NetworkConfig),
        ("firewall", FirewallConfig),
        ("package_sets", PackageSetsConfig),
        ("services", ServicesConfig),
    ):
        values = data.get(section)
        if isinstance(values, dict):
            known = {f.name for f in fields(section_cls)}
            data[section] = {key: value for key, value in values.items() if key in known}

    known_top = {f.name for f in fields(InstallConfig)}
    return {key: value for key, value in data.items() if key in known_top}


@dataclass
class RevocationConfig:
    """Удалённый отзыв и шлюз сеанса: носитель раз в пять минут и при подъёме
    сети спрашивает сервер о своём состоянии подписанным запросом.

    Закрывает случай, который не закрывает привязка к машинам: уволившийся
    сотрудник с невозвращённым носителем и уже зарегистрированной машиной.
    Получив «отозван», носитель уничтожает себя целиком -- необратимо. В
    режиме киоска, пока сервер не ответил «ok», приложения не запускаются.
    Протокол -- `docs/RELEASE-1.1.md`.

    Ресурс задаётся администратором и должен быть **общедоступным**. Это не
    недосмотр, а суть механизма: проверка обязана срабатывать раньше, чем
    носитель дотянется до внутренней сети. Ресурс во внутреннем контуре
    означал бы, что отозванный носитель сперва попадает внутрь периметра, а
    уже потом узнаёт, что он отозван.
    """

    enabled: bool = False
    #: Адрес ресурса, у которого носитель спрашивает состояние. Только HTTPS:
    #: подпись защищает от подмены ответа, но не от того, что по дороге видно,
    #: какие носители у организации есть и когда они включаются.
    url: str = ""
    #: Сколько ждать ответа. Больше -- дольше висит один проход без связи;
    #: меньше -- ложные «нет связи» на медленном канале.
    timeout_seconds: int = 10


@dataclass
class InstallConfig:
    name: str
    target_os: str = "debian"
    release: str = ""
    # Where *additional* packages come from: a company repository, a public
    # mirror, or nothing. The system itself is not installed from here -- it
    # comes from a local installation image chosen at install time. The image's
    # path is deliberately not part of the configuration: it belongs to one
    # particular machine, and a configuration copied to another admin station
    # would carry a path that does not exist there.
    mirror: str = ""
    # CPU architecture of the *target* system. Empty means "same as the machine
    # running the installer", which is the only combination that works without
    # extra tooling: debootstrap/dnf populate a foreign-architecture root only
    # under qemu-user-static, and the chroot steps below would otherwise fail
    # with "Exec format error". Kept explicit in the config so a wrong value is
    # reported by validation instead of surfacing as a cryptic chroot failure.
    arch: str = ""
    #: Имя устанавливаемой системы в сети. Прежде было зашито как
    #: "portable-os", из-за чего все носители организации представлялись
    #: одним и тем же именем.
    hostname: str = "portable-os"
    # What kind of system to build: the text-only console system this project
    # produced originally, or the restricted desktop. Kept as a separate field
    # from kiosk.enabled so the two can never disagree -- see __post_init__.
    profile: str = "console"
    disk: DiskLayoutConfig = field(default_factory=DiskLayoutConfig)
    luks: LuksConfig = field(default_factory=LuksConfig)
    machine_binding: MachineBindingConfig = field(default_factory=MachineBindingConfig)
    #: Удалённый отзыв. Отдельно от `machine_binding`: привязка закрывает чужие
    #: машины, отзыв -- бывшего своего сотрудника.
    revocation: RevocationConfig = field(default_factory=RevocationConfig)
    account: UserAccountConfig = field(default_factory=UserAccountConfig)
    localisation: LocalisationConfig = field(default_factory=LocalisationConfig)
    boot: BootConfig = field(default_factory=BootConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)
    #: Дополнительные учётные записи сверх `account`.
    extra_users: list[dict] = field(default_factory=list)
    package_sets: PackageSetsConfig = field(default_factory=PackageSetsConfig)
    services: ServicesConfig = field(default_factory=ServicesConfig)
    remote_access: RemoteAccessConfig = field(default_factory=RemoteAccessConfig)
    kiosk: KioskConfig = field(default_factory=KioskConfig)
    auto_update: AutoUpdateConfig = field(default_factory=AutoUpdateConfig)
    system_tasks: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    validation_status: str = "unchecked"  # "unchecked" | "valid" | "invalid"
    validation_errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # One truth, two spellings: `profile` is what the interface and the
        # validator talk about, `kiosk.enabled` is what the install steps read.
        # Reconciling them here means a configuration loaded from an older file
        # (which has neither field, or only one) still ends up consistent
        # instead of building a desktop whose restrictions were never applied.
        if self.profile == "kiosk":
            self.kiosk.enabled = True
        elif self.kiosk.enabled:
            self.profile = "kiosk"
        else:
            self.profile = "console"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InstallConfig":
        data = _migrated(dict(data))
        disk = DiskLayoutConfig(**data.pop("disk", {}))
        luks = LuksConfig(**data.pop("luks", {}))
        machine_binding = MachineBindingConfig(**data.pop("machine_binding", {}))
        account = UserAccountConfig(**data.pop("account", {}))
        remote_access = RemoteAccessConfig(**data.pop("remote_access", {}))
        # Configurations written before the kiosk profile existed simply have
        # no such key, and default to the console system they were made for.
        kiosk = KioskConfig(**data.pop("kiosk", {}))
        auto_update = AutoUpdateConfig(**data.pop("auto_update", {}))
        # Конфигурации, написанные до появления удалённого отзыва, этого ключа
        # не имеют и получают отзыв выключенным.
        revocation = RevocationConfig(**data.pop("revocation", {}))
        # Конфигурации, написанные до того, как язык и часовой пояс стали
        # настраиваемыми, этого ключа не имеют и получают прежние значения.
        localisation = LocalisationConfig(**data.pop("localisation", {}))
        boot = BootConfig(**data.pop("boot", {}))
        network = NetworkConfig(**data.pop("network", {}))
        firewall = FirewallConfig(**data.pop("firewall", {}))
        package_sets = PackageSetsConfig(**data.pop("package_sets", {}))
        services = ServicesConfig(**data.pop("services", {}))
        return cls(
            disk=disk,
            luks=luks,
            machine_binding=machine_binding,
            revocation=revocation,
            account=account,
            remote_access=remote_access,
            kiosk=kiosk,
            auto_update=auto_update,
            localisation=localisation,
            boot=boot,
            network=network,
            firewall=firewall,
            package_sets=package_sets,
            services=services,
            **data,
        )

    @classmethod
    def create_default(cls, name: str, target_os: str = "debian") -> "InstallConfig":
        """A plain configuration: the dataclass defaults, nothing decided.

        Kept as it was because the installer, the tests and any code that just
        needs *a* configuration rely on it. What an administrator starts from
        in the interface is `create_baseline` below.
        """
        from ..install.backends import BACKENDS

        backend = BACKENDS.get(target_os)
        release = backend.DEFAULT_RELEASE if backend else ""
        return cls(name=name, target_os=target_os, release=release)

    @classmethod
    def create_baseline(cls, name: str, target_os: str = "debian") -> "InstallConfig":
        """What a new configuration starts as: the locked-down minimum.

        The starting point is a decision, not a blank. A wizard that opens on
        defaults is telling the administrator what this tool is for, and the
        old defaults said "an ordinary system with a console login" -- which is
        the opposite of the point, and meant that getting a restricted medium
        required knowing to go and ask for one.

        So a new configuration begins as the most restricted thing this program
        can build, and every step afterwards is the administrator *opening*
        something deliberately: a permission granted, an application allowed, a
        service switched on. Widening is visible; narrowing does not have to be
        remembered.

        Ничего не разрешается заранее, и это тоже намеренно: there is
        no sensible guess at which applications a particular kiosk should have,
        and validation refuses a kiosk without any -- so the one thing the
        administrator must decide is the one thing they are asked for.
        """
        config = cls.create_default(name, target_os)
        config.profile = "kiosk"
        config.kiosk = KioskConfig(
            enabled=True,
            desktop_environment="mate",
            hidden_launchers=[],
            # No lock screen is possible anyway (the account password is
            # locked), and a kiosk that dims is a kiosk whose panel survives.
            screen_blank_minutes=15,
            # Granted: without it a user cannot switch the machine off, and
            # pulling the plug on a medium mid-write is worse than the risk of
            # letting them shut it down.
            allow_shutdown=True,
            # Refused: a mounted stick is arbitrary files, and this is the
            # single most useful thing to have to turn on consciously.
            allow_removable_media=False,
            # Off because it cannot be promised -- the package is in the RED OS
            # image and absent from the Debian DVD.
            firejail=False,
        )
        # Every optional opening starts closed.
        config.account.sudo = False
        config.remote_access = RemoteAccessConfig(enabled=False)
        config.auto_update = AutoUpdateConfig(enabled=False)
        config.packages = []
        config.system_tasks = []
        # Зеркало -- часть минимальной конфигурации, а не поле, которое
        # администратор обязан заполнить руками. Адрес знает семейство: у
        # Debian он один на все выпуски, у РЕД ОС включает выпуск и
        # архитектуру. Раньше оба адреса лежали в мастере конфигурации, и
        # конфигурация, созданная не через мастер, оставалась без зеркала.
        from ..install.backends import BACKENDS

        backend_cls = BACKENDS.get(target_os)
        if backend_cls is not None:
            config.mirror = backend_cls().default_mirror(config.release, config.arch)
        return config

    def diff_from(self, other: "InstallConfig") -> list[tuple[str, str, str]]:
        """(human-readable field, was, became) for everything that differs.

        Exists so the last step of the wizard can show what the administrator
        actually changed, rather than only the finished file. A JSON document
        of sixty fields does not tell anyone which four of them they touched,
        and in a configuration whose whole purpose is restricting a system,
        "what did I open up?" is the question worth answering.
        """
        return _diff_dicts(other.to_dict(), self.to_dict())
