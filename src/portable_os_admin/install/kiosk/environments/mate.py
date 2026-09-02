"""Режим киоска на MATE.

MATE ограничивается через dconf: настройки пишутся системно и запираются,
чтобы пользователь не мог записать их обратно. Это настройки-которые-заперты,
в отличие от KDE, где то же самое делают проверки авторизации в самих
приложениях.
"""
from __future__ import annotations

from ....config.schema import InstallConfig
from ..common import DESKTOP_MATE, EXCHANGE_DIR, SESSION_NAME
from .base import DesktopEnvironment

#: dconf profile telling the session to read the system database below.
DCONF_PROFILE_PATH = "etc/dconf/profile/user"
#: System-wide dconf settings and the locks that keep the user from changing
#: them back. Both are compiled into a binary database by `dconf update`.
#: Запись сессии, которой mate-session запускает Caja только как рисовальщик
#: рабочего стола. Лежит в /usr/local/share: XDG_DATA_DIRS просматривает его
#: раньше /usr/share, поэтому наша запись выигрывает у штатной, а сама
#: caja.desktop остаётся нетронутой -- как и всё прочее, что киоск скрывает,
#: но не удаляет.
CAJA_DESKTOP_ENTRY_PATH = "usr/local/share/applications/poa-caja-desktop.desktop"

