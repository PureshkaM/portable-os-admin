"""The restricted desktop.

These tests assert the *shape* of what gets written, which is all a test can
do here: whether MATE honours a lock, whether lightdm autologins into a locked
account and whether `noexec` stops the thing an administrator worried about
are properties of the medium, not of this code, and are recorded as verified or
unverified in README, раздел «Проверки, стенд и уроки».

What they do catch is the failure mode this feature is most prone to -- a
restriction that is generated but inert. Every real defect found while building
it was of that kind: a dconf path that no schema reads, a string that fails to
compile, a script written without an execute bit, an fstab row with a column
missing. Each would have installed cleanly and protected nothing.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

from portable_os_admin.config import InstallConfig
from portable_os_admin.config.schema import KioskConfig
from portable_os_admin.config.validator import BuiltinConfigValidator
from portable_os_admin.install import kiosk
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.disk import build_media_layout, plan_partitions


def make_kiosk_config(target_os: str = "debian", **kiosk_kwargs) -> InstallConfig:
    """Киоск с парой прикладных пакетов.

    Пакеты, а не список разрешённых имён: с 02.09.2026 доступное пользователю
    выводится из того, что поставил администратор. `allowed_apps` в вызовах
    ещё встречается -- он принимается и превращается в пакеты, чтобы старые
    тесты продолжали проверять то, ради чего писались, а не форму вызова.
    """
    config = InstallConfig.create_default("kiosk-stick", target_os)
    packages = kiosk_kwargs.pop("allowed_apps", None)
    config.packages = list(packages) if packages is not None else ["mate-calc", "firefox-esr"]
    config.kiosk = KioskConfig(enabled=True, **kiosk_kwargs)
    config.profile = "kiosk"
    return config


@pytest.fixture
def layout():
    config = InstallConfig.create_default("stick", "debian")
    return build_media_layout(
        plan_partitions("/dev/sdz", config.disk), mapper_name="poa-t", mount_root="/mnt/t"
    )


# ----------------------------------------------------------------------
# The profile switch
# ----------------------------------------------------------------------
def test_console_profile_generates_nothing_of_the_kiosk():
    config = InstallConfig.create_default("stick", "debian")

    assert not kiosk.is_enabled(config)
    assert kiosk.config_files(config) == {}
    assert get_backend("debian").desktop_packages(config) == []


def test_profile_and_enabled_flag_cannot_disagree():
    """Two spellings of one fact, reconciled on construction.

    They are separate fields because the interface talks about a profile while
    the install steps ask `kiosk.enabled`; if those two ever drifted apart the
    result would be a desktop built with none of its restrictions applied.
    """
    by_profile = InstallConfig(name="a", profile="kiosk")
    assert by_profile.kiosk.enabled is True

    by_flag = InstallConfig(name="b", kiosk=KioskConfig(enabled=True))
    assert by_flag.profile == "kiosk"

    plain = InstallConfig(name="c")
    assert plain.profile == "console" and plain.kiosk.enabled is False


def test_configuration_written_before_the_kiosk_existed_still_loads():
    """An older configuration file has no `kiosk` key at all."""
    config = InstallConfig.from_dict({"name": "old", "target_os": "debian", "release": "bookworm"})

    assert config.profile == "console"
    assert config.kiosk.enabled is False


def test_kiosk_configuration_survives_a_round_trip():
    config = make_kiosk_config(autostart_app="firefox-esr", screen_blank_minutes=10)

    restored = InstallConfig.from_dict(config.to_dict())

    assert restored.kiosk.enabled is True
    assert restored.packages == ["mate-calc", "firefox-esr"]
    assert restored.kiosk.hidden_launchers == []
    assert restored.kiosk.autostart_app == "firefox-esr"
    assert restored.kiosk.screen_blank_minutes == 10


# ----------------------------------------------------------------------
# dconf
# ----------------------------------------------------------------------
def test_every_setting_written_is_also_locked():
    """Otherwise the settings are defaults the user simply changes back."""
    config = make_kiosk_config()
    keyfile = kiosk.dconf_keyfile(config)
    locks = kiosk.dconf_locks(config)

    path = None
    written = []
    for line in keyfile.splitlines():
        line = line.strip()
        if line.startswith("["):
            path = line.strip("[]")
        elif line and not line.startswith("#") and "=" in line and path:
            written.append(f"/{path}/{line.split('=', 1)[0]}")

    assert written, "конфигурация киоска не записала ни одной настройки"
    for key in written:
        assert key in locks, f"настройка {key} записана, но не заблокирована"


def test_dconf_paths_are_the_ones_mate_actually_reads():
    """Regression: the session schema lives under desktop/, not at the top.

    `org.mate.session` has path /org/mate/desktop/session/. Written as
    /org/mate/session/ the key compiles fine and is read by nothing, so the
    screen never blanks and nothing reports a problem.
    """
    config = make_kiosk_config(screen_blank_minutes=5)
    keyfile = kiosk.dconf_keyfile(config)

    assert "[org/mate/desktop/session]" in keyfile
    assert "[org/mate/session]" not in keyfile
    assert "[org/mate/desktop/lockdown]" in keyfile
    assert "[org/mate/panel/general]" in keyfile


def test_string_values_are_gvariant_quoted():
    """Regression: an unquoted string aborts the whole `dconf update`.

    A dconf keyfile is GVariant text. `mode=blank-only` is a syntax error, and
    the failure is not local -- the database does not compile, so *every*
    restriction in it is missing from the medium.
    """
    config = make_kiosk_config(screen_blank_minutes=5)

    assert "mode='blank-only'" in kiosk.dconf_keyfile(config)


def test_the_run_command_dialog_is_always_disabled():
    """It is a launcher for anything installed, whitelist or not."""
    assert "disable-command-line=true" in kiosk.dconf_keyfile(make_kiosk_config())


def test_lock_screen_is_disabled_whatever_the_blanking_setting():
    """The account password is locked, so a lock screen strands the user.

    Blanking and locking are different things here, and only the first is
    offered -- see KioskConfig.screen_blank_minutes.
    """
    for minutes in (0, 15):
        keyfile = kiosk.dconf_keyfile(make_kiosk_config(screen_blank_minutes=minutes))
        assert "disable-lock-screen=true" in keyfile
        assert "lock-enabled=false" in keyfile


def test_blanking_delay_reaches_the_session():
    assert "idle-delay=15" in kiosk.dconf_keyfile(make_kiosk_config(screen_blank_minutes=15))
    assert "idle-delay=0" in kiosk.dconf_keyfile(make_kiosk_config(screen_blank_minutes=0))


def test_removable_media_is_refused_by_default_and_can_be_allowed():
    refused = kiosk.dconf_keyfile(make_kiosk_config())
    assert "automount=false" in refused
    assert "autorun-never=true" in refused

    allowed = kiosk.dconf_keyfile(make_kiosk_config(allow_removable_media=True))
    assert "automount=true" in allowed


# ----------------------------------------------------------------------
# polkit
# ----------------------------------------------------------------------
def test_pkexec_is_refused_for_the_kiosk_account():
    """The general-purpose "run as root" door, closed before the rest."""
    config = make_kiosk_config()
    config.account.username = "operator"
    rules = kiosk.polkit_rules(config)

    assert 'subject.user !== "operator"' in rules
    assert "org.freedesktop.policykit.exec" in rules
    assert "polkit.Result.NO" in rules


def test_polkit_rules_leave_other_users_alone():
    """An administrator with a root shell must not be fighting this policy."""
    rules = kiosk.polkit_rules(make_kiosk_config())
    assert "polkit.Result.NOT_HANDLED" in rules


def test_shutdown_permission_follows_the_configuration():
    allowed = kiosk.polkit_rules(make_kiosk_config(allow_shutdown=True))
    refused = kiosk.polkit_rules(make_kiosk_config(allow_shutdown=False))

    assert "org.freedesktop.login1.power-off" in allowed
    # The same action id appears in both; what differs is the verdict returned
    # for it, so the test compares the two rather than grepping one.
    assert allowed != refused


# ----------------------------------------------------------------------
# Mount hardening
# ----------------------------------------------------------------------
def test_everything_writable_is_mounted_noexec(layout):
    """The layer that holds even if every desktop setting is ignored."""
    config = make_kiosk_config()
    fstab = get_backend("debian").config_files(config, layout)["etc/fstab"]

    rows = {
        line.split("\t")[1]: line.split("\t")[3]
        for line in fstab.splitlines()
        if line and not line.startswith("#") and len(line.split("\t")) >= 4
    }
    for mountpoint in ("/home", "/tmp", "/var/tmp", "/mnt/exchange"):
        assert mountpoint in rows, f"{mountpoint} отсутствует в fstab"
        assert "noexec" in rows[mountpoint], f"{mountpoint} монтируется с правом запуска"
        assert "nosuid" in rows[mountpoint]


def test_every_fstab_row_has_all_six_columns(layout):
    """Regression: the exchange row once lost its filesystem type.

    Five columns instead of six shifts the options into the type field, and the
    medium boots without the exchange volume mounted -- or, without `nofail`,
    not at all.
    """
    for config in (make_kiosk_config(), InstallConfig.create_default("s", "debian")):
        fstab = get_backend("debian").config_files(config, layout)["etc/fstab"]
        for line in fstab.splitlines():
            if not line or line.startswith("#"):
                continue
            assert len(line.split("\t")) == 6, f"строка fstab неполна: {line!r}"


def test_console_profile_keeps_the_exchange_volume_executable(layout):
    config = InstallConfig.create_default("stick", "debian")
    fstab = get_backend("debian").config_files(config, layout)["etc/fstab"]

    assert "/home" not in fstab
    assert "defaults,nofail" in fstab


# ----------------------------------------------------------------------
# Session and login
# ----------------------------------------------------------------------
def test_kiosk_replaces_the_console_autologin_with_the_display_manager(layout):
    config = make_kiosk_config()
    files = get_backend("debian").config_files(config, layout)

    assert "etc/lightdm/lightdm.conf.d/50-poa-kiosk.conf" in files
    # A getty autologin as well would leave a text console -- reachable, and
    # exactly what the mode removes.
    assert not any("getty@tty1" in path for path in files)


def test_console_profile_still_gets_its_getty_autologin(layout):
    config = InstallConfig.create_default("stick", "debian")
    files = get_backend("debian").config_files(config, layout)

    assert any("getty@tty1" in path for path in files)
    assert not any("lightdm" in path for path in files)


def test_text_consoles_are_masked_and_the_session_is_graphical():
    config = make_kiosk_config()
    commands = get_backend("debian").chroot_setup_commands("/mnt/t", config)
    flat = [" ".join(argv) for argv in commands]

    assert any("set-default graphical.target" in c for c in flat)
    assert any("enable lightdm" in c for c in flat)
    assert any("mask getty@.service" in c for c in flat)


def test_the_getty_alias_is_masked_under_its_own_name():
    """Regression, найдено по журналу носителя: logind на каждом событии места
    писал "Failed to start autovt@tty1.service ... File exists".

    Дистрибутив поставляет `autovt@.service` ссылкой на `getty@.service`.
    Замаскировав только второй, мы оставляем logind возможность просить первый
    по имени; systemd разрешает псевдоним на уже загруженный юнит и отвергает
    запрос целиком. Маска под собственным именем даёт ему фрагмент, который
    находится без разрешения ссылки.
    """
    flat = [
        " ".join(argv)
        for argv in get_backend("debian").chroot_setup_commands("/mnt/t", make_kiosk_config())
    ]

    assert any("mask autovt@.service" in c for c in flat)
    # И включение getty@tty1 снимается до маскировки: ссылка в
    # getty.target.wants переживает `mask` и держит юнит загруженным.
    disable_at = next(i for i, c in enumerate(flat) if "disable getty@tty1.service" in c)
    mask_at = next(i for i, c in enumerate(flat) if "mask getty@.service" in c)
    assert disable_at < mask_at
    # Юнит может быть и не включён -- отказ не должен ронять сборку.
    assert "|| true" in flat[disable_at]


def test_no_virtual_console_is_reserved_for_a_getty():
    """ReserveVT называет консоль, которую logind резервирует под getty, а не
    ту, на которой живёт сеанс. Просить её на системе без текстовой консоли
    значит просить запускать autovt@, которого здесь быть не должно."""
    conf = kiosk.logind_conf()

    assert "NAutoVTs=0" in conf
    assert "ReserveVT=0" in conf


def test_whitelist_is_applied_after_the_packages_are_installed():
    """A launcher installed after the whitelist ran would stay visible."""
    config = make_kiosk_config()
    config.packages = ["mate-calc"]
    flat = [" ".join(a) for a in get_backend("debian").chroot_setup_commands("/mnt/t", config)]

    apply_at = next(i for i, c in enumerate(flat) if "poa-kiosk-apply" in c)
    install_at = next(i for i, c in enumerate(flat) if "apt-get install" in c)
    assert install_at < apply_at


# ----------------------------------------------------------------------
# Generated scripts
# ----------------------------------------------------------------------
def test_scripts_are_written_executable(layout):
    """A script written 0644 is present, correct and never runs."""
    files = get_backend("debian").scripted_files(make_kiosk_config(), layout)

    for path in (kiosk.APPLY_SCRIPT_PATH, kiosk.LAUNCHER_PATH):
        assert path in files, f"{path} не записан"
        assert files[path][1] == 0o755, f"{path} записан без права на запуск"


def test_the_script_asks_the_package_manager_instead_of_being_told():
    """Список доступного собирает сама система, а не мы при сборке.

    Разница не косметическая. Раньше имена вписывались в скрипт руками, и
    разойтись с действительностью им ничего не мешало: прогон 02.09.2026
    разрешил «atril», пакета с ним не поставил, и меню оказалось пустым --
    ошибка, неотличимая от исправной настройки. Теперь в скрипт едут имена
    пакетов, а запускатели он выясняет у менеджера пакетов на месте.
    """
    config = make_kiosk_config(allowed_apps=["mate-calc", "atril"])
    script = kiosk.apply_script(config)

    assert 'POA_PACKAGES="atril mate-calc"' in script
    assert 'PKG_FILES="dpkg -L"' in script, "Debian спрашивает dpkg"
    assert "/usr/share/applications/" in script, "ищутся именно запускатели"

    redos_script = kiosk.apply_script(make_kiosk_config("redos", allowed_apps=["mate-calc"]))
    assert 'PKG_FILES="rpm -ql"' in redos_script, "РЕД ОС спрашивает rpm"


def test_launchers_the_administrator_unchecked_are_subtracted():
    """Единственная ручная правка списка -- вычитание.

    Один пакет нередко ставит несколько запускателей: LibreOffice -- шесть.
    Снять лишние администратор может, добавить несуществующее -- нет.
    """
    config = make_kiosk_config(allowed_apps=["libreoffice-writer"])
    config.kiosk.hidden_launchers = ["libreoffice-startcenter"]
    script = kiosk.apply_script(config)

    assert 'POA_HIDDEN="libreoffice-startcenter"' in script
    assert "is_hidden" in script


def test_a_medium_where_nothing_can_be_started_says_so_at_install_time():
    """Пустое меню -- это то, о чём администратор узнаёт от пользователя.

    Поэтому система говорит об этом сама, в журнале установки, пока носитель
    ещё в руках у того, кто его собирает.
    """
    script = kiosk.apply_script(make_kiosk_config())

    assert "ни одного приложения" in script
    assert "пользователю доступны приложения" in script


def test_firejail_is_only_wired_in_when_asked_for():
    assert 'USE_FIREJAIL=""' in kiosk.apply_script(make_kiosk_config())
    assert 'USE_FIREJAIL="1"' in kiosk.apply_script(make_kiosk_config(firejail=True))


def _with_revocation(config):
    config.revocation.enabled = True
    config.revocation.url = "https://revoke.example.org"
    return config


def test_allowed_applications_go_through_the_session_gate_only_with_revocation():
    from portable_os_admin.install import revocation

    assert 'SESSION_GATE=""' in kiosk.apply_script(make_kiosk_config())

    script = kiosk.apply_script(_with_revocation(make_kiosk_config()))
    assert f'SESSION_GATE="/{revocation.GATE_PATH}"' in script
    # Шлюз -- первым, песочница -- за ним.
    assert 'PREFIX="$SESSION_GATE $PREFIX"' in script
    assert script.index('PREFIX="firejail $FIREJAIL_OPTS "') < script.index(
        'PREFIX="$SESSION_GATE $PREFIX"'
    )


def test_a_dbus_activatable_entry_cannot_slip_past_the_gate():
    """Такую запись среда запускает через D-Bus, не читая Exec=."""
    script = kiosk.apply_script(_with_revocation(make_kiosk_config()))
    assert "/^DBusActivatable[[:space:]]*=/d" in script


def test_desktop_shortcuts_point_at_the_gated_entry():
    """Ссылка на /usr/share/applications шла бы мимо шлюза."""
    script = kiosk.apply_script(_with_revocation(make_kiosk_config()))
    gate_at = script.index('PREFIX="$SESSION_GATE $PREFIX"')
    shortcuts_at = script.index('src="$OVERRIDE_DIR/$a.desktop"')
    assert gate_at < shortcuts_at, "ярлыки кладутся раньше, чем появились записи со шлюзом"


def test_autostart_waits_for_the_session_to_be_authorized():
    from portable_os_admin.install import revocation

    plain = kiosk.autostart_entry(make_kiosk_config(autostart_app="mate-calc"))
    assert "--wait" not in plain

    gated = kiosk.autostart_entry(_with_revocation(make_kiosk_config(autostart_app="mate-calc")))
    assert f"Exec=/{revocation.GATE_PATH} --wait /{kiosk.LAUNCHER_PATH} mate-calc" in gated


def test_the_mate_window_list_is_pinned_to_the_start_edge_and_locked():
    """Задача 24: список окон без кнопок на Debian + MATE.

    mate-panel 1.27 делал группу, не касающуюся края, «центральной», и
    растягиваемый список окон оставался без ширины. Лечится запертым
    `relative-to-edge='start'` -- в базе dconf, а не в раскладке: mate-panel
    1.28 у РЕД ОС объект раскладки с незнакомым ключом отбрасывает целиком.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config()
    section = f"[org/mate/panel/objects/{mate.WINDOW_LIST_OBJECT}]"
    keyfile = mate.dconf_keyfile(config)
    assert section in keyfile
    assert "relative-to-edge='start'" in keyfile
    assert f"position={mate.WINDOW_LIST_POSITION}" in keyfile

    locks = mate.dconf_locks(config)
    assert f"/org/mate/panel/objects/{mate.WINDOW_LIST_OBJECT}/relative-to-edge" in locks
    assert f"/org/mate/panel/objects/{mate.WINDOW_LIST_OBJECT}/position" in locks

    layout = mate.panel_layout(config)
    assert f"[Object {mate.WINDOW_LIST_OBJECT}]" in layout
    assert f"position={mate.WINDOW_LIST_POSITION}" in layout
    assert "relative-to-edge" not in layout, "ключ в раскладке уронит объект на РЕД ОС"


