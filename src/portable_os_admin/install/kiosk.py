"""The restricted desktop ("режим киоска") -- files and commands.

RED OS ships this mode as `redos-kiosk-utils`: a MATE desktop plus a modified
bash, driven by `kiosk-mode-on -u <user> -a <apps>`. That package is published
in RED SOFT's network repositories and is **not present in the installation
image** (checked against redos-8: 3274 packages, none named *kiosk*), while
installing from a local image is this project's primary and recommended path.
So the mode is reproduced here rather than delegated, using only facilities
both target families already carry -- MATE, dconf, polkit, X and mount options.
One implementation then covers Debian and RED OS alike and survives a change
of image version, because none of these interfaces are versioned.

Where the restrictions actually live, strongest first. This ordering matters:
the interface-level ones are the easiest to write and the easiest to walk
around, so they are deliberately the *last* line rather than the only one.

1. **Nothing to run.** The system is built from a minimal base; a terminal, a
   file manager with a "run command" box and the MATE control centre are never
   installed. What is absent cannot be launched.
2. **Nowhere to run it from.** Every location the user can write to is mounted
   `noexec,nosuid,nodev` -- /home, /tmp, /var/tmp and the exchange volume. A
   binary or script brought in on the exchange partition will not execute.
3. **No way out of the session.** X refuses VT switching (`DontVTSwitch`) and
   the text consoles are masked, so Ctrl+Alt+F<n> leads nowhere.
4. **No privileged actions.** polkit refuses pkexec, package management, unit
   management and (unless allowed) mounting removable media for this user.
5. **Nothing to click.** Desktop entries outside the whitelist are hidden, and
   dconf locks the panel, the desktop and the settings the session exposes.

What this does *not* stop, stated plainly because a kiosk that is trusted
beyond its reach is worse than none: an allowed application that can itself run
code -- office macros, a browser's developer tools, anything with a built-in
shell -- runs that code as the user. firejail narrows this where it exists (it
is in the RED OS image and absent from the Debian DVD), which is why it is
offered as an option and never assumed.
"""
from __future__ import annotations

import shlex

from ..config.schema import InstallConfig

#: dconf profile telling the session to read the system database below.
DCONF_PROFILE_PATH = "etc/dconf/profile/user"
#: System-wide dconf settings and the locks that keep the user from changing
#: them back. Both are compiled into a binary database by `dconf update`.
DCONF_KEYFILE_PATH = "etc/dconf/db/local.d/00-poa-kiosk"
DCONF_LOCKS_PATH = "etc/dconf/db/local.d/locks/00-poa-kiosk"
XORG_CONF_PATH = "etc/X11/xorg.conf.d/10-poa-kiosk.conf"
POLKIT_RULES_PATH = "etc/polkit-1/rules.d/50-poa-kiosk.rules"
#: Where the whitelist is applied. Run once, in the chroot, after the packages
#: are installed -- the set of desktop entries is not known before that.
APPLY_SCRIPT_PATH = "usr/local/sbin/poa-kiosk-apply"
#: Launcher used by the autostart entry, resolving a desktop-entry name the way
#: `gtk-launch` would without depending on gtk being installed.
LAUNCHER_PATH = "usr/local/bin/poa-kiosk-launch"
AUTOSTART_PATH = "etc/xdg/autostart/zz-poa-kiosk-app.desktop"
#: Overrides live here because XDG_DATA_DIRS puts /usr/local/share ahead of
#: /usr/share, so a file of the same name wins without touching the packages.
OVERRIDE_DIR = "usr/local/share/applications"

#: Mount options for every filesystem the user can write to. `noexec` is the
#: one that matters: it is what stops a file arriving over the exchange volume
#: from being run.
HARDENED_OPTS = "noexec,nosuid,nodev"


DESKTOP_MATE = "mate"
DESKTOP_KDE = "kde"

SESSION_X11 = "x11"
SESSION_WAYLAND = "wayland"

#: Name of the session entry this program writes. Constant across
#: distributions and Plasma versions, unlike the distributions' own names.
SESSION_NAME = "poa-kiosk"

LOGIND_CONF_PATH = "etc/systemd/logind.conf.d/50-poa-kiosk.conf"

#: Where the exchange volume is mounted inside the installed system. Must match
#: backends/base.py:EXCHANGE_MOUNTPOINT, and is repeated rather than imported
#: because base.py imports this module -- importing it back would be a cycle.
#: test_kiosk.py asserts the two stay equal.
EXCHANGE_DIR = "/mnt/exchange"

#: KDE reads system-wide Kiosk restrictions from here.
KDEGLOBALS_PATH = "etc/xdg/kdeglobals"
#: Plasma's own settings, locked the same way.
PLASMARC_PATH = "etc/xdg/plasmarc"
KSCREENLOCKER_PATH = "etc/xdg/kscreenlockerrc"
POWERMANAGEMENT_PATH = "etc/xdg/powermanagementprofilesrc"


def is_enabled(config: InstallConfig) -> bool:
    return bool(getattr(config, "kiosk", None) and config.kiosk.enabled)


def desktop_of(config: InstallConfig) -> str:
    return getattr(config.kiosk, "desktop_environment", DESKTOP_MATE) or DESKTOP_MATE


def wants_wayland(config: InstallConfig) -> bool:
    """Whether this kiosk runs its session on Wayland rather than X11.

    Only KDE can: MATE has no Wayland session, and asking for one is refused by
    validation rather than quietly downgraded.
    """
    if desktop_of(config) != DESKTOP_KDE:
        return False
    return getattr(config.kiosk, "session_type", SESSION_X11) == SESSION_WAYLAND


