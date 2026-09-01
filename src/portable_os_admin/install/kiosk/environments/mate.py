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
                # Файловый менеджер снят со списка обязательных компонентов
                # сессии. Умолчание MATE -- ['windowmanager', 'panel',
                # 'filemanager', 'dock'], где filemanager это caja, и
                # mate-session, по описанию собственного ключа, «automatically
                # add the required components back to the session at login
                # time if they do get removed». То есть удалить пакет мало:
                # сессия каждый вход просила бы то, чего на носителе нет.
                #
                # Оконный менеджер и панель остаются -- без них сеанс это
                # чёрный экран, и это уже проверялось живой загрузкой. Dock в
                # умолчании пуст, поэтому и не назван.
                ("required-components-list", "['windowmanager', 'panel']"),
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
                ("show-desktop-icons", "false"),
            ],
        ),
    ]
    return settings


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