def test_autostart_entry_only_exists_when_an_application_is_named(layout):
    without = get_backend("debian").scripted_files(make_kiosk_config(), layout)
    assert kiosk.AUTOSTART_PATH not in without

    with_app = get_backend("debian").scripted_files(
        make_kiosk_config(autostart_app="mate-calc"), layout
    )
    assert kiosk.AUTOSTART_PATH in with_app
    assert "mate-calc" in with_app[kiosk.AUTOSTART_PATH][0]


# ----------------------------------------------------------------------
# Packages
# ----------------------------------------------------------------------
@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_no_terminal_or_control_centre_is_installed(target_os):
    """The strongest restriction of the mode: the tools are simply absent."""
    packages = get_backend(target_os).desktop_packages(make_kiosk_config(target_os))

    forbidden = (
        "mate-terminal",
        "mate-control-center",
        "mate-system-monitor",
        "caja-open-terminal",
        "gparted",
        "xterm", "openssh-server", "openssh-client", "openssh-clients", "gir1.2-vte-2.91", "vte291",
        # The distribution's desktop metapackages, each of which drags the
        # above back in.
        "mate-desktop-environment",
        "task-mate-desktop",
        "@mate-desktop",
    )
    for name in forbidden:
        assert name not in packages, f"{name} не должен ставиться в режиме киоска"


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_the_desktop_can_actually_start(target_os):
    """A session needs a server, a session manager, a window manager and a DM."""
    backend = get_backend(target_os)
    packages = backend.desktop_packages(make_kiosk_config(target_os))

    assert any("Xorg" in p or p == "xserver-xorg" for p in packages)
    assert "mate-session-manager" in packages
    assert "marco" in packages
    assert backend.display_manager_service
    # dconf, or the lockdown database is never compiled.
    assert any(p.startswith("dconf") for p in packages)