# ----------------------------------------------------------------------
# KDE: the Kiosk framework
# ----------------------------------------------------------------------
#: Что стоит за каждым разрешением из `allowed_settings`: модули KDE, которые
#: его обслуживают. Имя слева -- назначение, а не модуль: программа, которая
#: настраивает сеть, называется по-разному в разных средах.
SETTINGS_MODULES = {
    "network": ("kcm_networkmanagement",),
    "sound": ("kcm_pulseaudio",),
    "datetime": ("kcm_clock",),
}

#: Разделы «Параметров системы», нужные каждому разрешению.
#:
#: Их приходится называть отдельно от модулей: раздел -- это собственный файл
#: в /usr/share/systemsettings/categories, и он показывается сам по себе, даже
#: когда внутри не осталось ни одного модуля. Так и нашлись «Внешние
#: носители»: раздел пустой, все модули под ним закрыты, а пользователь его
#: всё равно видит. Запретами модулей раздел не убрать -- убирается только
#: удалением его файла.
SETTINGS_CATEGORIES = {
    "network": ("settings-network", "settings-network-connectivity",
                "settings-network-networksettings"),
    "sound": ("settings-hardware", "settings-hardware-multimedia"),
    "datetime": ("settings-personalization", "settings-personalization-regionalsettings"),
}

#: Корневой раздел: без него дерево не строится вовсе.
SETTINGS_ROOT_CATEGORY = "settings-root-category"

#: Стартовая страница «Параметров системы». Запретить её нельзя: приложение
#: открывает именно её при запуске и, не найдя, разыменовывает пустой
#: указатель. Наблюдалось на носителе: окно не появлялось вовсе, systemsettings
#: падал в SettingsBase::changePlugin ещё до первой отрисовки.
SETTINGS_LANDING_MODULE = "kcm_landingpage"


def settings_modules_script(config: InstallConfig) -> str:
    """Shell that writes the module refusals, computed on the target itself.

    KDE has no whitelist for control modules -- only a refusal per module name
    (verified against the Kiosk documentation). A list written here would
    therefore have to name every module that must not open, and that set is a
    property of the installed image, not of this program: RED OS 8 ships 62 of
    them, another image ships a different 62. So the list is produced where the
    modules actually are.

    Nothing being allowed is a valid answer and the common one: then every
    module is refused and the application is not offered anywhere.
    """
    allowed = set()
    categories = set()
    for purpose in config.kiosk.allowed_settings:
        allowed.update(SETTINGS_MODULES.get(purpose, ()))
        categories.update(SETTINGS_CATEGORIES.get(purpose, ()))
    if allowed:
        allowed.add(SETTINGS_LANDING_MODULE)
        categories.add(SETTINGS_ROOT_CATEGORY)
    keep = " ".join(sorted(allowed))
    keep_categories = " ".join(sorted(categories))
    return f"""
# Модули «Параметров системы»: разрешено ровно названное администратором.
#
# Список запретов считается здесь, а не в программе: набор модулей зависит от
# образа. Перечисляются установленные, и каждый, кто не разрешён, получает
# отказ. Секция [$i] делает записи неизменяемыми.
KEEP_MODULES="{keep}"
KEEP_CATEGORIES="{keep_categories}"

is_kept() {{
    for m in $KEEP_MODULES; do
        [ "$m" = "$1" ] && return 0
    done
    return 1
}}

is_kept_category() {{
    for c in $KEEP_CATEGORIES; do
        [ "$c" = "$1" ] && return 0
    done
    return 1
}}

# Два признака, потому что одного не хватает ни одному.
#
# По имени `kcm*` не находятся модули, названные иначе: корзина -- kcmtrash,
# прокси -- proxy, общие ресурсы -- smb. По строке `KCModule` не находятся
# те, чьи метаданные в Plasma 5 переехали внутрь плагина (.so), а .desktop
# остался лишь запускалкой для kcmshell -- таких в РЕД ОС 8 большинство: 55
# из 62. Замер на носителе: одна маска ловит 55, другая 8, и ни одна не
# ловит всё.
modules=""
for f in /usr/share/applications/*.desktop /usr/share/kservices5/*.desktop; do
    [ -e "$f" ] || continue
    n=${{f##*/}}
    n=${{n%.desktop}}
    case "$n" in
        kcm*) ;;
        *) grep -q "KCModule" "$f" 2>/dev/null || continue ;;
    esac
    modules="$modules $n"
done

# Записи неразрешённых модулей удаляются, а не только запрещаются.
#
# Запрет через [KDE Control Module Restrictions] эта версия не применяет:
# проверено на носителе -- 59 модулей были перечислены с `=false`, и
# «Пользователи», «Уведомления», «Обратная связь» открывались как ни в чём
# не бывало. То же поведение описано в KDE bug 509984. Запреты ниже всё
# равно пишутся -- там, где они работают, это второй рубеж, -- но полагаться
# на них нельзя.
removed=0
for f in /usr/share/applications/*.desktop /usr/share/kservices5/*.desktop; do
    [ -e "$f" ] || continue
    n=${{f##*/}}
    n=${{n%.desktop}}
    case "$n" in
        kcm*) ;;
        *) grep -q "KCModule" "$f" 2>/dev/null || continue ;;
    esac
    is_kept "$n" && continue
    rm -f "$f"
    removed=$((removed + 1))
done
echo "poa-kiosk: записей модулей настроек удалено: $removed"

restrictions=/etc/xdg/kdeglobals
denied=0
{{
    echo ""
    echo "[KDE Control Module Restrictions][\\$i]"
    for m in $modules; do
        is_kept "$m" && continue
        echo "$m.desktop=false"
    done
}} >> "$restrictions"
for m in $modules; do
    is_kept "$m" || denied=$((denied + 1))
done
echo "poa-kiosk: модулей настроек закрыто: $denied, оставлено: ${{KEEP_MODULES:-нет}}"

# Разделы. Запрет модулей раздел не убирает: он показывается и пустым.
dropped=0
for f in /usr/share/systemsettings/categories/*.desktop; do
    [ -e "$f" ] || continue
    n=${{f##*/}}
    is_kept_category "${{n%.desktop}}" && continue
    rm -f "$f"
    dropped=$((dropped + 1))
done
echo "poa-kiosk: разделов настроек убрано: $dropped, оставлено: ${{KEEP_CATEGORIES:-нет}}"
"""