DCONF_KEYFILE_PATH = "etc/dconf/db/local.d/00-poa-kiosk"
DCONF_LOCKS_PATH = "etc/dconf/db/local.d/locks/00-poa-kiosk"
# ----------------------------------------------------------------------
# dconf
# ----------------------------------------------------------------------
def _dconf_settings(config: InstallConfig) -> list[tuple[str, list[tuple[str, str]]]]:
    """(dconf path, [(key, value)]) for the system-wide database.

    Every key here exists in MATE's own schemas, which is what makes this
    portable between the two distributions: it is the desktop that defines
    them, not the distribution.
    """
    kiosk = config.kiosk
    blank_minutes = max(0, int(kiosk.screen_blank_minutes or 0))
    blanking = blank_minutes > 0

    settings: list[tuple[str, list[tuple[str, str]]]] = [
        (
            "org/mate/desktop/lockdown",
            [
                # The "run application" dialog and every other place the panel
                # offers to execute an arbitrary command.
                ("disable-command-line", "true"),
                ("disable-user-switching", "true"),
                # Always disabled, and not a setting: the account password is
                # locked, so a lock screen would ask for a secret that does not
                # exist and strand the user until a reboot.
                ("disable-lock-screen", "true"),
                # Stops the session offering to open unknown file types with an
                # application the user picks -- a launcher in disguise.
                ("disable-application-handlers", "true"),
                ("disable-theme-settings", "true"),
                # Logging out is only useful where it leads somewhere: with
                # autologin it lands back in the same session, and where the
                # user may not power the machine off it should not be offered
                # at all.
                ("disable-log-out", "false" if kiosk.allow_shutdown else "true"),
                # Диалоги сохранения и открытия файлов -- тоже обзор файловой
                # системы, просто через другое окно. Ключ проверен по схеме
                # org.mate.lockdown образа: там девять ключей, и запрета
                # именно файлового менеджера среди них нет вовсе -- потому
                # доступ к Caja закрывается не одним запретом, а тремя мерами
                # ниже.
                ("disable-save-to-disk", "true"),
            ],
        ),
        (
            "org/mate/panel/general",
            [
                # The panel cannot be re-arranged, and no launcher can be added
                # to it. This is RED OS's "изменение свойств главной панели".
                ("locked-down", "true"),
                # `enable-program-list` is deliberately NOT set to false.
                #
                # It was, and it made the medium unusable: the menu stopped
                # opening at all, so with no desktop icons either there was no
                # way to start a single allowed application. A kiosk you cannot
                # work in is not a restricted kiosk, it is a broken one -- and
                # only a real boot showed it, since every file was written
                # exactly as intended.
                #
                # Hiding the disallowed entries is what limits the menu, and
                # that is done per entry with NoDisplay. Switching the whole
                # program list off is both blunter and unnecessary.
                ("disable-force-quit", "true"),
            ],
        ),
        (
            "org/mate/desktop/media-handling",
            [
                # "автоматическое монтирование внешних накопителей" -- refused
                # at the session level here and at the polkit level below.
                ("automount", "true" if kiosk.allow_removable_media else "false"),
                ("automount-open", "true" if kiosk.allow_removable_media else "false"),
                ("autorun-never", "false" if kiosk.allow_removable_media else "true"),
            ],
        ),
        (
            "org/mate/caja/desktop",
            [
                # No icons that lead anywhere but the allowed applications.
                ("computer-icon-visible", "false"),
                ("home-icon-visible", "false"),
                ("network-icon-visible", "false"),
                ("trash-icon-visible", "false"),
                ("volumes-visible", "true" if kiosk.allow_removable_media else "false"),
            ],
        ),
        (
            "org/mate/caja/preferences",
            [
                # Caja's location bar is a file browser and a launcher at once.
                ("always-use-browser", "false"),
                ("show-hidden-files", "false"),
                # Рабочий стол Caja рисует, а вот окно по щелчку по нему --
                # нет: двойной щелчок по фону не должен открывать домашний
                # каталог.
                ("desktop-is-home-dir", "false"),
                # Строка адреса вместо кнопок пути: набрать в ней путь --
                # значит обойти всё, что закрыто выше. Отключается заранее, на
                # случай если окно всё же откроется.
                ("always-use-location-entry", "false"),
            ],
        ),
        (
            "org/mate/screensaver",
            [
                ("idle-activation-enabled", "true" if blanking else "false"),
                # Never locking -- see disable-lock-screen above. The screen
                # goes dark and comes back on a keypress.
                ("lock-enabled", "false"),
                # Quoted: a dconf keyfile holds GVariant text, where a string
                # is single-quoted and a bare word is a keyword. Unquoted, this
                # fails the whole `dconf update`, and a failed update means not
                # one of the restrictions above reaches the session.
                ("mode", "'blank-only'"),
            ],
        ),
        (
            # /org/mate/desktop/session/, not /org/mate/session/. The schema is
            # named org.mate.session but its path is under desktop/ -- read out
            # of the shipped gschema rather than assumed, after the assumed
            # spelling turned out to be wrong and would have silently written
            # a setting nothing reads.
            "org/mate/desktop/session",
            [
                ("idle-delay", str(blank_minutes) if blanking else "0"),
                # Имя записи без «.desktop»: mate-session ищет её в
                # XDG_DATA_DIRS, где /usr/local/share идёт первым.
                ("filemanager", "'poa-caja-desktop'"),
                # Файловый менеджер сессии -- наша запись, а не штатная.
                #
                # 01.09.2026 из списка убирали `filemanager` вместе с самим
                # Caja, и сеанс перестал подниматься: три загрузки подряд
                # заканчивались чёрным экраном при собранной базе dconf и всех
                # прочих компонентах на месте. Caja в MATE -- часть среды, а
                # не приложение поверх неё.
                #
                # Поэтому он остаётся, но запускается ключами
                # `--force-desktop --no-default-window`: рабочий стол рисует,
                # окон не открывает. Подробнее -- в `caja_desktop_entry`.
                ("required-components-list",
                 "['windowmanager', 'panel', 'filemanager']"),
            ],
        ),
        (
            "org/mate/desktop/background",
            [
                # Фон рисует mate-settings-daemon, а не файловый менеджер:
                # «Have MATE draw the desktop background» -- прочитано в
                # gschema образа, а не предположено.
                ("draw-background", "true"),
                # А это -- «Have MATE file manager (Caja) draw the desktop
                # icons». Caja на носителе нет, и ключ пишется на случай,
                # когда он всё же появится зависимостью пакета
                # администратора: рабочий стол со значками -- это папка, а
                # папка -- это файловый менеджер.
                # Рабочий стол рисует Caja, и это оставлено как есть: без
                # него сеанс на РЕД ОС не поднимался вовсе (прогон
                # 01.09.2026). Значков при этом нет -- они убраны поимённо
                # ниже, в org/mate/caja/desktop, где выключены компьютер,
                # домашний каталог, сеть и корзина.
                ("show-desktop-icons", "true"),
            ],
        ),
    ]
    return settings