def test_the_two_families_describe_the_desktop_by_the_same_roles():
    """Найденное на одной системе обязано попадать в обе.

    До 02.09.2026 наборы лежали в бэкендах целиком и совпадали на десять
    пакетов из двенадцати у MATE, а объяснения были
    скопированы слово в слово. Именно в такой форме и потерялся пакет схем
    GSettings: правку внесли бы в один файл и не заметили второй, а обошлось
    бы это в сборку, запись и семь минут ожидания загрузки.

    Тест держит форму описания: роли общие, бэкенд называет только исключения.
    """
    from portable_os_admin.install import desktop_roles

    for desktop in ("mate",):
        roles = desktop_roles.ROLES[desktop]
        assert roles, f"{desktop}: роли не описаны"
        # Каждая роль объясняет, зачем она в наборе: набор собран вручную
        # именно затем, чтобы лишнего не было.
        for role in roles:
            assert role.why.strip(), f"{desktop}/{role.key}: роль без объяснения"
        keys = [role.key for role in roles]
        assert len(keys) == len(set(keys)), f"{desktop}: роль названа дважды"

    # Исключений должно быть мало -- иначе общего описания нет, есть два
    # списка, записанных другим способом.
    for target_os in ("debian", "redos"):
        overrides = get_backend(target_os).DESKTOP_PACKAGE_OVERRIDES
        named = sum(len(v) for v in overrides.values())
        assert named <= 2, f"{target_os}: исключений {named} -- описание разошлось"