#: Плазмоиды, которые в киоске не должны существовать. Не «скрыть», а снести:
#: системный лоток Plasma собирает свой состав из установленных плазмоидов, и
#: вычищенный из конфигурации апплет возвращается сам при следующем входе.
#: Проверено на носителе: «Диски и устройства» вернулись после того, как их
#: убрали из extraItems/knownItems, и показывали служебные разделы носителя
#: вместе с кнопкой «Безопасно извлечь» напротив системного тома.
REMOVED_PLASMOIDS = ("org.kde.plasma.devicenotifier",)


def plasma_layout_script(config: InstallConfig) -> str:
    """Shell that turns the desktop into the one this kiosk promises.

    Three things, all of which have to happen on the target because they touch
    the account's own Plasma configuration:

    * the panel keeps launchers for exactly the allowed applications;
    * the widgets are locked (`immutability=2`), so the user cannot add any;
    * the application launcher's favourites are the allowed applications, and
      not the distribution's default set -- which on RED OS put System Settings
      one click away from a user who is not supposed to reach it.
    """
    if desktop_of(config) != DESKTOP_KDE:
        return ""

    apps = sorted(set(config.kiosk.allowed_apps))
    if config.kiosk.allowed_settings:
        # Разрешив настройки, администратор должен дать чем до них дойти.
        apps.append("systemsettings")
    launchers = ",".join(f"applications:{a}.desktop" for a in apps)
    favourites = ",".join(f"{a}.desktop" for a in apps)
    return f"""
# Панель, избранное и запертые виджеты.
APPLETSRC_LAUNCHERS="{launchers}"
FAVOURITES="{favourites}"

for home in /etc/skel /home/*; do
    [ -d "$home" ] || continue
    f="$home/.config/plasma-org.kde.plasma.desktop-appletsrc"
    [ -f "$f" ] || continue
    sed -i \
        -e "s|^launchers=.*|launchers=$APPLETSRC_LAUNCHERS|" \
        -e "s|^immutability=1$|immutability=2|" \
        "$f"
    stats="$home/.config/kactivitymanagerd-statsrc"
    [ -f "$stats" ] && sed -i "s|^ordering=.*|ordering=$FAVOURITES|" "$stats"
    # База избранного KAstats хранит прежний набор помимо ordering.
    rm -rf "$home/.local/share/kactivitymanagerd/resources"
    owner=${{home##*/}}
    [ "$home" != "/etc/skel" ] && chown -R "$owner:$owner" "$home/.config" 2>/dev/null
done
echo "poa-kiosk: панель и избранное: $APPLETSRC_LAUNCHERS"

# Плазмоиды, которых в киоске быть не должно.
for plasmoid in {" ".join(REMOVED_PLASMOIDS)}; do
    rm -rf "/usr/share/plasma/plasmoids/$plasmoid"
    rm -f "/usr/share/kservices5/plasma-applet-$plasmoid.desktop"
    echo "poa-kiosk: плазмоид удалён: $plasmoid"
done
"""


def _run_desktop_files_lines(kiosk) -> list[str]:
    """Whether to forbid executing .desktop files, and why.

    The restriction stops a `.desktop` brought in from outside from running an
    arbitrary `Exec=` line. It is worth having -- but in Plasma 5 it costs more
    than it looks: measured on the built medium, `run_desktop_files=false`
    refuses **every** desktop entry opened through KIO, including the
    distribution's own under /usr/share/applications. Icons on the desktop and
    the file manager stop launching anything at all; only the application
    launcher still works, because it goes through KService rather than KIO.

    So the restriction is applied exactly when there is something for it to
    defend against: an account allowed to mount removable media can carry a
    prepared entry in. An account that cannot has no way to introduce one --
    the exchange volume is written by the administrator, and it is mounted
    noexec besides. Rather than choosing between "kiosk that launches nothing"
    and "kiosk with a hole", the two follow the same setting.
    """
    if kiosk.allow_removable_media:
        return [
            "# Запуск .desktop-файлов: пользователю разрешены съёмные носители,",
            "# поэтому принесённый на них ярлык не должен выполняться. Цена —",
            "# ярлыки на рабочем столе тоже перестают запускаться (KIO не делает",
            "# исключения даже для системных записей), поэтому в этом режиме их",
            "# не кладут: запуск идёт из меню.",
            "run_desktop_files=false",
        ]
    return [
        "# run_desktop_files намеренно не ограничивается: съёмные носители",
        "# пользователю запрещены, раздел обмена наполняет администратор и он",
        "# смонтирован noexec, то есть принести свой ярлык неоткуда. А запрет",
        "# в Plasma 5 отказывает и системным записям, оставляя рабочий стол и",
        "# файловый менеджер без единого запускаемого приложения.",
    ]


