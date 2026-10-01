"""Режим киоска на MATE.

MATE ограничивается через dconf: настройки пишутся системно и запираются,
чтобы пользователь не мог записать их обратно.
"""
from __future__ import annotations

from ....config.schema import InstallConfig
from ..common import DESKTOP_MATE
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

#: Имя нашей раскладки панели -- без «.layout», ровно так его называет ключ
#: `default-layout` схемы org.mate.panel.
PANEL_LAYOUT_NAME = "poa-kiosk"

#: Куда она кладётся. Каталог общий для обоих семейств: раскладки читает сам
#: mate-panel, а не дистрибутив.
PANEL_LAYOUT_PATH = f"usr/share/mate-panel/layouts/{PANEL_LAYOUT_NAME}.layout"
#: Объект списка окон в раскладке и его позиция. Названы одним местом, потому
#: что те же имя и число запираются в dconf (см. `_dconf_settings`): разойдись
#: они, панель держала бы список окон там, где его нет.
WINDOW_LIST_OBJECT = "window-list"
WINDOW_LIST_POSITION = 20

#: Файл апплета меню Brisk. Его наличие проверяется на целевой системе, а не
#: здесь: в РЕД ОС он есть, в Debian 13 пакета brisk-menu нет вовсе (проверено
#: по списку Packages репозитория trixie 02.09.2026).
BRISK_APPLET_FILE = "/usr/share/mate-panel/applets/com.solus_project.brisk.BriskMenu.mate-panel-applet"

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
                # Панель собирается из нашей раскладки, а не из той, что
                # дистрибутив считает своей.
                #
                # Найдено прогоном 02.09.2026. В РЕД ОС умолчание ключа --
                # 'ventanaXP', и эта раскладка ставит на панель три
                # запускателя: caja-browser, mate-terminal и chromium. Двух
                # последних в собранной системе нет, а Caja есть -- и щелчок
                # по нему открывал полноценный обзор файловой системы, с
                # деревом «File System» и «Browse Network». Всё, что закрыто
                # ниже -- меню, значки стола, диалоги, -- панель обходила
                # мимо, потому что запускатель на ней не .desktop-запись в
                # меню, а собственный объект панели.
                ("default-layout", f"'{PANEL_LAYOUT_NAME}'"),
            ],
        ),
        (
            # Список окон привязан к левому краю панели -- и заперт.
            #
            # Задача 24, закрыта 15.09.2026 живой загрузкой Debian + MATE.
            # mate-panel 1.27 сам выбирает, к чему привязать объект, когда
            # сохраняет его положение (`panel_widget_determine_applet_edge_
            # relativity`): группа апплетов, пересекающая середину панели и не
            # касающаяся её краёв, становится «центральной». Наша группа
            # начинается с позиции 10 -- позиция 0 оставлена меню, а у Debian
            # меню нет, -- и растягивающийся список окон получал 'center' и
            # -620. Центральному растягиваемому апплету панель добавляет
            # ширину по меньшему из промежутков слева и справа, а слева их
            # двадцать пикселей: апплет жив, окна есть, кнопок нет. У РЕД ОС
            # меню стоит в нуле, группа касается края -- отсюда «плавающий»
            # дефект. Проверено: с запертым 'start' кнопка окна появляется, с
            # 'center' после того же перезапуска панели пропадает.
            #
            # Ключ `relative-to-edge` есть только в mate-panel 1.27 (Debian);
            # в раскладку его вписать нельзя -- 1.28 у РЕД ОС отвергает объект
            # раскладки с незнакомым ключом целиком. База dconf схему не
            # проверяет, поэтому здесь он лишний у РЕД ОС, но безвреден.
            f"org/mate/panel/objects/{WINDOW_LIST_OBJECT}",
            [
                ("relative-to-edge", "'start'"),
                ("position", str(WINDOW_LIST_POSITION)),
            ],
        ),
        (
            # Меню Brisk (РЕД ОС; в Debian его нет, и раздел там просто ни на
            # что не влияет -- dconf хранит путь, а не схему).
            "com/solus-project/brisk-menu",
            [
                # Умолчание схемы -- ['matecc.desktop'], то есть Центр
                # управления MATE закреплён в боковой панели меню и виден
                # даже тогда, когда `allowed_settings` пуст, а сама запись
                # matecc скрыта из меню. Открыть его щелчком не удалось, но
                # кнопка настроек в запертом киоске не должна и предлагаться.
                #
                # Тип указан явно, и это не украшение: на `[]` компиляция
                # базы падает целиком -- «invalid value: []: unable to infer
                # type», и вместе с ней остаются несобранными все остальные
                # ключи киоска. Пустой список не несёт в себе типа элементов,
                # а GVariant его требует; `@as` и означает «массив строк».
                ("pinned-shortcuts", "@as []"),
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


def panel_layout(config: InstallConfig) -> str:
    """Панель киоска: показывает состояние, но ничего не запускает.

    Раскладка -- это то, из чего mate-panel строит панель при первом входе,
    когда в базе пользователя ещё нет ни одного объекта. Дистрибутив
    подставляет сюда свою (`ventanaXP` в РЕД ОС), и она приносит запускатели:
    файловый менеджер, терминал, браузер. Запускатель панели -- отдельный
    объект, а не запись меню, поэтому ни NoDisplay, ни запреты lockdown его не
    касаются: прогон 02.09.2026 открыл этим щелчком полный обзор файловой
    системы в запертом киоске.

    Здесь оставлено ровно то, что показывает состояние и не ведёт наружу:
    список окон, область уведомлений, часы и кнопка «свернуть всё». Меню
    добавляется отдельно и только там, где оно есть -- см. `apply_fragment`.
    Приложения запускаются с рабочего стола, ярлыками, которые кладёт скрипт
    белого списка.

    Так же устроен и штатный киоск РЕД ОС: его раскладки redkiosk0..3 состоят
    из действий и апплетов, и ни одного запускателя приложений в них нет.
    """
    blocks = [
        "# generated by portable-os-admin\n"
        "# Панель режима киоска. Запускателей здесь нет намеренно.\n"
        "\n"
        "[Toplevel bottom]\n"
        "expand=true\n"
        "orientation=bottom\n"
        "size=28\n",
        # Позиция 0 слева оставлена меню: оно дописывается позже и только там,
        # где есть, а два объекта на одной позиции панель разложила бы по
        # своему усмотрению.
        _panel_applet("show-desktop", "WnckletFactory::ShowDesktopApplet", 10),
        _panel_applet(WINDOW_LIST_OBJECT, "WnckletFactory::WindowListApplet", WINDOW_LIST_POSITION),
        _panel_applet(
            "notification-area",
            "NotificationAreaAppletFactory::NotificationArea",
            20,
            right=True,
        ),
        _panel_applet("clock", "ClockAppletFactory::ClockApplet", 5, right=True),
    ]

    # Выход из сеанса -- только там, где ему есть куда вести. При запрете
    # выключения тот же ключ lockdown снимает и выход, и кнопка на панели
    # осталась бы обманом: нажимается и ничего не делает.
    if config.kiosk.allow_shutdown:
        blocks.append(
            "[Object logout]\n"
            "object-type=action\n"
            "action-type=logout\n"
            "toplevel-id=bottom\n"
            "position=40\n"
            "panel-right-stick=true\n"
            "locked=true\n"
        )
    return "\n".join(blocks)


def _panel_applet(name: str, iid: str, position: int, right: bool = False) -> str:
    """Один апплет раскладки.

    `locked=true` у каждого объекта -- это запрет перетащить и удалить его.
    Он дублирует `locked-down` из dconf намеренно: тот запирает панель
    целиком, этот остаётся при объекте, даже если панель когда-нибудь
    пересоберут.
    """
    return (
        f"[Object {name}]\n"
        "object-type=applet\n"
        f"applet-iid={iid}\n"
        "toplevel-id=bottom\n"
        f"position={position}\n"
        f"panel-right-stick={'true' if right else 'false'}\n"
        "locked=true\n"
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
            PANEL_LAYOUT_PATH: (panel_layout(config), None),
        }

    def apply_fragment(self, config: InstallConfig) -> str:
        # Скомпилировать системную базу dconf из файлов, записанных
        # установщиком. Без этого шага и настройки, и блокировки остаются
        # просто текстом.
        # Меню дописывается в раскладку здесь, а не в самом файле, потому что
        # ответ на вопрос «есть ли меню» даёт только целевая система. В РЕД ОС
        # это Brisk, в Debian 13 пакета brisk-menu нет вовсе, и объект панели
        # со ссылкой на отсутствующий апплет обернулся бы диалогом ошибки при
        # каждом входе -- ровно тем, чего в киоске быть не должно.
        #
        # Без меню киоск работоспособен: приложения запускаются ярлыками с
        # рабочего стола, которые кладёт скрипт белого списка.
        return f"""
MENU_APPLET=$(poa_probe_file "апплет меню Brisk" \\
    "меню на панели не будет, приложения запускаются ярлыками с рабочего стола" \\
    "" "" "{BRISK_APPLET_FILE}")
if [ -n "$MENU_APPLET" ]; then
    cat >> "/{PANEL_LAYOUT_PATH}" <<'POA_MENU'

[Object menu]
object-type=applet
applet-iid=BriskMenuFactory::BriskMenu
toplevel-id=bottom
position=0
panel-right-stick=false
locked=true
POA_MENU
    echo "poa-kiosk: в панель добавлено меню Brisk"
else
    echo "poa-kiosk: меню на панели не будет: апплет Brisk в системе отсутствует," \
         "приложения запускаются ярлыками с рабочего стола"
fi

DCONF=$(poa_probe_cmd "dconf" "настройки рабочей среды не применены" dconf)
[ -z "$DCONF" ] || dconf update
"""