def test_every_role_of_the_session_is_actually_installed():
    """Роль без пакета -- это компонент, которого в системе не окажется.

    Проверяется на обоих семействах разом: смысл выноса ролей в том, что
    ответ «чем исполняется эта роль» есть у каждой системы.
    """
    from portable_os_admin.install import desktop_roles

    for target_os in ("debian", "redos"):
        backend = get_backend(target_os)
        for desktop in ("mate",):
            config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
            config.kiosk.desktop_environment = desktop
            config.kiosk.screen_blank_minutes = 15
            packages = backend.desktop_packages(config)
            for role in desktop_roles.ROLES[desktop]:
                name = backend.DESKTOP_PACKAGE_OVERRIDES.get(desktop, {}).get(
                    role.key, role.package
                )
                if not name:
                    continue  # роли у этого семейства нет, и это сказано явно
                assert name in packages, (
                    f"{target_os}/{desktop}: роль {role.key} не исполняет никто"
                )


def test_the_schemas_package_is_named_as_a_role_not_as_a_habit():
    """Схемы GSettings -- отдельная роль, и это цена шести чёрных экранов.

    `mate-panel` зависит только от библиотеки `libmate-desktop-2.so.17`, и на
    этом цепочка обрывается: сами схемы приходят другим пакетом. Пока это было
    записано двумя строками в двух списках, ошибку было не видно; ролью её
    видно сразу -- у роли есть имя и объяснение.
    """
    from portable_os_admin.install import desktop_roles

    schemas = [r for r in desktop_roles.MATE_ROLES if r.key == "settings-schemas"]
    assert len(schemas) == 1
    assert "org.mate.interface" in schemas[0].why

    for target_os in ("debian", "redos"):
        config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
        config.kiosk.desktop_environment = "mate"
        assert "mate-desktop" in get_backend(target_os).desktop_packages(config)


def test_a_package_can_be_traced_back_to_its_role():
    """«Не поставился mate-desktop» ничего не говорит тому, кто читает журнал.

    «Не поставились схемы настроек» объясняет, почему сеанс не поднимется, --
    поэтому обратный поиск по имени пакета есть и он проверяется.
    """
    from portable_os_admin.install import desktop_roles

    overrides = get_backend("debian").DESKTOP_PACKAGE_OVERRIDES
    assert desktop_roles.role_of("mate", "mate-desktop") == "settings-schemas"
    assert desktop_roles.role_of("mate", "lightdm", overrides) == "display-manager"
    assert desktop_roles.role_of("mate", "чего-нет") == ""


def test_screensaver_is_only_installed_when_the_screen_should_blank():
    assert "mate-screensaver" not in get_backend("debian").desktop_packages(make_kiosk_config())
    assert "mate-screensaver" in get_backend("debian").desktop_packages(
        make_kiosk_config(screen_blank_minutes=10)
    )


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def _errors(config) -> list[str]:
    return BuiltinConfigValidator().validate(config).errors


def test_a_kiosk_with_no_application_packages_is_refused():
    """Система собирается минимальной, и без прикладных пакетов в ней пусто.

    Это единственное решение, которое администратор обязан принять сам: какие
    программы нужны конкретному киоску, за него не угадать.
    """
    config = make_kiosk_config(allowed_apps=[])
    assert any("хотя бы один пакет" in e for e in _errors(config))


def test_hidden_launcher_names_must_be_desktop_entry_names():
    """Не путь, не имя пакета -- и не команда оболочки.

    Имена попадают в скрипт, который выполняется внутри целевой системы, и
    имя, которое не может нести косую черту или кавычку, из него не вырвется.
    """
    for bad in ("/usr/bin/sh", "a; rm -rf /", "app.desktop; echo", "../../etc/passwd"):
        config = make_kiosk_config()
        config.kiosk.hidden_launchers = [bad]
        assert _errors(config), f"{bad!r} принято как имя"

    config = make_kiosk_config()
    config.kiosk.hidden_launchers = ["libreoffice-startcenter"]
    assert not _errors(config)


def test_autostart_application_cannot_be_one_that_was_hidden():
    """Иначе приложение запускалось бы, но открыть его заново было бы нечем.

    Проверить автозапуск против списка доступного здесь больше нельзя: список
    складывается уже в собранной системе. Несуществующее имя ловит установка и
    называет его в журнале -- эта проверка ловит противоречие, видное сразу.
    """
    config = make_kiosk_config(allowed_apps=["mate-calc"], autostart_app="mate-calc")
    config.kiosk.hidden_launchers = ["mate-calc"]
    assert any("автозапуска" in e for e in _errors(config))

def test_firejail_options_without_firejail_are_refused():
    config = make_kiosk_config(firejail=False, firejail_options="--net=none")
    assert any("песочница выключена" in e for e in _errors(config))


def test_a_complete_kiosk_configuration_validates():
    config = make_kiosk_config(
        allowed_apps=["mate-calc"], autostart_app="mate-calc", screen_blank_minutes=10
    )
    assert _errors(config) == []


# ----------------------------------------------------------------------
# Рабочая среда и её пакеты
# ----------------------------------------------------------------------
def test_exchange_path_is_the_same_in_both_modules():
    """kiosk.py repeats it rather than importing, to avoid an import cycle.

    A silent divergence would put the URL restriction on a directory that is
    not where the exchange volume is mounted, so the rule would allow nothing
    and the user could not open their own files.
    """
    from portable_os_admin.install.backends.base import EXCHANGE_MOUNTPOINT

    assert kiosk.EXCHANGE_DIR == EXCHANGE_MOUNTPOINT


def test_mate_stays_the_default():
    """An existing configuration must not change desktop underneath its author."""
    assert InstallConfig(name="a").kiosk.desktop_environment == "mate"
    assert kiosk.desktop_of(make_kiosk_config()) == "mate"


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_mate_writes_dconf_and_no_kde_files(target_os, layout):
    backend = get_backend(target_os)
    config = make_kiosk_config(target_os)
    files = dict(backend.config_files(config, layout))
    files.update(backend.scripted_files(config, layout))

    assert kiosk.DCONF_KEYFILE_PATH in files
    # Файлов упразднённой среды KDE на носителе нет.
    assert "etc/xdg/kdeglobals" not in files
    if target_os == "debian":
        assert not any("sddm" in path for path in files), "на Debian вход -- lightdm"


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_the_desktop_gets_the_full_driver_set(target_os):
    """A medium is carried to hardware nobody has seen yet.

    Shipping only the driver for the administrator's own card gives a stick
    that shows a black screen on the next machine, which is the one failure a
    portable system may not have.
    """
    backend = get_backend(target_os)
    for desktop in ("mate",):
        config = make_kiosk_config(target_os, desktop_environment=desktop)
        packages = backend.desktop_packages(config)
        if target_os == "debian":
            assert "xserver-xorg-video-all" in packages
            assert "xserver-xorg-input-all" in packages
        else:
            drivers = [p for p in packages if p.startswith("xorg-x11-drv-")]
            assert len(drivers) >= 12, f"драйверов слишком мало: {drivers}"
            assert "linux-firmware" in packages


def test_debian_installs_the_firmware_the_image_hides_from_its_index():
    """The Debian image publishes only main and contrib.

    So firmware-iwlwifi and the rest cannot be installed by name, and a medium
    built from it reaches unfamiliar hardware with no wireless. They are on the
    image as loose .deb files, and this is what picks them up.
    """
    backend = get_backend("debian")
    argv = backend.firmware_install_command("/mnt/t", "/mnt/poa-image-x")

    assert argv is not None
    script = argv[-1]
    assert "/mnt/poa-image-x/firmware" in script
    # A missing firmware package must not end an hour-long install.
    assert "exit 0" in script
    # *-installer packages download during configuration, which cannot work on
    # the isolated network this project is built for.
    assert "-installer_" in script

    assert backend.firmware_install_command("/mnt/t", "") is None