def kdeglobals(config: InstallConfig) -> str:
    """System-wide KDE Kiosk restrictions.

    `[$i]` after a group name marks every entry in it immutable: configuration
    files read later -- including the user's own under ~/.config -- cannot
    override it. That is the mechanism the whole KDE half rests on, and it is
    the one thing here that must not be omitted, since without it these are
    merely defaults.

    Unlike the MATE half, these are not settings that happen to be locked. KDE
    applications ask `KAuthorized::authorize()` before performing the action,
    so `shell_access=false` is checked by Konsole before it opens a shell and
    by Dolphin before it offers to open one -- a refusal at the point of use
    rather than a hidden menu entry.
    """
    kiosk = config.kiosk
    lines = [
        "# generated by portable-os-admin",
        "# Режим киоска KDE. [$i] делает записи неизменяемыми: пользовательские",
        "# файлы настроек не могут их переопределить.",
        "",
        "[KDE Action Restrictions][$i]",
        "# Оболочка для ввода произвольных команд. Проверяется самими",
        "# приложениями KDE, поэтому закрывает и Konsole, и «открыть терминал»",
        "# в файловом менеджере, а не прячет ярлык.",
        "shell_access=false",
        "# KRunner (Alt+F2).",
        "run_command=false",
        *_run_desktop_files_lines(kiosk),
        "# Настройка приложений и панелей.",
        "action/options_configure=false",
        "action/options_configure_keybinding=false",
        "action/options_configure_toolbars=false",
        "action/options_show_menubar=false",
        "movable_toolbars=false",
        "editable_desktop_icons=false",
        "action/kwin_rules=false",
        "# Справочная система: в киоске она ведёт наружу, к произвольным URL.",
        "action/help_contents=false",
        # action/help_report_bug здесь НЕТ, и это не забывчивость.
        #
        # KStandardAction спрашивает KAuthorized и на запрет возвращает
        # nullptr, а не отключённое действие. Приложение, которое результат не
        # проверяет, разыменовывает пустой указатель. «Параметры системы» РЕД
        # ОС падают именно так: segfault в SettingsBase::changePlugin ещё до
        # первой отрисовки, то есть окно не появляется вовсе. Найдено
        # перебором на носителе -- убирали ключи группы по одному, и живым
        # приложение становилось ровно без этого.
        #
        # Общее правило, которое из этого следует: запрет стандартного
        # действия KDE может не ограничить приложение, а уронить его. Каждый
        # такой ключ проверяется живым запуском, а не добавляется «на всякий
        # случай» -- польза от спрятанного пункта «Сообщить об ошибке» не
        # стоит неработающего приложения.
        "action/switch_application_language=false",
        "# Смена пользователя: в системе он один, и пароля у него нет.",
        "action/switch_user=false",
        "action/start_new_session=false",
        "# Работа от имени root из сессии.",
        "user/root=false",
        # Locking is impossible here for the same reason as under MATE: the
        # account password is locked and the login is automatic, so a lock
        # screen would ask for a secret that does not exist.
        "lock_screen=false",
        "action/lock_screen=false",
        f"logout={'true' if kiosk.allow_shutdown else 'false'}",
        "",
        "# Модули «Параметров системы» закрываются не здесь: их набор зависит",
        "# от образа, поэтому список запретов считается на целевой системе —",
        "# см. settings_modules_script(). Здесь остался бы список из шести",
        "# имён, выбранных наугад, при том что модулей в РЕД ОС 8 — 62.",
        "",
    ]

    # Restricting the autostart *resource* would close ~/.config/autostart --
    # otherwise dropping a .desktop there is a way around the whitelist. But it
    # is a KStandardDirs resource, not the XDG directory, and there is no way
    # to verify from here that it leaves /etc/xdg/autostart alone. So it is
    # only applied when this project is not itself relying on autostart; where
    # it is, `run_desktop_files=false` above is what refuses a .desktop that
    # did not come from a system directory, and that covers the same opening.
    if not kiosk.autostart_app:
        lines += [
            "[KDE Resource Restrictions][$i]",
            "# Пользовательский автозапуск: иначе достаточно положить .desktop",
            "# в ~/.config/autostart, чтобы обойти список разрешённых приложений.",
            "autostart=false",
            "",
        ]

    # File dialogs are the remaining way to wander the filesystem, so they are
    # confined to the user's own directory and the exchange volume. Written as
    # a deny-then-allow pair because the rules are evaluated in order and the
    # first rule is the blanket refusal.
    rules = [
        "list,,,,file,,,false",
        "list,,,,file,,$HOME,true",
        f"list,,,,file,,{EXCHANGE_DIR},true",
        "open,,,,file,,,false",
        "open,,,,file,,$HOME,true",
        f"open,,,,file,,{EXCHANGE_DIR},true",
    ]
    lines.append("[KDE URL Restrictions][$i]")
    lines.append("# Диалоги открытия и сохранения не выходят за пределы домашнего")
    lines.append("# каталога и раздела обмена. У MATE такого механизма нет вовсе.")
    lines.append(f"rule_count={len(rules)}")
    lines += [f"rule_{index}={rule}" for index, rule in enumerate(rules, start=1)]
    lines.append("")
    return "\n".join(lines)


def plasmarc(config: InstallConfig) -> str:
    """Plasma's own lockdown: the shell cannot be rearranged."""
    return (
        "# generated by portable-os-admin\n"
        "[Updates][$i]\n"
        "# Плазма не пытается применять свои миграции настроек к запертой\n"
        "# конфигурации при каждом входе.\n"
        "performed=999999\n"
        "\n"
        "[PlasmaViews][$i]\n"
        "immutability=2\n"
    )


def kscreenlockerrc(config: InstallConfig) -> str:
    """Turns the automatic lock screen off. Blank, never lock.

    The single most important file of the KDE half, and the one this project
    got wrong first: the restriction `lock_screen=false` in kdeglobals is a
    KAuthorized check, so it only stops the *user* asking for a lock. The
    automatic lock after idle time is done by the screen-locker daemon, which
    asks nobody and reads `kscreenlockerrc`. With that file absent the daemon
    kept its default -- lock after five minutes -- and the medium locked itself
    behind a password the account does not have. Observed on the built medium:
    the desktop came up, sat idle, and became unreachable until a reboot.

    `kscreensaverrc` used to be written here instead. That is the KDE 4 screen
    saver's file; Plasma's locker does not read it, so every value in it was
    inert.
    """
    minutes = max(0, int(config.kiosk.screen_blank_minutes or 0))
    return (
        "# generated by portable-os-admin\n"
        "# Автоматическая блокировка выключена: пароль учётной записи заблокирован\n"
        "# (единственный секрет — фраза LUKS), поэтому запертый экран не открыть\n"
        "# ничем, кроме перезагрузки.\n"
        "[Daemon][$i]\n"
        "Autolock=false\n"
        "LockOnResume=false\n"
        "LockGrace=0\n"
        f"Timeout={minutes}\n"
    )