def caja_desktop_entry() -> str:
    """Caja как рисовальщик рабочего стола -- и только.

    В MATE файловый менеджер входит в саму среду: mate-session держит его в
    `required-components`, и без него сеанс не поднимается вовсе. Проверено
    прогоном 01.09.2026 -- три загрузки подряд заканчивались чёрным экраном
    при полностью исправном во всём остальном носителе.

    Значит убрать Caja нельзя, а обзор файлов давать нельзя тоже. Разводится
    это его же ключами, прочитанными в man caja:

    * `--force-desktop` -- рисовать рабочий стол независимо от настроек и
      окружения. Без него Caja может решить, что стол рисует кто-то другой, и
      не нарисовать ничего;
    * `--no-default-window` -- «создавать окна только для явно указанных
      URI». То есть запуск сам по себе окна не открывает, а открыть его
      явно неоткуда: запись в меню скрыта, обработчик каталогов снят, а
      командная строка запрещена ключом lockdown.

    Запись подменяет штатную не удалением, а порядком поиска: XDG_DATA_DIRS
    просматривает /usr/local/share раньше /usr/share.
    """
    return (
        "[Desktop Entry]\n"
        "# generated by portable-os-admin\n"
        "Type=Application\n"
        "Name=Рабочий стол\n"
        "Exec=caja --force-desktop --no-default-window\n"
        # Не показывать нигде: это компонент сеанса, а не приложение.
        "NoDisplay=true\n"
        "X-MATE-Autostart-Phase=Desktop\n"
    )


def dconf_keyfile(config: InstallConfig) -> str:
    lines = [
        "# generated by portable-os-admin",
        "# Режим киоска: системные настройки рабочей среды.",
        "",
    ]
    for path, keys in _dconf_settings(config):
        lines.append(f"[{path}]")
        lines += [f"{key}={value}" for key, value in keys]
        lines.append("")
    return "\n".join(lines)


def dconf_locks(config: InstallConfig) -> str:
    """Without locks the settings above are merely defaults the user overwrites.

    Every key written is locked; nothing is set that is not also locked, so the
    two files can never drift apart.
    """
    lines = [
        "# generated by portable-os-admin",
        "# Ключи, которые пользователь не может изменить обратно.",
    ]
    for path, keys in _dconf_settings(config):
        lines += [f"/{path}/{key}" for key, _ in keys]
    return "\n".join(lines) + "\n"


def dconf_profile() -> str:
    return (
        "# generated by portable-os-admin\n"
        # Order matters: the user database is consulted first for anything the
        # system database has not locked.
        "user-db:user\n"
        "system-db:local\n"
    )




class MateEnvironment(DesktopEnvironment):
    """MATE: настройки пишутся в dconf и запираются."""

    name = DESKTOP_MATE

    def files(self, config: InstallConfig) -> dict[str, tuple[str, int | None]]:
        return {
            DCONF_PROFILE_PATH: (dconf_profile(), None),
            DCONF_KEYFILE_PATH: (dconf_keyfile(config), None),
            DCONF_LOCKS_PATH: (dconf_locks(config), None),
            CAJA_DESKTOP_ENTRY_PATH: (caja_desktop_entry(), None),
        }

    def apply_fragment(self, config: InstallConfig) -> str:
        # Скомпилировать системную базу dconf из файлов, записанных
        # установщиком. Без этого шага и настройки, и блокировки остаются
        # просто текстом.
        return """
if command -v dconf >/dev/null 2>&1; then
    dconf update
else
    echo "poa-kiosk: dconf не установлен, настройки рабочей среды не применены" >&2
fi
"""