def test_redos_needs_no_firmware_step():
    """It packages the firmware in its own repository, so it installs by name."""
    assert get_backend("redos").firmware_install_command("/mnt/t", "/mnt/img") is None
    assert "linux-firmware" in get_backend("redos").desktop_packages(make_kiosk_config("redos"))


def test_unknown_desktop_is_refused():
    config = make_kiosk_config(desktop_environment="gnome")
    assert any("рабочая среда" in e for e in _errors(config))


# ----------------------------------------------------------------------
# Сеанс X11
# ----------------------------------------------------------------------
def test_the_session_entry_is_this_program_s_own():
    """Имя сеанса -- своё, постоянное на всех дистрибутивах; сеанс -- X11."""
    path, content = kiosk.session_entry(make_kiosk_config())

    assert path == f"usr/share/xsessions/{kiosk.SESSION_NAME}.desktop"
    assert "Exec=/usr/bin/mate-session" in content
    assert "Type=XSession" in content


def test_console_switching_is_refused_by_polkit_not_only_by_xorg():
    """Второй рубеж за DontVTSwitch: запрос к logind на переключение отклонён."""
    rules = kiosk.polkit_rules(make_kiosk_config())

    assert "org.freedesktop.login1.chvt" in rules
    # And the spare consoles are not allocated at all.
    assert "NAutoVTs=0" in kiosk.logind_conf()


def test_the_xorg_lockdown_is_written():
    config = make_kiosk_config()
    files = kiosk.config_files(config)

    assert kiosk.XORG_CONF_PATH in files
    assert "DontVTSwitch" in files[kiosk.XORG_CONF_PATH][0]
    assert kiosk.LOGIND_CONF_PATH in files


def test_display_manager_asks_for_our_session_by_name(layout):
    for target_os in ("debian", "redos"):
        backend = get_backend(target_os)
        config = make_kiosk_config(target_os)
        files = backend.config_files(config, layout)
        dm_file = next(path for path in files if "sddm" in path or "lightdm" in path)
        assert kiosk.SESSION_NAME in files[dm_file]


# ----------------------------------------------------------------------
# Install speed
# ----------------------------------------------------------------------
def test_packages_are_installed_through_eatmydata_inside_the_target():
    """Regression: the accelerator silently did nothing on cross-arch builds.

    `maybe_eatmydata` prefixes the *host's* copy, which cannot be preloaded
    into a process of another architecture -- measured on this project's own
    arm64→amd64 run, where every package logged "libeatmydata.so ... cannot be
    preloaded ... ignored". The install then sat waiting on fsync with the CPU
    idle: load average 5, everything in D-state behind a USB stick and dm-crypt.

    Resolved inside the target instead, at run time, because the package is not
    there yet the first time this runs.
    """
    backend = get_backend("debian")
    argv = backend.package_install_command("/mnt/t", ["mate-panel"])

    script = argv[-1]
    assert "/usr/bin/eatmydata" in script
    # Falls back rather than failing where it is absent.
    assert "else run=" in script
    assert "apt-get install -y --no-install-recommends" in script


def test_eatmydata_is_installed_early_enough_to_help():
    """It has to be in before the hundreds of desktop packages, not after."""
    config = make_kiosk_config()
    boot = get_backend("debian").boot_packages(config)

    assert "eatmydata" in boot
    assert boot.index("eatmydata") == 0


def test_package_names_cannot_break_out_of_the_shell():
    """The command now goes through `sh -c`, so quoting has to hold."""
    argv = get_backend("debian").package_install_command("/mnt/t", ["ok", "a b; rm -rf /"])

    assert "'a b; rm -rf /'" in argv[-1]


def test_firmware_does_not_rebuild_the_initramfs_once_per_package():
    """Regression: thirty initramfs rebuilds in a row.

    Every firmware package fires initramfs-tools' trigger, so installing them
    one by one rebuilt the initramfs after each -- observed on a real run as
    `initramfs-tools.postinst triggered update-initramfs` between packages,
    minutes apiece under emulation. The initramfs this program ships is built
    later anyway, once crypttab and the recovery hooks are in place.
    """
    argv = get_backend("debian").firmware_install_command("/mnt/t", "/mnt/img")
    script = argv[-1]

    assert "--no-triggers" in script
    # ...and the deferred triggers are run once, at the end.
    assert "--triggers-only --pending" in script
    assert script.index("--no-triggers") < script.index("--triggers-only")


def test_firmware_step_repairs_what_dpkg_could_not_resolve():
    """Regression: one half-installed package blocked every later apt command.

    `dpkg -i` installs without satisfying dependencies. `intel-microcode` ships
    in the image's /firmware directory and depends on `iucode-tool`, which does
    not -- so it stayed half-installed and the install died two steps later,
    105 minutes in, with "Unmet dependencies" while installing the kiosk's own
    applications. The dependency is in the image's index, so apt can fetch it.
    """
    script = get_backend("debian").firmware_install_command("/mnt/t", "/mnt/img")[-1]

    assert "--fix-broken install" in script
    # After the packages, not before them.
    assert script.index("--no-triggers") < script.index("--fix-broken")
    # And it reports what is still broken rather than leaving it to be found
    # two steps later.
    assert "осталось ненастроенных" in script


# ----------------------------------------------------------------------
# Cleanup
# ----------------------------------------------------------------------
def test_the_package_cache_is_not_carried_on_the_medium():
    """It is dead weight: those .deb files will never be read again.

    Measured mid-install on a real run: 144 files and 48 MB before the desktop
    was even installed. The apt lists go too -- they index the installation
    image, which does not exist once the stick is unplugged.
    """
    script = get_backend("debian").package_cleanup_command("/mnt/t")[-1]

    assert "apt-get clean" in script
    assert "/var/lib/apt/lists" in script

    redos = get_backend("redos").package_cleanup_command("/mnt/t")
    assert "clean" in redos and "all" in redos


def test_a_console_system_is_cleaned_up_too():
    """Nothing about this depends on the kiosk profile."""
    assert get_backend("debian").package_cleanup_command("/mnt/t") is not None


def test_the_image_mount_point_is_removed_after_unmounting(tmp_path):
    """Regression: one empty directory left behind per install.

    `mount()` always creates the directory, but `unmount()` only removed it
    when the *path* had been chosen by the object. Nine stale
    /mnt/poa-image-<id> directories had accumulated on the test machine.
    """
    from portable_os_admin.install.image_source import MountedImage

    mountpoint = tmp_path / "poa-image-test"
    image = MountedImage(str(tmp_path / "fake.iso"), mountpoint=str(mountpoint))

    # Pretend the mount succeeded: this test is about the bookkeeping, not
    # about loop devices, which need root.
    mountpoint.mkdir()
    image._created_mountpoint = True
    image._mounted = True
    image.unmount()

    assert not mountpoint.exists(), "точка монтирования образа осталась после размонтирования"


def test_hidden_entries_keep_their_exec_line():
    """Regression: the medium booted to a black screen.

    The override used to be a stub with Hidden=true and no Exec. But marco,
    mate-panel and caja are not menu entries -- they are components of the
    desktop itself, and mate-session reads their .desktop files to start them.
    With no Exec it started none of them: autologin worked, X ran, and there
    was nothing inside. Only a real boot showed this; every test passed.
    """
    script = kiosk.apply_script(make_kiosk_config(allowed_apps=["mate-calc"]))

    # The original file is copied and annotated, not replaced by a stub: the
    # source entry is read and written through to the override.
    assert '"$f" > "$OVERRIDE_DIR/$name.desktop"' in script
    assert 'print "NoDisplay=true"' in script
    # Nothing writes a Hidden line any more (the words survive only in the
    # comment explaining why).
    assert "printf 'Hidden" not in script
    # And any Hidden/NoDisplay already in the source is dropped, so the two
    # cannot contradict each other.
    assert "/^Hidden[[:space:]]*=/" in script
    assert "/^NoDisplay[[:space:]]*=/" in script