def powermanagement_profiles(config: InstallConfig) -> str:
    """Screen blanking after idle time -- the part that is actually wanted.

    Plasma does this through powerdevil's profiles, not through the locker, so
    the two have to be set in different files: the locker is switched off above
    and the display is turned off here. `lockBeforeTurnOff=0` matters as much
    as the timeout -- powerdevil offers to lock before blanking, and that would
    put back exactly the screen this mode must never show.
    """
    minutes = max(0, int(config.kiosk.screen_blank_minutes or 0))
    header = "# generated by portable-os-admin\n"
    if not minutes:
        # Ноль значит «не гасить». Пустые профили и есть выключенное гашение.
        return header + "# Гашение экрана выключено: screen_blank_minutes = 0.\n"

    seconds = minutes * 60
    body = "".join(
        f"[{profile}][DPMSControl][$i]\n"
        f"idleTime={seconds}\n"
        "lockBeforeTurnOff=0\n\n"
        for profile in ("AC", "Battery", "LowBattery")
    )
    return header + body


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
            [("idle-delay", str(blank_minutes) if blanking else "0")],
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


# ----------------------------------------------------------------------
# X, polkit
# ----------------------------------------------------------------------
def xorg_conf() -> str:
    """Closes the two escapes X itself provides.

    DontVTSwitch is RED OS's "смена виртуальной консоли по Ctrl+Alt+Fn"; DontZap
    keeps Ctrl+Alt+Backspace from killing the server back to a login prompt.

    Written on every kiosk, including Wayland ones, because it costs nothing
    and a session that falls back to X11 still gets it -- but it is **not** the
    load-bearing part. Under Wayland this file is never read, so the polkit
    refusal of `org.freedesktop.login1.chvt` is what actually holds.
    """
    return (
        "# generated by portable-os-admin\n"
        "# Действует только под X11. Под Wayland переключение консолей\n"
        "# закрывает правило polkit на org.freedesktop.login1.chvt.\n"
        'Section "ServerFlags"\n'
        '    Option "DontVTSwitch" "true"\n'
        '    Option "DontZap" "true"\n'
        '    Option "AllowClosedownGrabs" "false"\n'
        "EndSection\n"
    )


def logind_conf() -> str:
    """Stops the extra virtual consoles from existing in the first place.

    Third line of defence behind the polkit refusal and the masked getty units:
    with no spare VTs allocated there is nothing to switch to even if a switch
    were somehow permitted.

    `ReserveVT=0` and not 1. The setting does not name the VT the session lives
    on -- it names one that logind reserves *for a getty*, unconditionally and
    regardless of NAutoVTs. Asking for VT 1 therefore asked logind to keep
    starting `autovt@tty1.service` on a system whose whole point is having no
    text console, and the medium's journal filled with the failure of that
    request on every seat event. Nothing here needs a reserved VT: the
    display manager asks logind for one of its own.
    """
    return (
        "# generated by portable-os-admin\n"
        "[Login]\n"
        "# Не выделять дополнительных виртуальных консолей.\n"
        "NAutoVTs=0\n"
        "# Не резервировать консоль под getty: резервировать её значит просить\n"
        "# logind запускать autovt@, которого в этом режиме быть не должно.\n"
        "ReserveVT=0\n"
    )


def session_entry(config: InstallConfig) -> tuple[str, str]:
    """(path, content) for a session this program defines itself.

    Naming the distribution's own session was a mistake waiting to happen: the
    X11 session is `plasma` under Plasma 5 and `plasmax11` under Plasma 6,
    where `plasma` now means the *Wayland* one. Asking for "plasma" would
    therefore select X11 on RED OS 8 and Wayland on Debian 13 -- silently, and
    with different security properties, since the X11 lockdown file is not read
    under Wayland.

    Defining the entry here makes the name constant (`poa-kiosk`) and the
    protocol explicit, on every distribution and every version.
    """
    desktop = desktop_of(config)
    wayland = wants_wayland(config)
    if desktop == DESKTOP_KDE:
        command = "/usr/bin/startplasma-wayland" if wayland else "/usr/bin/startplasma-x11"
        names = "KDE"
    else:
        # MATE has no Wayland session at all; validation refuses that pairing.
        command = "/usr/bin/mate-session"
        names = "MATE"

    directory = "usr/share/wayland-sessions" if wayland else "usr/share/xsessions"
    content = (
        "[Desktop Entry]\n"
        "# generated by portable-os-admin\n"
        "Name=Portable OS kiosk\n"
        "Comment=Ограниченный сеанс переносной системы\n"
        f"Exec={command}\n"
        f"DesktopNames={names}\n"
        f"Type={'Application' if wayland else 'XSession'}\n"
    )
    return f"{directory}/{SESSION_NAME}.desktop", content


