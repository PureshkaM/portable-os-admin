"""Режим киоска на KDE Plasma 5.

Отдельный модуль от Plasma 6 намеренно: у них расходятся пути (kservices5
против kservices6), имена инструментов (kcmshell5/6) и устройство меню. Пока
поддерживается только 5 -- ветка на 6 добавляется рядом, а не внутрь.

Главное, что показали живые прогоны: в этой версии запреты KDE Kiosk на
модули настроек не применяются, поэтому лишнее приходится удалять, а не
запрещать. Подробности -- у каждой функции.
"""
from __future__ import annotations

from ....config.schema import InstallConfig
from ..common import DESKTOP_KDE, EXCHANGE_DIR, SESSION_NAME, desktop_of
from .base import DesktopEnvironment

#: KDE reads system-wide Kiosk restrictions from here.
KDEGLOBALS_PATH = "etc/xdg/kdeglobals"
#: Plasma's own settings, locked the same way.
PLASMARC_PATH = "etc/xdg/plasmarc"
KSCREENLOCKER_PATH = "etc/xdg/kscreenlockerrc"
POWERMANAGEMENT_PATH = "etc/xdg/powermanagementprofilesrc"


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

# Сами модули -- плагины, а не записи.
#
# Удаления .desktop мало: «Параметры системы» Plasma 5 берут список из
# плагинов, чьи метаданные лежат внутри .so. Проверено на носителе -- после
# удаления 59 записей «Пользователи», «Уведомления» и «Учётные записи в
# Интернете» остались на месте, потому что читаются отсюда.
plugins_removed=0
for d in /usr/lib64/qt5/plugins/plasma/kcms /usr/lib64/qt5/plugins/kcms \
         /usr/lib/qt5/plugins/plasma/kcms /usr/lib/qt5/plugins/kcms \
         /usr/lib/*/qt5/plugins/plasma/kcms /usr/lib/*/qt5/plugins/kcms; do
    [ -d "$d" ] || continue
    for so in $(find "$d" -name '*.so' 2>/dev/null); do
        n=${{so##*/}}
        is_kept "${{n%.so}}" && continue
        rm -f "$so"
        plugins_removed=$((plugins_removed + 1))
    done
done
echo "poa-kiosk: плагинов модулей настроек удалено: $plugins_removed"

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




class Kde5Environment(DesktopEnvironment):
    """KDE Plasma 5: ограничения — проверки авторизации в самих приложениях.

    Плюс то, чего ими не сделать: в этой версии запреты на модули настроек не
    применяются, а системный лоток собирает состав из установленных
    плазмоидов, — и то, и другое лечится удалением, а не настройкой.
    """

    name = DESKTOP_KDE

    def files(self, config: InstallConfig) -> dict[str, tuple[str, int | None]]:
        return {
            KDEGLOBALS_PATH: (kdeglobals(config), None),
            PLASMARC_PATH: (plasmarc(config), None),
            KSCREENLOCKER_PATH: (kscreenlockerrc(config), None),
            POWERMANAGEMENT_PATH: (powermanagement_profiles(config), None),
        }

    def apply_fragment(self, config: InstallConfig) -> str:
        return (
            settings_modules_script(config)
            + plasma_layout_script(config)
            + """
# KDE читает ограничения из /etc/xdg/kdeglobals при запуске приложения,
# компилировать нечего. Пересобирается лишь кэш меню, чтобы скрытые записи
# исчезли из него сразу, а не после первого входа.
for tool in kbuildsycoca6 kbuildsycoca5; do
    if command -v "$tool" >/dev/null 2>&1; then
        "$tool" --noincremental >/dev/null 2>&1 || true
        break
    fi
done
if [ ! -f /etc/xdg/kdeglobals ]; then
    echo "poa-kiosk: ВНИМАНИЕ: /etc/xdg/kdeglobals отсутствует, ограничения KDE не применены" >&2
fi
"""
        )

    def desktop_shortcuts_work(self, config: InstallConfig) -> bool:
        """Под запретом run_desktop_files ярлык не запускается ничем.

        KIO отказывает даже системным записям (замер на носителе), поэтому
        класть ярлык значило бы обещать кнопку, которая не работает.
        """
        return not config.kiosk.allow_removable_media