def test_allowed_applications_get_desktop_icons():
    """Regression: a kiosk with nothing the user can start.

    The first medium booted into exactly that: applications installed and
    allowed, the panel locked, the menu not opening -- and no icons anywhere.
    RED OS puts the allowed applications on the desktop for this reason, and so
    does this now.
    """
    script = kiosk.apply_script(make_kiosk_config(allowed_apps=["mate-calc"]))

    assert "/etc/skel" in script, "ярлыки не попадут в учётные записи, созданные позже"
    assert "Desktop" in script
    # Caja refuses to launch a .desktop that is not executable.
    assert "chmod 0755" in script
    assert "chown" in script, "ярлыки останутся принадлежать root"


def test_the_generated_script_is_valid_shell_in_every_combination():
    """`sh -n` по всему скрипту, а не по глазам.

    Скрипт исполняется в chroot на носителе, и синтаксическая ошибка в нём
    всплывает только на живой установке -- через час работы, когда носитель уже
    стёрт. Раньше синтаксис проверяли руками; с 12.09.2026 проверки «на месте»
    делают два общих помощника, и сломать их можно сразу для всех четырёх
    комбинаций одной правкой.
    """
    import subprocess

    for target in ("redos", "debian"):
        for desktop in ("mate",):
            script = kiosk.apply_script(
                make_kiosk_config(
                    target,
                    desktop_environment=desktop,
                    allowed_apps=["mate-calc"],
                    allowed_settings=["network"],
                )
            )
            checked = subprocess.run(
                ["sh", "-n"], input=script, text=True, capture_output=True
            )
            assert checked.returncode == 0, f"{target} + {desktop}: {checked.stderr}"

            # Помощник объявлен ровно один раз и до первого обращения: иначе
            # `set -eu` уронит скрипт на первой же проверке.
            for helper in ("poa_probe_file", "poa_probe_cmd"):
                assert script.count(f"{helper}() {{") == 1, f"{helper}: не одно объявление"
                call = f"$({helper} "
                if call in script:
                    assert script.index(f"{helper}() {{") < script.index(call)


def test_the_program_list_is_not_switched_off_wholesale():
    """Regression: the menu stopped opening, so nothing could be started.

    Limiting the menu is done per entry with NoDisplay. Turning the whole
    program list off is blunter, unnecessary, and made the medium unusable.
    """
    keyfile = kiosk.dconf_keyfile(make_kiosk_config())

    assert "enable-program-list=false" not in keyfile
    # The panel is still locked against being rearranged.
    assert "locked-down=true" in keyfile


# ----------------------------------------------------------------------
# Разделение по средам
# ----------------------------------------------------------------------
def test_the_script_carries_only_the_mate_fragment():
    """Кусок среды выбирается при генерации, а не по `$DESKTOP` на целевой
    системе: чужого (бывшего KDE) куска в скрипте нет."""
    mate = kiosk.apply_script(make_kiosk_config(allowed_apps=["mate-calc"]))
    assert "dconf update" in mate
    assert "kbuildsycoca" not in mate
    assert "DESKTOP_SHORTCUT" not in mate


def test_the_public_names_survived_the_split():
    """Пакет заменил модуль, и всё, чем пользуются снаружи, осталось на месте."""
    for name in ("config_files", "chroot_commands", "apply_script",
                 "dconf_keyfile", "polkit_rules", "logind_conf", "session_entry",
                 "hardening_fstab_rows", "HARDENED_OPTS", "EXCHANGE_DIR",
                 "DESKTOP_MATE", "SESSION_NAME"):
        assert hasattr(kiosk, name), name
    # Имена упразднённой среды KDE ушли вместе с ней.
    for gone in ("DESKTOP_KDE", "kdeglobals", "wants_wayland", "SESSION_WAYLAND"):
        assert not hasattr(kiosk, gone), gone


def test_mate_is_chosen_regardless_of_what_the_image_carries():
    """Что бы ни нёс образ, среда киоска -- MATE (KDE упразднён 01.10.2026)."""
    from types import SimpleNamespace

    from portable_os_admin.install.kiosk.environments import base

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    env = base.for_config(config, SimpleNamespace(desktop_versions={"kde": "6.3.6"}))
    assert type(env).__name__ == "MateEnvironment"


# ----------------------------------------------------------------------
# Программная поверхность и обособленность
# ----------------------------------------------------------------------
def test_services_the_kiosk_never_asked_for_are_masked():
    """На собранном носителе РЕД ОС в журнале работали sshd и avahi.

    Ни один не был заказан — оба пришли с дистрибутивом. Носитель попадает в
    чужую сеть, где каждая слушающая служба это и поверхность атаки, и
    объявление о себе. Маскируются, а не выключаются: выключенную поднимет
    обратно первое же обновление пакета.
    """
    flat = [
        " ".join(argv)
        for argv in get_backend("redos").chroot_setup_commands("/mnt/t", make_kiosk_config())
    ]

    for unit in ("sshd.service", "ssh.socket", "sshd@.service", "debug-shell.service", "avahi-daemon.service", "smbd.service"):
        assert any(f"mask {unit}" in c for c in flat), unit
    # Отсутствие службы в образе — норма, а не повод уронить сборку.
    assert all("|| true" in c for c in flat if "avahi" in c)


def test_the_host_swap_is_never_taken_and_hibernation_is_off():
    """Обособленность: из машины берутся процессор и память, и ничего больше.

    Чужой своп означал бы страницы памяти пользователя, записанные открытым
    текстом на чужой диск; noresume — что systemd не поднимет чужую систему из
    найденного на диске образа гибернации.
    """
    cmdline = get_backend("debian").kernel_cmdline(InstallConfig.create_default("s", "debian"))

    assert "noresume" in cmdline
    assert "nohibernate" in cmdline
    flat = [
        " ".join(argv)
        for argv in get_backend("debian").chroot_setup_commands("/mnt/t", make_kiosk_config())
    ]
    assert any("mask swap.target" in c for c in flat)


def test_dma_is_closed_without_making_the_medium_unbootable():
    """IOMMU включается там, где он есть, но не требуется безусловно.

    `iommu=force` на части машин мешает загрузке, а носитель обязан грузиться
    на железе, которого никто не видел: защита, из-за которой система не
    стартует, защищает только от пользователя.
    """
    cmdline = get_backend("redos").kernel_cmdline(InstallConfig.create_default("s", "redos"))

    assert "intel_iommu=on" in cmdline and "amd_iommu=on" in cmdline
    assert "efi=disable_early_pci_dma" in cmdline
    assert "iommu=force" not in cmdline