def polkit_rules(config: InstallConfig) -> str:
    """Refuses the privileged actions a desktop normally offers.

    Written as a polkit .rules file, which both families use (Debian trixie
    ships polkitd 12x, RED OS 8 likewise). The rule is scoped to the kiosk
    account by name, so an administrator who later unlocks a shell as root is
    not fighting their own policy.

    org.freedesktop.policykit.exec is the important one: it is pkexec, the
    general-purpose "run this as root" door that every other refusal here would
    otherwise be routed around.
    """
    kiosk = config.kiosk
    username = config.account.username
    power = "polkit.Result.YES" if kiosk.allow_shutdown else "polkit.Result.NO"
    media = "polkit.Result.YES" if kiosk.allow_removable_media else "polkit.Result.NO"
    return f"""// generated by portable-os-admin
// Режим киоска: ограничения привилегированных действий для учётной записи.
polkit.addRule(function(action, subject) {{
    if (subject.user !== "{username}") {{
        return polkit.Result.NOT_HANDLED;
    }}

    // pkexec -- общая дверь «выполнить от root». Закрывается первой.
    if (action.id === "org.freedesktop.policykit.exec") {{
        return polkit.Result.NO;
    }}

    // Управление пакетами и службами: установка ПО и правка автозапуска.
    if (action.id.indexOf("org.freedesktop.packagekit.") === 0 ||
        action.id.indexOf("org.freedesktop.systemd1.") === 0 ||
        action.id.indexOf("org.freedesktop.RealtimeKit1.") === 0) {{
        return polkit.Result.NO;
    }}

    // Настройки сети меняет администратор, не пользователь киоска.
    if (action.id.indexOf("org.freedesktop.NetworkManager.settings.modify") === 0 ||
        action.id.indexOf("org.freedesktop.NetworkManager.enable-disable") === 0) {{
        return polkit.Result.NO;
    }}

    // Съёмные носители: смонтированный носитель — это принесённые файлы.
    if (action.id.indexOf("org.freedesktop.udisks2.") === 0 ||
        action.id.indexOf("org.freedesktop.udisks.") === 0) {{
        return {media};
    }}

    // Переключение виртуальных консолей.
    //
    // Это то, что закрывает Ctrl+Alt+F<n> **независимо от протокола**.
    // Option "DontVTSwitch" ниже работает только под X11, а Plasma уходит на
    // Wayland, где xorg.conf не читается вообще: там сочетание обрабатывает
    // сам композитор и просит logind переключить консоль — вот этот запрос и
    // отклоняется здесь.
    if (action.id === "org.freedesktop.login1.chvt") {{
        return polkit.Result.NO;
    }}

    // Выключение и перезагрузка.
    if (action.id.indexOf("org.freedesktop.login1.power-off") === 0 ||
        action.id.indexOf("org.freedesktop.login1.reboot") === 0 ||
        action.id.indexOf("org.freedesktop.login1.halt") === 0) {{
        return {power};
    }}

    return polkit.Result.NOT_HANDLED;
}});
"""