def test_a_carried_in_drive_is_refused_at_the_device_level(layout):
    """Выключенного автомонтирования мало: оно закрывает удобный путь, а не
    возможность. Накопитель не авторизуется, поэтому устройства для него не
    появляется вовсе.

    Запрет включает служба, а правило живёт в /run, и это не оформление.
    Правило, положенное в /etc, применяется при coldplug -- systemd после
    перехода из initramfs шлёт `udevadm trigger --action=add` по всем уже
    существующим устройствам, включая тот, с которого загрузились. Носитель
    деавторизовывал сам себя: `Synchronizing SCSI cache`, отказ ввода-вывода,
    корень перемонтирован только для чтения. Найдено прогоном 28.08.2026.
    """
    import re

    from portable_os_admin.install.kiosk.common import (
        UDEV_RULES_PATH,
        UDEV_RUNTIME_RULES_PATH,
        USB_LOCKDOWN_RULES_PATH,
        USB_LOCKDOWN_UNIT_PATH,
    )

    refused = get_backend("debian").scripted_files(make_kiosk_config(), layout)
    # В /etc правила быть не должно -- именно оно отключало носитель.
    assert UDEV_RULES_PATH not in refused
    assert USB_LOCKDOWN_UNIT_PATH in refused
    unit = refused[USB_LOCKDOWN_UNIT_PATH][0]
    assert 'ATTR{authorized}="0"' in refused[USB_LOCKDOWN_RULES_PATH][0]
    assert UDEV_RUNTIME_RULES_PATH in unit and f"/{USB_LOCKDOWN_RULES_PATH}" in unit
    # Каждая строка -- секция, комментарий или ключ=значение: многострочные
    # правила в ExecStart рвали строку, и systemd отвергал юнит целиком
    # (журнал носителя РЕД ОС, 03.10.2026).
    for line in unit.splitlines():
        assert not line or re.match(r"^(#|\[[A-Za-z]+\]$|[A-Za-z]+=)", line), line
    # После того как система поднялась, а не раньше: до этого момента правило
    # достаётся загрузочному носителю.
    assert "After=multi-user.target" in unit

    # Разрешив носители, администратор получает обычное поведение — и ни
    # правила, ни службы, которые ничего не делают, на носителе не остаётся.
    allowed = get_backend("debian").scripted_files(
        make_kiosk_config(allow_removable_media=True), layout
    )
    assert UDEV_RULES_PATH not in allowed
    assert USB_LOCKDOWN_UNIT_PATH not in allowed


# ----------------------------------------------------------------------
# Файловый менеджер: его нет ни в пакетах, ни в компонентах сессии
# ----------------------------------------------------------------------
def test_mate_keeps_caja_because_the_session_needs_it():
    """В MATE файловый менеджер -- часть среды, и убрать его нельзя.

    01.09.2026 его убрали, положившись на схему MATE: `draw-background`
    обещает, что фон рисует mate-settings-daemon, а Caja отвечает лишь за
    значки. Обещание оказалось неполным -- сеанс не поднялся вовсе: три
    загрузки подряд, чёрный экран, пустой `.xsession-errors` при собранной
    базе dconf и всех прочих компонентах на месте.

    Вывод записан здесь, чтобы никто не повторил: Caja возвращён, а обзор
    файлов закрывается тремя мерами, каждую из которых проверяют тесты ниже.
    """
    for target_os in ("debian", "redos"):
        config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
        config.kiosk.desktop_environment = "mate"
        packages = get_backend(target_os).desktop_packages(config)
        assert "caja" in packages, f"{target_os}: без caja сеанс MATE не поднимется"


def test_caja_draws_the_desktop_and_opens_no_windows():
    """Мера первая: Caja запускается ключами, при которых окон не бывает.

    `--force-desktop` -- рисовать рабочий стол независимо от настроек и
    окружения; `--no-default-window` -- «создавать окна только для явно
    указанных URI». Оба прочитаны в man caja, а не подобраны.

    Запись подменяет штатную порядком поиска, а не удалением: XDG_DATA_DIRS
    просматривает /usr/local/share раньше /usr/share.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"
    files = mate.MateEnvironment().files(config)

    assert mate.CAJA_DESKTOP_ENTRY_PATH.startswith("usr/local/share/applications/")
    entry, _mode = files[mate.CAJA_DESKTOP_ENTRY_PATH]
    assert "--force-desktop" in entry
    assert "--no-default-window" in entry
    assert "NoDisplay=true" in entry, "запись компонента не должна быть в меню"


def test_the_session_uses_our_entry_not_the_packaged_one():
    """Мера вторая: сессия запускает именно нашу запись.

    Список обязательных компонентов остаётся полным -- windowmanager, panel и
    filemanager, -- иначе mate-session вернёт файловый менеджер сам, по
    описанию собственного ключа: «automatically add the required components
    back to the session at login time if they do get removed». Меняется не
    состав списка, а то, чем в нём является filemanager.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"
    keyfile = mate.dconf_keyfile(config)

    assert "required-components-list=['windowmanager', 'panel', 'filemanager']" in keyfile
    assert "filemanager='poa-caja-desktop'" in keyfile


def test_what_else_is_closed_around_caja():
    """Мера третья: всё, чем окно можно было бы открыть в обход.

    Командная строка панели, диалоги сохранения и открытия, выбор приложения
    для незнакомого типа файла, значки на рабочем столе, домашний каталог по
    двойному щелчку. Каждый пункт -- отдельная дверь к обзору файловой
    системы; ключа, закрывающего сам файловый менеджер, в схеме MATE нет
    вовсе (там девять ключей lockdown, и такого среди них не значится).
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"
    keyfile = mate.dconf_keyfile(config)

    for key in (
        "disable-command-line=true",
        "disable-save-to-disk=true",
        "disable-application-handlers=true",
        "computer-icon-visible=false",
        "home-icon-visible=false",
        "desktop-is-home-dir=false",
    ):
        assert key in keyfile, f"не закрыто: {key}"

    # И всё это заперто, а не предложено: незапертая настройка -- умолчание,
    # которое пользователь перепишет.
    locks = mate.dconf_locks(config)
    assert "/org/mate/desktop/lockdown/disable-command-line" in locks



def test_the_panel_is_built_from_our_layout_not_the_distribution_one():
    """Мера четвёртая, и она закрывает дверь мимо всех предыдущих.

    Прогон 02.09.2026: сеанс MATE поднялся, меню оказалось пустым, значков на
    столе не было -- а на панели стоял запускатель Caja, и щелчок по нему
    открыл полный обзор файловой системы, с деревом «File System» и «Browse
    Network». Запускатель панели -- собственный объект панели, а не запись
    меню, поэтому ни NoDisplay, ни запреты lockdown его не касались.

    Пришёл он из раскладки, которую РЕД ОС считает своей: умолчание ключа
    `default-layout` там 'ventanaXP', а в ней три запускателя -- caja-browser,
    mate-terminal и chromium-browser.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"

    keyfile = mate.dconf_keyfile(config)
    assert f"default-layout='{mate.PANEL_LAYOUT_NAME}'" in keyfile
    # Незапертый ключ -- это умолчание, а не решение.
    assert "/org/mate/panel/general/default-layout" in mate.dconf_locks(config)

    files = mate.MateEnvironment().files(config)
    layout, _mode = files[mate.PANEL_LAYOUT_PATH]
    assert "object-type=launcher" not in layout, "на панели киоска запускателей быть не должно"
    for forbidden in ("caja", "terminal", "chromium", "firefox"):
        assert forbidden not in layout, f"в раскладке остался {forbidden}"


def test_the_panel_still_shows_what_a_kiosk_needs():
    """Убрать лишнее -- не то же самое, что убрать всё.

    Панель без списка окон и часов не запрёт киоск сильнее, а работать в нём
    станет нельзя: свёрнутое окно будет некуда вернуть.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"
    layout, _mode = mate.MateEnvironment().files(config)[mate.PANEL_LAYOUT_PATH]

    for applet in (
        "WnckletFactory::WindowListApplet",
        "WnckletFactory::ShowDesktopApplet",
        "NotificationAreaAppletFactory::NotificationArea",
        "ClockAppletFactory::ClockApplet",
    ):
        assert applet in layout, f"панель осталась без {applet}"

    # И ни один объект нельзя утащить с панели или удалить.
    assert layout.count("locked=true") >= 4


def test_logging_out_is_offered_only_where_it_leads_somewhere():
    """Кнопка выхода следует тому же ключу lockdown, что и сам выход.

    При запрете выключения `disable-log-out` закрывает выход из сеанса, и
    кнопка на панели осталась бы обманом: нажимается и ничего не делает.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"

    config.kiosk.allow_shutdown = True
    assert "action-type=logout" in mate.panel_layout(config)

    config.kiosk.allow_shutdown = False
    assert "action-type=logout" not in mate.panel_layout(config)


def test_the_menu_is_added_only_where_the_applet_exists():
    """Меню Brisk есть в РЕД ОС и нет в Debian -- проверяет это сама система.

    Объект панели со ссылкой на отсутствующий апплет обернулся бы диалогом
    ошибки при каждом входе. Поэтому в раскладке меню нет вовсе, а фрагмент,
    выполняемый в собранной системе, дописывает его, если файл апплета на
    месте.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"

    layout, _mode = mate.MateEnvironment().files(config)[mate.PANEL_LAYOUT_PATH]
    assert "BriskMenu" not in layout

    fragment = mate.MateEnvironment().apply_fragment(config)
    assert mate.BRISK_APPLET_FILE in fragment
    assert "BriskMenuFactory::BriskMenu" in fragment
    assert f"/{mate.PANEL_LAYOUT_PATH}" in fragment


def test_the_menu_pins_no_settings_shortcut():
    """Схема Brisk по умолчанию закрепляет Центр управления MATE.

    Прогон 02.09.2026 показал кнопку «Control Center» в боковой панели меню
    при пустом `allowed_settings` -- умолчание ключа `pinned-shortcuts` там
    ['matecc.desktop']. Открыть его щелчком не удалось, но предлагать
    настройки в запертом киоске не должно и меню.
    """
    from portable_os_admin.install.kiosk.environments import mate

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "mate"

    # Тип пустого списка указан явно: на «[]» dconf отказывается собирать
    # базу целиком -- «unable to infer type», -- и вместе с ней теряются все
    # остальные ключи киоска. Поймано сборкой 02.09.2026.
    assert "pinned-shortcuts=@as []" in mate.dconf_keyfile(config)
    assert "/com/solus-project/brisk-menu/pinned-shortcuts" in mate.dconf_locks(config)


@pytest.mark.skipif(shutil.which("dconf") is None, reason="dconf на этой машине нет")
def test_the_dconf_database_actually_compiles():
    """Проверка формой не поймала бы того, что поймала сборка 02.09.2026.

    Ключ `pinned-shortcuts` был записан как `[]`, и dconf отказался собирать
    базу целиком: «invalid value: []: unable to infer type». Пустой список не
    несёт типа элементов, а GVariant его требует. Упал при этом не один ключ,
    а вся база -- вместе со всеми запретами киоска, и сборка дистрибутива
    закончилась ошибкой на шаге 6 из 10.

    Форму такой ошибки не видно: строка `pinned-shortcuts=[]` выглядит
    правильной ровно до того, как её прочитает dconf. Поэтому тест не
    сравнивает текст, а вызывает `dconf compile` -- ту же программу, которая
    выполняется на целевой системе.
    """
    for desktop, target_os in (("mate", "redos"), ("mate", "debian")):
        config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
        config.kiosk.desktop_environment = desktop

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for path, (content, _mode) in kiosk.config_files(config).items():
                if "/dconf/db/" not in path:
                    continue
                target = root / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            keydir = root / "etc/dconf/db/local.d"
            assert keydir.is_dir(), "файлы dconf не записаны -- проверять нечего"
            result = subprocess.run(
                ["dconf", "compile", str(root / "local"), str(keydir)],
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, (
                f"{target_os}/{desktop}: dconf не собрал базу: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )


# ----------------------------------------------------------------------
# Схемы GSettings
# ----------------------------------------------------------------------
def test_mate_gets_its_gsettings_schemas():
    """Без схем сеанс MATE не поднимается вовсе.

    Прогон 02.09.2026: `mate-session` падал в первом же `g_object_new` с
    «GLib-GIO-ERROR: Settings schema 'org.mate.interface' is not installed»,
    SDDM возвращался к экрану входа и запускал сеанс снова -- и так по кругу.
    Снаружи это выглядело как чёрный экран без единого сообщения, и найти
    причину удалось только по коредампу в журнале носителя.

    Схемы лежат отдельно от библиотеки: mate-panel требует
    `libmate-desktop-2.so.17` (её несёт mate-desktop-libs), а файлы
    `org.mate.*.gschema.xml` -- в mate-desktop-gschemas у РЕД ОС и
    mate-desktop-common у Debian. Тянет их `mate-desktop`.

    До этого в коде стояло обратное утверждение -- что нужное сеансу придёт
    зависимостью mate-panel. Библиотека приходила, схемы нет, и разницу было
    видно только на живой загрузке.
    """
    for target_os in ("debian", "redos"):
        config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
        config.kiosk.desktop_environment = "mate"
        packages = get_backend(target_os).desktop_packages(config)
        assert "mate-desktop" in packages, (
            f"{target_os}: нет пакета со схемами GSettings -- сеанс не запустится"
        )


# ----------------------------------------------------------------------
# Служебные разделы
# ----------------------------------------------------------------------
def test_service_volumes_are_hidden_from_the_desktop():
    """Служебные разделы носителя не показываются рабочим столом.

    Прогон 04.09.2026: раздел `POA_EXCHANGE` лежал в «Removable Devices»
    меню Plasma. Закрывается там, где рабочий стол берёт список, -- в
    Solid/udisks2, а не в каждой оболочке отдельно: Solid считает том
    незначащим по `HintIgnore`, а тот приходит из udev-переменной
    `UDISKS_IGNORE`.

    **Проверено на стенде 07.09.2026** без сеанса Plasma: том с меткой
    `POA_TESTLBL` показывал `HintIgnore: false`, после правила и `udevadm
    trigger` -- `HintIgnore: true`.

    Правило по метке, а не по имени устройства: `/dev/sdb5` зависит от того,
    куда воткнули носитель, а метку ставим мы при разметке.

    В /etc, в отличие от запрета USB, ему место: правило ничего не
    деавторизует, поэтому coldplug при переходе из initramfs ему безразличен.
    """
    from portable_os_admin.install import kiosk as kiosk_mode
    from portable_os_admin.install.kiosk import common

    rules = common.hidden_volumes_udev_rules()

    assert 'ENV{UDISKS_IGNORE}="1"' in rules, "переменная, по которой udisks ставит HintIgnore, не задана"
    assert 'ENV{ID_FS_LABEL}=="POA_*"' in rules, "правило не по метке носителя"
    assert "authorized" not in rules, "правило что-то деавторизует -- носитель отключит сам себя"

    files = kiosk_mode.config_files(make_kiosk_config())
    assert common.HIDDEN_VOLUMES_RULES_PATH in files, "правило не попадает на носитель"
    assert common.HIDDEN_VOLUMES_RULES_PATH.startswith("etc/udev/rules.d/"), (
        "правило не в /etc: положенное в /run оно переживёт только текущую загрузку"
    )



def test_a_failed_boot_reboots_instead_of_opening_a_shell():
    """Аварийная оболочка initramfs -- root после открытия LUKS: испорченный
    своими руками образ давал бы пользователю ключ носителя. Обе семьи -- без
    оболочки и с перезагрузкой (а с ней -- откат GRUB на прежний слот)."""
    for family in ("debian", "redos"):
        words = get_backend(family).kernel_cmdline(InstallConfig.create_default("s", family)).split()
        assert {"rd.shell=0", "rd.emergency=reboot", "panic=10"} <= set(words)