# ----------------------------------------------------------------------
# The whitelist, applied inside the target
# ----------------------------------------------------------------------
def apply_script(config: InstallConfig) -> str:
    """Shell script that hides every launcher outside the whitelist.

    It runs in the chroot rather than being a set of files written from here
    because the set of installed desktop entries is only known once the
    packages are in: a package can install launchers nobody asked for, and
    those are exactly the ones that must disappear.

    Hiding is done with an override file in /usr/local/share/applications, not
    by deleting anything: XDG_DATA_DIRS searches /usr/local/share first, so a
    same-named entry wins while the packaged file stays intact and a later
    `kiosk off` is a matter of removing one directory.

    The override is a **copy of the original with NoDisplay=true added**, not a
    stub. Writing a stub with Hidden=true and no Exec broke the session
    outright, and only a real boot showed it: marco, mate-panel and caja are
    not menu entries but components of the desktop itself, and mate-session
    reads their .desktop files to start them. With no Exec it started none of
    them, so the medium booted to a black screen -- autologin fine, X running,
    and nothing inside. Hidden=true means "this entry has been deleted";
    NoDisplay=true means "it exists, just keep it out of the menu", which is
    what was wanted all along.

    The allowed list is baked into the script instead of passed as arguments --
    a desktop-entry name is administrator input, and text that never crosses a
    shell command line cannot be quoted wrong.
    """
    kiosk = config.kiosk
    allowed = " ".join(sorted(set(kiosk.allowed_apps)))
    firejail_opts = kiosk.firejail_options.strip()
    use_firejail = "1" if kiosk.firejail else ""
    desktop = desktop_of(config)
    # Под KDE с запретом run_desktop_files ярлык не запускается ничем -- KIO
    # отказывает даже системным записям (см. _run_desktop_files_lines). Класть
    # его значило бы обещать пользователю кнопку, которая не работает.
    shortcuts = "" if desktop == DESKTOP_KDE and kiosk.allow_removable_media else "1"
    return f"""#!/bin/sh
# generated by portable-os-admin
# Применяет список разрешённых приложений режима киоска.
set -eu

ALLOWED="{allowed}"
DESKTOP="{desktop}"
USE_FIREJAIL="{use_firejail}"
FIREJAIL_OPTS="{firejail_opts}"
DESKTOP_SHORTCUTS="{shortcuts}"
OVERRIDE_DIR=/{OVERRIDE_DIR}

mkdir -p "$OVERRIDE_DIR"

is_allowed() {{
    for a in $ALLOWED; do
        [ "$a" = "$1" ] && return 0
    done
    return 1
}}

# Скрыть всё, что не разрешено явно.
hidden=0
for f in /usr/share/applications/*.desktop; do
    [ -e "$f" ] || continue
    name=${{f##*/}}
    name=${{name%.desktop}}
    if is_allowed "$name"; then
        continue
    fi
    # Копия исходной записи плюс NoDisplay=true — а не подмена её пустышкой.
    #
    # Раньше здесь создавалась запись с Hidden=true и без Exec, и это ломало
    # сессию целиком. marco, mate-panel и caja — не «приложения из меню», а
    # компоненты самой среды: оконный менеджер, панель и рисовальщик рабочего
    # стола. mate-session читает их .desktop, чтобы запустить их, не находит
    # Exec и не поднимает ни одного. Носитель загружался в чёрный экран:
    # автологин проходил, X работал, а внутри не было ни окон, ни панели.
    #
    # Hidden=true по спецификации значит «записи больше нет», то есть её
    # удалили. NoDisplay=true значит «есть, но не показывать в меню» — ровно
    # то, что нужно: запись остаётся исправной для всего, что запускает её
    # само.
    awk '
        /^\\[Desktop Entry\\]/ {{ print; print "NoDisplay=true"; next }}
        /^NoDisplay[[:space:]]*=/ {{ next }}
        /^Hidden[[:space:]]*=/ {{ next }}
        {{ print }}
    ' "$f" > "$OVERRIDE_DIR/$name.desktop"
    hidden=$((hidden + 1))
done
echo "poa-kiosk: скрыто записей приложений: $hidden"

# Проверить, что каждое разрешённое приложение действительно установлено.
# Молча разрешённая, но отсутствующая запись — это пустое место в меню, о
# котором администратор узнал бы только от пользователя.
for a in $ALLOWED; do
    if [ ! -f "/usr/share/applications/$a.desktop" ]; then
        echo "poa-kiosk: ВНИМАНИЕ: разрешено '$a', но записи /usr/share/applications/$a.desktop нет" >&2
    fi
done

# Ярлыки разрешённых приложений на Рабочем столе.
#
# Так же, как в штатном киоске РЕД ОС: «ярлыки таких приложений находятся на
# Рабочем столе». Это не украшение, а единственный надёжный способ запустить
# приложение в запертой среде — панель заблокирована, а меню может быть
# недоступно. Первый собранный носитель загрузился именно в такое состояние:
# приложения установлены и разрешены, а открыть их нечем.
#
# Кладутся и в /etc/skel (для учётных записей, которые создадут позже), и в
# домашний каталог уже существующего пользователя, потому что порядок шагов
# установки на это не влияет.
placed=0
if [ -z "$DESKTOP_SHORTCUTS" ]; then
    echo "poa-kiosk: ярлыки на рабочем столе не кладутся: запрет run_desktop_files" \
         "не даст их запустить, запуск идёт из меню" >&2
fi
for home in /etc/skel /home/*; do
    [ -n "$DESKTOP_SHORTCUTS" ] || break
    [ -d "$home" ] || continue
    desktop_dir="$home/Desktop"
    mkdir -p "$desktop_dir" || continue
    for a in $ALLOWED; do
        src="/usr/share/applications/$a.desktop"
        [ -f "$src" ] || continue
        rm -f "$desktop_dir/$a.desktop"
        cp "$src" "$desktop_dir/$a.desktop" 2>/dev/null || continue
        # Caja запускает ярлык только если он исполняемый и помечен доверенным.
        chmod 0755 "$desktop_dir/$a.desktop" 2>/dev/null || true
        placed=$((placed + 1))
    done
    # Владелец: домашний каталог пользователя должен остаться его.
    owner=${{home##*/}}
    if [ "$home" != "/etc/skel" ]; then
        chown -R "$owner:$owner" "$desktop_dir" 2>/dev/null || true
    fi
done
echo "poa-kiosk: ярлыков на рабочем столе размещено: $placed"

# Песочница: разрешённые приложения перезапускаются через firejail.
# Только если пакет есть — в образе Debian DVD его нет, и тогда это не отказ
# установки, а работа без дополнительного слоя, о чём сказано в журнале.
if [ -n "$USE_FIREJAIL" ]; then
    if command -v firejail >/dev/null 2>&1; then
        for a in $ALLOWED; do
            src="/usr/share/applications/$a.desktop"
            [ -f "$src" ] || continue
            sed "s|^Exec=|Exec=firejail $FIREJAIL_OPTS |" "$src" > "$OVERRIDE_DIR/$a.desktop"
        done
        echo "poa-kiosk: разрешённые приложения запускаются в песочнице firejail"
    else
        echo "poa-kiosk: firejail не установлен, песочница не применена" >&2
    fi
fi

if [ "$DESKTOP" = "mate" ]; then
    # Скомпилировать системную базу dconf из файлов, записанных установщиком.
    # Без этого шага и настройки, и блокировки остаются просто текстом.
    if command -v dconf >/dev/null 2>&1; then
        dconf update
    else
        echo "poa-kiosk: dconf не установлен, настройки рабочей среды не применены" >&2
    fi
else
{settings_modules_script(config)}
{plasma_layout_script(config)}
    # KDE читает ограничения из /etc/xdg/kdeglobals при запуске приложения,
    # компилировать нечего. Пересобирается лишь кэш меню, чтобы скрытые
    # записи исчезли из него сразу, а не после первого входа.
    for tool in kbuildsycoca6 kbuildsycoca5; do
        if command -v "$tool" >/dev/null 2>&1; then
            "$tool" --noincremental >/dev/null 2>&1 || true
            break
        fi
    done
    if [ ! -f /etc/xdg/kdeglobals ]; then
        echo "poa-kiosk: ВНИМАНИЕ: /etc/xdg/kdeglobals отсутствует, ограничения KDE не применены" >&2
    fi
fi
"""


def launcher_script() -> str:
    """Runs a desktop entry by name, for the autostart entry below.

    `gtk-launch` would do this, but it lives in a package (libgtk-3-bin /
    gtk3-devel-tools) that is not guaranteed to be pulled in, and depending on
    it would mean an autostart that silently does nothing. Reading Exec= out of
    the entry needs nothing but a shell.
    """
    return """#!/bin/sh
# generated by portable-os-admin
# Запуск приложения по имени desktop-записи.
set -eu

app="${1:-}"
if [ -z "$app" ]; then
    echo "usage: poa-kiosk-launch <имя-desktop-записи>" >&2
    exit 2
fi

for dir in /usr/local/share/applications /usr/share/applications; do
    entry="$dir/$app.desktop"
    [ -f "$entry" ] || continue
    # Скрытая запись означает, что приложение не разрешено в этом режиме.
    if grep -qi '^Hidden=true' "$entry"; then
        continue
    fi
    line=$(grep -m1 '^Exec=' "$entry" | cut -d= -f2-)
    [ -n "$line" ] || continue
    # Поля подстановки XDG (%f, %U, %i ...) подставлять нечем: приложение
    # запускается без документа. Оставленные как есть, они приходят
    # приложению как аргумент "%f" и, например, браузером понимаются как URL.
    line=$(printf '%s' "$line" | sed 's/%[fFuUickdDnNvm]//g')
    exec /bin/sh -c "$line"
done

echo "poa-kiosk: приложение '$app' не найдено или не разрешено" >&2
exit 1
"""


def autostart_entry(config: InstallConfig) -> str:
    app = config.kiosk.autostart_app
    return (
        "[Desktop Entry]\n"
        "# generated by portable-os-admin\n"
        "Type=Application\n"
        "Name=Portable OS kiosk application\n"
        f"Exec=/{LAUNCHER_PATH} {app}\n"
        "Terminal=false\n"
        "X-MATE-Autostart-enabled=true\n"
        "NoDisplay=true\n"
    )


# ----------------------------------------------------------------------
# Mount hardening
# ----------------------------------------------------------------------
def hardening_fstab_rows() -> list[tuple[str, str, str, str, str, str]]:
    """fstab rows that take execution away from everything the user can write.

    /home is bind-mounted onto itself purely to carry mount options: it lives
    on the root filesystem, which must stay executable, so the options cannot
    be set on the root mount. /tmp and /var/tmp become tmpfs, which also means
    nothing a session writes there survives a reboot.

    This is the layer that does not depend on any desktop setting being
    honoured -- it is the kernel refusing execve, and it is why a file arriving
    on the exchange volume cannot be run no matter what the user clicks.
    """
    return [
        ("/home", "/home", "none", f"bind,{HARDENED_OPTS}", "0", "0"),
        ("tmpfs", "/tmp", "tmpfs", f"{HARDENED_OPTS},mode=1777", "0", "0"),
        ("tmpfs", "/var/tmp", "tmpfs", f"{HARDENED_OPTS},mode=1777", "0", "0"),
    ]


# ----------------------------------------------------------------------
# What the installer asks for
# ----------------------------------------------------------------------
def config_files(config: InstallConfig) -> dict[str, tuple[str, int | None]]:
    """Relative path -> (content, mode) for everything written into the target.

    Deliberately excludes the display-manager configuration: which one is
    installed is a property of the distribution family, so the backend supplies
    that file while everything here stays shared.
    """
    if not is_enabled(config):
        return {}

    # Shared by both desktops: these restrict the system, not the session, and
    # hold regardless of which shell is drawing the screen.
    files: dict[str, tuple[str, int | None]] = {
        XORG_CONF_PATH: (xorg_conf(), None),
        LOGIND_CONF_PATH: (logind_conf(), None),
        POLKIT_RULES_PATH: (polkit_rules(config), None),
        APPLY_SCRIPT_PATH: (apply_script(config), 0o755),
        LAUNCHER_PATH: (launcher_script(), 0o755),
    }
    session_path, session_content = session_entry(config)
    files[session_path] = (session_content, 0o644)

    if desktop_of(config) == DESKTOP_KDE:
        files[KDEGLOBALS_PATH] = (kdeglobals(config), None)
        files[PLASMARC_PATH] = (plasmarc(config), None)
        files[KSCREENLOCKER_PATH] = (kscreenlockerrc(config), None)
        files[POWERMANAGEMENT_PATH] = (powermanagement_profiles(config), None)
    else:
        files[DCONF_PROFILE_PATH] = (dconf_profile(), None)
        files[DCONF_KEYFILE_PATH] = (dconf_keyfile(config), None)
        files[DCONF_LOCKS_PATH] = (dconf_locks(config), None)
    if config.kiosk.autostart_app:
        files[AUTOSTART_PATH] = (autostart_entry(config), 0o644)
    return files


def chroot_commands(mount_root: str, config: InstallConfig, backend) -> list[list[str]]:
    """Commands run inside the target once its packages are installed.

    Order matters: the whitelist is applied last, after everything that could
    still install a desktop entry has run.
    """
    if not is_enabled(config):
        return []

    commands: list[list[str]] = []

    # Boot into the graphical session rather than a text console.
    commands.append(
        backend.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "set-default", "graphical.target")
    )
    display_manager = backend.display_manager_for(config)
    if display_manager:
        commands.append(backend.systemctl_enable(mount_root, display_manager))

    # The text consoles. DontVTSwitch already keeps X from handing them over,
    # and masking the units means there is nothing listening even if it did.
    #
    # `autovt@.service` belongs in the list even though it looks like a
    # duplicate: the distribution ships it as a symlink to `getty@.service`,
    # so masking only the latter leaves logind able to ask for the former by
    # name. systemd then resolves the alias onto a unit it has already loaded
    # separately -- masked, but loaded -- and refuses the whole request with
    # "failed to load properly ... File exists". Observed on the built medium:
    # logind repeated that failure on every seat event. Masking the alias
    # under its own name gives systemd a fragment to find without resolving
    # anything, and the request is refused cleanly instead of erroring.
    # Снять включение getty@tty1 до маскировки. Дистрибутив включает его сам,
    # и ссылка в getty.target.wants переживает `mask`: юнит остаётся
    # загруженным — пусть и замаскированным, — что и сталкивало его с
    # autovt@tty1. Отказ здесь не должен ронять сборку: юнит может быть и не
    # включён, и тогда systemctl вправе вернуть ненулевой код.
    disable_getty = backend.chroot(
        mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "disable", "getty@tty1.service"
    )
    commands.append(["sh", "-c", f"{shlex.join(disable_getty)} || true"])

    for unit in ("getty@.service", "serial-getty@.service", "autovt@.service"):
        commands.append(
            backend.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "mask", unit)
        )

    commands.append(backend.chroot(mount_root, "/bin/sh", f"/{APPLY_SCRIPT_PATH}"))
    return commands
