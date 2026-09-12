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
        "xterm",
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
    пакетов из двенадцати у MATE и на четыре из семи у KDE, а объяснения были
    скопированы слово в слово. Именно в такой форме и потерялся пакет схем
    GSettings: правку внесли бы в один файл и не заметили второй, а обошлось
    бы это в сборку, запись и семь минут ожидания загрузки.

    Тест держит форму описания: роли общие, бэкенд называет только исключения.
    """
    from portable_os_admin.install import desktop_roles

    for desktop in ("mate", "kde"):
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
        assert named <= 4, f"{target_os}: исключений {named} -- описание разошлось"


def test_every_role_of_the_session_is_actually_installed():
    """Роль без пакета -- это компонент, которого в системе не окажется.

    Проверяется на обоих семействах разом: смысл выноса ролей в том, что
    ответ «чем исполняется эта роль» есть у каждой системы.
    """
    from portable_os_admin.install import desktop_roles

    for target_os in ("debian", "redos"):
        backend = get_backend(target_os)
        for desktop in ("mate", "kde"):
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
# KDE: the Kiosk framework
# ----------------------------------------------------------------------
def make_kde_config(target_os: str = "debian", **kiosk_kwargs) -> InstallConfig:
    kiosk_kwargs.setdefault("desktop_environment", "kde")
    return make_kiosk_config(target_os, **kiosk_kwargs)


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


def test_every_restriction_group_is_marked_immutable():
    """[$i] is the whole mechanism.

    Without it these are defaults, and anything ~/.config says later wins --
    which is to say the kiosk would restrict nothing at all.
    """
    text = kiosk.kdeglobals(make_kde_config())

    groups = [line for line in text.splitlines() if line.startswith("[")]
    assert groups, "не записана ни одна группа ограничений"
    for group in groups:
        assert group.endswith("[$i]"), f"группа {group} не помечена неизменяемой"


def test_shell_access_and_command_running_are_refused():
    """The two that close the general-purpose escape routes."""
    text = kiosk.kdeglobals(make_kde_config())

    assert "shell_access=false" in text
    assert "run_command=false" in text
    # Третий, run_desktop_files, ставится не всегда -- см.
    # test_desktop_files_may_be_executed_when_removable_media_are_refused.


def test_file_dialogs_are_confined_to_home_and_the_exchange_volume():
    """Deny first, then allow: the rules are evaluated in order."""
    text = kiosk.kdeglobals(make_kde_config())

    assert "rule_count=6" in text
    assert "rule_1=list,,,,file,,,false" in text
    assert "rule_2=list,,,,file,,$HOME,true" in text
    assert f"rule_3=list,,,,file,,{kiosk.EXCHANGE_DIR},true" in text
    assert "rule_4=open,,,,file,,,false" in text


def test_lock_screen_is_refused_under_kde_too():
    """Same constraint as MATE: the account password is locked."""
    text = kiosk.kdeglobals(make_kde_config(screen_blank_minutes=10))

    assert "lock_screen=false" in text


def test_the_automatic_lock_is_switched_off_where_the_daemon_reads_it():
    """Regression, найдено живым прогоном: носитель запирался сам собой.

    `lock_screen=false` в kdeglobals — проверка KAuthorized, она останавливает
    только запрос блокировки от пользователя. Блокировку по простою делает
    демон, который ничего не спрашивает и читает kscreenlockerrc; без этого
    файла он остаётся при своём умолчании (запереть через пять минут). Пароль
    учётной записи заблокирован, поэтому запертый экран не открыть ничем,
    кроме перезагрузки.
    """
    text = kiosk.kscreenlockerrc(make_kde_config(screen_blank_minutes=10))

    assert "[Daemon][$i]" in text, "без [$i] это лишь умолчание, которое сессия перезапишет"
    assert "Autolock=false" in text
    assert "LockOnResume=false" in text


def test_blanking_is_set_where_plasma_actually_does_it():
    """Гашение экрана в Plasma делает powerdevil, а не блокировщик."""
    ten = kiosk.powermanagement_profiles(make_kde_config(screen_blank_minutes=10))

    assert "[AC][DPMSControl][$i]" in ten
    assert "idleTime=600" in ten
    # powerdevil умеет запереть экран перед гашением -- ровно то, чего здесь
    # быть не должно.
    assert "lockBeforeTurnOff=0" in ten

    off = kiosk.powermanagement_profiles(make_kde_config(screen_blank_minutes=0))
    assert "DPMSControl" not in off


def test_the_kde4_screensaver_file_is_not_written_any_more():
    """Plasma его не читает, поэтому всё написанное в него было мертво."""
    files = kiosk.config_files(make_kde_config(screen_blank_minutes=10))

    assert not any("kscreensaver" in path for path in files)
    assert kiosk.KSCREENLOCKER_PATH in files
    assert kiosk.POWERMANAGEMENT_PATH in files


def test_logout_follows_the_shutdown_setting():
    assert "logout=true" in kiosk.kdeglobals(make_kde_config(allow_shutdown=True))
    assert "logout=false" in kiosk.kdeglobals(make_kde_config(allow_shutdown=False))


def test_autostart_resource_is_only_closed_when_nothing_needs_it():
    """Closing it while this project relies on autostart could disable both.

    The opening it covers -- a .desktop dropped into ~/.config/autostart -- is
    what run_desktop_files closes where that restriction applies at all.
    """
    without = kiosk.kdeglobals(make_kde_config())
    assert "autostart=false" in without

    with_app = kiosk.kdeglobals(make_kde_config(autostart_app="mate-calc"))
    assert "[KDE Resource Restrictions][$i]" not in with_app
    assert "run_desktop_files=false" in kiosk.kdeglobals(
        make_kde_config(autostart_app="mate-calc", allow_removable_media=True)
    )


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_kde_writes_kde_files_and_no_dconf(target_os, layout):
    backend = get_backend(target_os)
    config = make_kde_config(target_os)
    files = dict(backend.config_files(config, layout))
    files.update(backend.scripted_files(config, layout))

    assert kiosk.KDEGLOBALS_PATH in files
    assert kiosk.PLASMARC_PATH in files
    # dconf belongs to the MATE half; writing both would be two half-applied
    # configurations rather than one.
    assert kiosk.DCONF_KEYFILE_PATH not in files


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_mate_writes_dconf_and_no_kde_files(target_os, layout):
    backend = get_backend(target_os)
    config = make_kiosk_config(target_os)
    files = dict(backend.config_files(config, layout))
    files.update(backend.scripted_files(config, layout))

    assert kiosk.DCONF_KEYFILE_PATH in files
    assert kiosk.KDEGLOBALS_PATH not in files


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_kde_session_uses_sddm_and_our_own_session_entry(target_os, layout):
    """SDDM, and the session this program defines rather than a distribution's.

    It used to ask for "plasma" by name, which was wrong in a way no test would
    have caught: that name means the X11 session on Plasma 5 and the Wayland
    one on Plasma 6, so the same configuration would have produced a different
    protocol -- and a different set of restrictions -- on RED OS than on Debian.
    """
    backend = get_backend(target_os)
    config = make_kde_config(target_os)

    assert backend.display_manager_for(config) == "sddm"
    files = backend.config_files(config, layout)
    dm_file = next(path for path in files if "sddm" in path)
    assert f"Session={kiosk.SESSION_NAME}" in files[dm_file]
    assert config.account.username in files[dm_file]


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_kde_kiosk_installs_no_terminal_and_no_settings_centre(target_os):
    packages = get_backend(target_os).desktop_packages(make_kde_config(target_os))

    for name in (
        "konsole",
        "konsole5",  # what RED OS calls it
        "systemsettings",
        "plasma-systemsettings",
        "plasma-discover",
        "dolphin",  # Plasma draws its desktop with plasmashell, not a file manager
        "kde-standard",
        "kwrited",
    ):
        assert name not in packages, f"{name} не должен ставиться в режиме киоска"


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_kde_session_has_what_it_needs_to_start(target_os):
    packages = get_backend(target_os).desktop_packages(make_kde_config(target_os))

    assert "plasma-desktop" in packages
    assert "plasma-workspace" in packages
    assert any(p.startswith("kwin") for p in packages)
    assert "sddm" in packages


@pytest.mark.parametrize("target_os", ["debian", "redos"])
def test_both_desktops_get_the_full_driver_set(target_os):
    """A medium is carried to hardware nobody has seen yet.

    Shipping only the driver for the administrator's own card gives a stick
    that shows a black screen on the next machine, which is the one failure a
    portable system may not have.
    """
    backend = get_backend(target_os)
    for desktop in ("mate", "kde"):
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
    assert "linux-firmware" in get_backend("redos").desktop_packages(make_kde_config("redos"))


def test_unknown_desktop_is_refused():
    config = make_kiosk_config(desktop_environment="gnome")
    assert any("рабочая среда" in e for e in _errors(config))


# ----------------------------------------------------------------------
# X11 and Wayland
# ----------------------------------------------------------------------
def test_x11_is_the_default_session():
    assert InstallConfig(name="a").kiosk.session_type == "x11"
    assert kiosk.wants_wayland(make_kiosk_config()) is False


def test_the_session_entry_is_this_program_s_own():
    """Naming the distribution's session would pick a different protocol.

    "plasma" is the X11 session under Plasma 5 (RED OS 8) and the *Wayland*
    one under Plasma 6 (Debian 13). Asking for it by name would therefore give
    X11 on one distribution and Wayland on the other -- silently, and with
    different security properties, since the X11 lockdown file is not read
    under Wayland.
    """
    path, content = kiosk.session_entry(make_kde_config())

    assert path.endswith(f"{kiosk.SESSION_NAME}.desktop")
    assert "plasma.desktop" not in path
    assert "startplasma-x11" in content


def test_wayland_selects_the_wayland_session_directory_and_binary():
    config = make_kde_config(session_type="wayland")
    path, content = kiosk.session_entry(config)

    assert kiosk.wants_wayland(config) is True
    assert path.startswith("usr/share/wayland-sessions/")
    assert "startplasma-wayland" in content


def test_mate_is_always_x11_whatever_was_asked_for():
    """It has no Wayland session; validation refuses the pairing separately."""
    config = make_kiosk_config(session_type="wayland")

    assert kiosk.wants_wayland(config) is False
    path, content = kiosk.session_entry(config)
    assert path.startswith("usr/share/xsessions/")
    assert "mate-session" in content


def test_console_switching_is_refused_by_polkit_not_only_by_xorg():
    """The restriction has to survive Plasma's move to Wayland.

    Under Wayland xorg.conf is never read, so DontVTSwitch does nothing; the
    compositor asks logind to switch, and that request is what gets refused.
    """
    rules = kiosk.polkit_rules(make_kde_config(session_type="wayland"))

    assert "org.freedesktop.login1.chvt" in rules
    # And the spare consoles are not allocated at all.
    assert "NAutoVTs=0" in kiosk.logind_conf()


def test_the_xorg_lockdown_is_still_written_but_is_not_the_load_bearing_part():
    config = make_kde_config(session_type="wayland")
    files = kiosk.config_files(config)

    assert kiosk.XORG_CONF_PATH in files
    assert "DontVTSwitch" in files[kiosk.XORG_CONF_PATH][0]
    assert kiosk.LOGIND_CONF_PATH in files


def test_wayland_packages_are_added_only_when_asked_for():
    backend = get_backend("debian")
    x11 = backend.desktop_packages(make_kde_config(session_type="x11"))
    wayland = backend.desktop_packages(make_kde_config(session_type="wayland"))

    assert "kwin-wayland" not in x11
    assert "kwin-wayland" in wayland
    # X-only applications are most of what a kiosk allows.
    assert "xwayland" in wayland


def test_wayland_is_refused_where_it_cannot_work():
    """Both refusals are facts about the images, not preferences."""
    mate = make_kiosk_config(session_type="wayland")
    assert any("MATE" in e for e in _errors(mate))

    redos = make_kde_config("redos", session_type="wayland")
    assert any("РЕД ОС" in e for e in _errors(redos))

    assert _errors(make_kde_config("redos", session_type="x11")) == []
    assert _errors(make_kde_config("debian", session_type="wayland")) == []


def test_display_manager_asks_for_our_session_by_name(layout):
    for target_os in ("debian", "redos"):
        backend = get_backend(target_os)
        config = make_kde_config(target_os)
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


def test_desktop_files_may_be_executed_when_removable_media_are_refused():
    """Regression, найдено живым прогоном: киоск KDE не запускал ничего.

    Замер на носителе: с `run_desktop_files=false` KIO отказывает **любой**
    записи, включая `/usr/share/applications/mate-calc.desktop`. Проверено
    четырьмя способами -- прямой запуск бинарника и `gio launch` работают,
    `kioclient5 exec` возвращает 1 и по ярлыку, и по системной записи. То есть
    ограничение убирает рабочий стол и файловый менеджер целиком, а не только
    посторонние записи.

    Поэтому оно ставится ровно тогда, когда есть чему противостоять: если
    пользователю разрешены съёмные носители, чужой ярлык принести можно, и
    запрет нужен. Если нет -- принести неоткуда, и запрет только вредит.
    """
    strict = kiosk.kdeglobals(make_kde_config(allow_removable_media=True))
    assert "run_desktop_files=false" in strict

    usable = kiosk.kdeglobals(make_kde_config(allow_removable_media=False))
    assert "run_desktop_files=false" not in usable


def test_the_bug_report_action_is_not_restricted():
    """Regression: «Параметры системы» падали, не показав окна.

    KStandardAction спрашивает KAuthorized и на запрет возвращает nullptr, а
    не отключённое действие; приложение, которое это не проверяет, падает.
    Найдено перебором на носителе: ключи группы убирали по одному, и живым
    systemsettings становился ровно без action/help_report_bug.
    """
    text = kiosk.kdeglobals(make_kde_config())

    assert "help_report_bug" not in text
    # Остальные ограничения группы остаются: проверено тем же перебором --
    # без каждого из них падение сохранялось.
    assert "shell_access=false" in text
    assert "action/help_contents=false" in text


def test_settings_modules_are_computed_on_the_target_not_listed_here():
    """KDE не умеет whitelist модулей -- только отказ поимённо, а набор
    модулей зависит от образа (в РЕД ОС 8 их 62). Значит список отказов
    строится там, где модули лежат."""
    script = kiosk.settings_modules_script(
        make_kiosk_config(desktop_environment="kde",
                          allowed_settings=["network", "datetime"])
    )

    assert "kcm_networkmanagement" in script
    assert "kcm_clock" in script
    assert "kcm_pulseaudio" not in script, "звук не разрешали"
    # Стартовая страница обязана остаться: без неё приложение падает в
    # SettingsBase::changePlugin, не дойдя до первой отрисовки.
    assert kiosk.SETTINGS_LANDING_MODULE in script
    # Перечисление идёт по установленным файлам, а не по списку из кода, и по
    # признаку модуля, а не по имени: «Внешние носители» зовутся
    # device_automounter_kcm и маску kcm* не проходят.
    assert 'grep -q "KCModule"' in script
    assert "/usr/share/applications/*.desktop" in script
    assert "[KDE Control Module Restrictions]" in script


def test_the_modules_themselves_are_removed_not_just_their_entries():
    """Regression: удаления .desktop оказалось мало.

    «Параметры системы» Plasma 5 берут список из плагинов, чьи метаданные
    лежат внутри .so. Замер на носителе: после удаления 59 записей
    «Пользователи», «Уведомления» и «Учётные записи в Интернете» остались на
    месте; после удаления 62 плагинов в списке осталось два раздела вместо
    восьми.
    """
    script = kiosk.settings_modules_script(
        make_kiosk_config(desktop_environment="kde", allowed_settings=["network"])
    )

    assert "plugins/plasma/kcms" in script
    assert "plugins_removed" in script
    # Разрешённый модуль переживает чистку -- иначе «Параметры» откроются
    # пустыми, а администратор думает, что сеть настраивать можно.
    assert 'is_kept "${n%.so}" && continue' in script


def test_nothing_is_allowed_by_default_and_the_landing_page_stays_shut():
    script = kiosk.settings_modules_script(make_kiosk_config(desktop_environment="kde"))

    assert 'KEEP_MODULES=""' in script
    assert kiosk.SETTINGS_LANDING_MODULE not in script


def test_the_panel_carries_the_allowed_applications_and_locks_itself():
    """Regression: панель приезжала с ярлыком «Параметры системы», а
    «Избранное» меню -- с их же плиткой. Ни того, ни другого администратор не
    разрешал."""
    script = kiosk.plasma_layout_script(
        make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc", "atril"])
    )

    # Список собирается на целевой системе из $ALLOWED -- того, что она сама
    # вычислила по пакетам. Раньше имена вписывались сюда при сборке, и панель
    # могла показывать не то, что установлено.
    assert "for a in $ALLOWED" in script
    assert "applications:$a.desktop" in script
    assert "systemsettings" not in script, "настройки не разрешены -- нечего выносить"
    assert "ordering=" in script, "иначе в избранном останется набор дистрибутива"
    # immutability=2 -- SystemImmutable: пользователь не разблокирует виджеты.
    assert "immutability=2" in script
    assert "/etc/skel" in script


def test_settings_reach_the_panel_only_when_something_is_allowed():
    """Запускатель настроек ищется на носителе, а не называется из кода.

    Имя у «Параметров системы» разное: РЕД ОС кладёт
    `kdesystemsettings.desktop`, Plasma 6 в Debian -- `systemsettings.desktop`.
    Прогон 12.09.2026: мы называли второе, на носителе лежало первое, и
    настройки не попадали никуда -- ни в панель, ни в избранное, ни в меню;
    поиск по «settings» и по «system» в живом сеансе не находил ничего, хотя
    сам `systemsettings` в образе есть.
    """
    with_settings = kiosk.plasma_layout_script(
        make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc"],
                          allowed_settings=["network"])
    )
    # Имя не вписано, а выбрано из тех, что действительно лежат на носителе, --
    # и выбрано общим механизмом проверок «на месте», а не своим кодом здесь.
    assert "SETTINGS_ENTRY=$(poa_probe_file" in with_settings
    assert "/usr/share/applications/ .desktop kdesystemsettings systemsettings)" in with_settings
    # И попадает в тот же список, что и разрешённые приложения.
    assert "for a in $ALLOWED $SETTINGS_ENTRY" in with_settings
    # Если ни одного из имён на носителе нет -- в журнале установки будет и что
    # искали, и чем это обернётся; сама строка печатается общим механизмом.
    assert "разрешённые настройки будут недоступны из меню и панели" in with_settings

    without = kiosk.plasma_layout_script(
        make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc"])
    )
    assert "systemsettings" not in without, "настройки не разрешены -- нечего выносить"
    assert "SETTINGS_ENTRY=" not in without


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
        for desktop in ("kde", "mate"):
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


def test_the_device_notifier_is_removed_rather_than_hidden():
    """Regression, найдено пользователем в живом сеансе: апплет «Диски и
    устройства» вернулся после вычистки из конфигурации и показывал служебные
    разделы носителя с кнопкой «Безопасно извлечь» напротив системного тома.
    Системный лоток собирает состав из установленных плазмоидов, поэтому
    убирать надо сам плазмоид."""
    script = kiosk.plasma_layout_script(
        make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc"])
    )

    assert "org.kde.plasma.devicenotifier" in script
    assert "rm -rf \"/usr/share/plasma/plasmoids/$plasmoid\"" in script


def test_the_plasma_layout_is_kde_only():
    assert kiosk.plasma_layout_script(make_kiosk_config(allowed_apps=["mate-calc"])) == ""


def test_no_desktop_shortcuts_are_promised_where_they_cannot_launch():
    """Ярлык, который не запускается, хуже отсутствующего."""
    blocked = kiosk.apply_script(
        make_kiosk_config(allowed_apps=["mate-calc"], desktop_environment="kde",
                          allow_removable_media=True)
    )
    assert 'DESKTOP_SHORTCUTS=""' in blocked

    working = kiosk.apply_script(
        make_kiosk_config(allowed_apps=["mate-calc"], desktop_environment="kde",
                          allow_removable_media=False)
    )
    assert 'DESKTOP_SHORTCUTS="1"' in working
    # MATE ограничения не знает, поэтому ярлыки у него есть всегда.
    mate = kiosk.apply_script(
        make_kiosk_config(allowed_apps=["mate-calc"], allow_removable_media=True)
    )
    assert 'DESKTOP_SHORTCUTS="1"' in mate


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
def test_each_environment_gets_only_its_own_fragment():
    """Ради этого модули и разнесены.

    До разделения скрипт нёс обе ветки и выбирал по `$DESKTOP` уже на целевой
    системе. Из шести дефектов, найденных живыми прогонами, четыре были одного
    вида: правило написано для одной среды и молча применено к другой. Теперь
    ветка выбирается при генерации, и чужого куска в скрипте просто нет.
    """
    mate = kiosk.apply_script(make_kiosk_config(allowed_apps=["mate-calc"]))
    assert "dconf update" in mate
    assert "kbuildsycoca" not in mate
    assert "$DESKTOP" != "", "переменная осталась для прочих проверок"

    kde = kiosk.apply_script(
        make_kiosk_config(allowed_apps=["mate-calc"], desktop_environment="kde")
    )
    assert "kbuildsycoca" in kde
    assert "dconf update" not in kde


def test_the_environment_answers_whether_shortcuts_work():
    """Причина у каждой среды своя, поэтому и ответ даёт она сама."""
    from portable_os_admin.install.kiosk.environments import base

    kde = base.for_config(make_kiosk_config(desktop_environment="kde",
                                            allow_removable_media=True))
    assert not kde.desktop_shortcuts_work(
        make_kiosk_config(desktop_environment="kde", allow_removable_media=True)
    )
    mate = base.for_config(make_kiosk_config(allow_removable_media=True))
    assert mate.desktop_shortcuts_work(make_kiosk_config(allow_removable_media=True))


def test_the_public_names_survived_the_split():
    """Пакет заменил модуль, и всё, чем пользуются снаружи, осталось на месте."""
    for name in ("config_files", "chroot_commands", "apply_script", "kdeglobals",
                 "dconf_keyfile", "polkit_rules", "logind_conf", "session_entry",
                 "hardening_fstab_rows", "settings_modules_script",
                 "plasma_layout_script", "kscreenlockerrc", "powermanagement_profiles",
                 "HARDENED_OPTS", "EXCHANGE_DIR", "DESKTOP_KDE", "SESSION_NAME"):
        assert hasattr(kiosk, name), name


def test_the_generation_comes_from_the_image_not_from_the_release():
    """Regression, предупреждённый заранее: Debian 13 несёт Plasma 6.

    Проверено чтением настоящих образов 27.08.2026: РЕД ОС 8 — Plasma 5.27.11,
    Debian 13 — Plasma 6.3.6. Ограничения у поколений разные, и выбор не того
    означает молча неприменённые ограничения, а не заметную ошибку.
    """
    from types import SimpleNamespace

    from portable_os_admin.install.kiosk.environments import base

    config = make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc"])

    five = base.for_config(config, SimpleNamespace(desktop_versions={"kde": "5.27.11"}))
    six = base.for_config(config, SimpleNamespace(desktop_versions={"kde": "4:6.3.6-1"}))
    assert type(five).__name__ == "Kde5Environment"
    assert type(six).__name__ == "Kde6Environment"

    # Расходятся не именами классов, а тем, что делают на целевой системе.
    assert "kservices5" in five.apply_fragment(config)
    assert "kbuildsycoca5" in five.apply_fragment(config)
    assert "kservices6" in six.apply_fragment(config)
    assert "kbuildsycoca6" in six.apply_fragment(config)
    assert "qt6" in six.apply_fragment(config)
    assert "qt5" not in six.apply_fragment(config)


def test_mate_is_chosen_regardless_of_what_the_image_carries():
    """Версия KDE в образе не делает киоск на MATE киоском на KDE."""
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

    for unit in ("sshd.service", "avahi-daemon.service", "smbd.service"):
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
    from portable_os_admin.install.kiosk.common import (
        UDEV_RULES_PATH,
        UDEV_RUNTIME_RULES_PATH,
        USB_LOCKDOWN_UNIT_PATH,
    )

    refused = get_backend("debian").scripted_files(make_kiosk_config(), layout)
    # В /etc правила быть не должно -- именно оно отключало носитель.
    assert UDEV_RULES_PATH not in refused
    assert USB_LOCKDOWN_UNIT_PATH in refused
    unit = refused[USB_LOCKDOWN_UNIT_PATH][0]
    assert 'ATTR{authorized}="0"' in unit
    assert UDEV_RUNTIME_RULES_PATH in unit
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
def test_kde_installs_no_file_manager_at_all():
    """У KDE обзор файлов закрывается отсутствием, и это сильнее запретов.

    Рабочий стол Plasma рисует plasmashell, поэтому Dolphin не нужен вовсе.
    Проверено сборками 01.09.2026: в собранных корнях РЕД ОС + KDE и
    Debian + KDE нет ни dolphin, ни caja.
    """
    for target_os in ("debian", "redos"):
        config = make_kiosk_config(target_os, allowed_apps=["mate-calc"])
        config.kiosk.desktop_environment = "kde"
        packages = get_backend(target_os).desktop_packages(config)
        for manager in ("caja", "dolphin", "nautilus", "thunar", "pcmanfm"):
            assert manager not in packages, f"{target_os}: в наборе KDE есть {manager}"


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


def test_the_panel_layout_is_delivered_the_way_plasma_reads_it():
    """Правка чужого файла не работала, и это молчало с самого начала.

    До 04.09.2026 раскладка панели наносилась правкой
    `plasma-org.kde.plasma.desktop-appletsrc` в домашнем каталоге. Этого файла
    при установке ещё нет: Plasma создаёт его при первом входе, уже после
    того, как отработал скрипт. Правка пропускалась молча (`[ -f "$f" ] ||
    continue`), и носитель уезжал с умолчаниями Plasma. Прочитано с живого
    носителя: `immutability=1` и `launchers=applications:systemsettings.desktop,
    applications:org.kde.discover.desktop` -- то есть виджеты не заперты, а в
    панели закреплены «Параметры системы» и «Центр приложений».

    Теперь раскладка отдаётся так, как Plasma её читает: пакетом оформления,
    названным в kdeglobals. Его сценарий выполняется при первом входе.
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config(desktop_environment="kde")
    files = kde.Kde5Environment().files(config)

    assert kde.LOOK_AND_FEEL_METADATA in files, "пакета оформления нет"
    assert kde.LOOK_AND_FEEL_DEFAULTS in files
    assert "poa-kiosk" in files[kde.LOOK_AND_FEEL_METADATA][0]

    settings = kde.kdeglobals(config)
    assert f"LookAndFeelPackage={kde.LOOK_AND_FEEL_NAME}" in settings, (
        "Plasma не узнает, откуда брать раскладку"
    )


def test_widgets_are_locked_by_a_restriction_not_by_a_file_that_may_not_exist():
    """Запрет читает сама оболочка при запуске -- ему не нужен чужой файл.

    `plasma/plasmashell/unlockedDesktop=false` запрещает разблокировать
    виджеты, добавлять и убирать панели и менять избранное в меню. В отличие
    от `immutability=2` в конфигурации пользователя, он действует с первого
    входа.
    """
    from portable_os_admin.install.kiosk.environments import kde

    settings = kde.kdeglobals(make_kiosk_config(desktop_environment="kde"))

    assert "plasma/plasmashell/unlockedDesktop=false" in settings
    # И он в неизменяемой группе -- иначе это лишь умолчание.
    restrictions = settings[settings.index("[KDE Action Restrictions][$i]"):]
    assert "plasma/plasmashell/unlockedDesktop=false" in restrictions


def test_the_panel_pins_the_allowed_applications_and_nothing_else():
    """В панели закрепляется то, что установил администратор.

    По умолчанию Plasma закрепляет «Параметры системы» и «Центр приложений»:
    и то, и другое в киоске недопустимо, и увидели мы их на живом носителе, а
    не вывели рассуждением.
    """
    from portable_os_admin.install.kiosk.environments import kde

    script = kde.plasma_layout_script(make_kiosk_config(desktop_environment="kde"))

    assert "org.kde.plasma.icontasks" in script
    assert "writeConfig('launchers'" in script
    assert "$APPLETSRC_LAUNCHERS" in script, "список должен приходить из вычисленного"
    assert "systemsettings.desktop,applications:org.kde.discover" not in script
    # Переключателя рабочих мест нет: место одно, а виджет -- лишняя дверь.
    assert "org.kde.plasma.pager" not in script


def test_the_desktop_container_is_named_explicitly():
    """Чем рисуется рабочий стол -- решение, а не умолчание дистрибутива."""
    from portable_os_admin.install.kiosk.environments import kde

    defaults = kde.look_and_feel_defaults()

    assert "Containment=org.kde.plasma.folder" in defaults, (
        "стол должен показывать ярлыки разрешённых приложений"
    )
    assert "Containment=org.kde.panel" in defaults


def test_the_generations_do_not_share_a_settings_tree():
    """Разделы у Plasma 5 и 6 разные, и перепутать их дорого.

    Прочитано из образов по `X-KDE-System-Settings-Parent-Category`: в 5.27
    регион лежит под персонализацией
    (`settings-personalization-regionalsettings`), а в 6.3 это раздел верхнего
    уровня (`settings-regionalsettings`), и раскладка получила собственный
    подраздел. Разреши шестому поколению пятое дерево -- и пользователь
    получит либо пустой видимый раздел, либо спрятанное разрешённое. Пустой
    раздел в этом проекте уже находили: «Внешние носители».
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config("debian", allowed_apps=["mate-calc"],
                               allowed_settings=["localisation"])
    five = kde.settings_modules_script(config, kde.KDE5_PATHS)
    six = kde.settings_modules_script(config, kde.KDE6_PATHS)

    assert "settings-personalization-regionalsettings" in five
    assert "settings-regionalsettings" in six
    # Именно то, чего нельзя допустить: персонализация в шестом поколении
    # осталась бы пустой, потому что регион из неё уехал.
    assert "settings-personalization" not in six
    # А в пятом нет раздела верхнего уровня, который есть в шестом.
    assert " settings-regionalsettings" not in five.replace(
        "settings-personalization-regionalsettings", "")


def test_the_keyboard_section_follows_the_generation():
    """Раскладка: в 5.27 под оборудованием, в 6.3 -- собственный подраздел."""
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config("debian", allowed_apps=["mate-calc"],
                               allowed_settings=["keyboard"])
    five = kde.settings_modules_script(config, kde.KDE5_PATHS)
    six = kde.settings_modules_script(config, kde.KDE6_PATHS)

    assert "settings-hardware-input" in five and "settings-hardware" in five
    assert "settings-hardware-keyboard" in six


# ----------------------------------------------------------------------
# Plasma 6: чем шестое поколение отличается от пятого
# ----------------------------------------------------------------------
def test_favourites_are_written_where_each_generation_keeps_them():
    """Избранное Kickoff живёт в двух местах, и писать надо в оба.

    В конфигурации плазмоида org.kde.plasma.kickoff 6.3.6 есть и `favorites`
    (список самого апплета), и `favoritesPortedToKAstats` -- признак того, что
    список перенесён в KActivitiesStats. Какое из двух мест окажется главным,
    зависит от поколения и от истории профиля, поэтому пишутся оба: лишняя
    правка ничего не ломает, а пропущенная оставила бы пользователю чужое
    избранное. На РЕД ОС оно ставило «Параметры системы» в один клик от того,
    кому туда нельзя.
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "kde"

    for paths in (kde.KDE5_PATHS, kde.KDE6_PATHS):
        script = kde.plasma_layout_script(config, paths)
        assert "favorites=$FAVOURITES" in script, "не задано избранное самого апплета"
        assert "ordering=$FAVOURITES" in script, "не задано избранное KAStats"


def test_only_plasma_5_has_applet_service_files_to_delete():
    """Удалить плазмоид в пятом поколении мало: рядом лежит его `.desktop`.

    В Plasma 6 таких записей нет вовсе -- метаданные переехали в
    `metadata.json` внутрь самого плазмоида. Проверено по
    plasma-workspace-data 6.3.6 и plasma-desktop-data 6.3.6 Debian 13: ни
    одной записи в kservices.

    Разница важна не ради экономии одной команды: `rm -f` на отсутствующий
    файл молчит, и без этого поля «удалили» было бы неотличимо от «искали не
    там».
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config(allowed_apps=["mate-calc"])
    config.kiosk.desktop_environment = "kde"

    five = kde.plasma_layout_script(config, kde.KDE5_PATHS)
    six = kde.plasma_layout_script(config, kde.KDE6_PATHS)

    assert "plasma-applet-$plasmoid.desktop" in five
    assert "kservices5" in five
    assert "plasma-applet-" not in six, "шестое поколение ищет то, чего у него нет"
    # Сам плазмоид удаляется в обоих: каталог у поколений один и тот же.
    assert "/usr/share/plasma/plasmoids/$plasmoid" in six


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


def test_the_menu_does_not_pretend_to_have_favourites():
    """Избранное в раскладке не задаётся, и это решение (задача 31).

    Меню киоска -- `kicker` (задача 29, он единственный без вкладки мест), а
    он избранное **не показывает вовсе**: `globalFavorites` рисуется только в
    `DashboardRepresentation.qml`, то есть в полноэкранном `kickerdash`.
    Прогон 07.09.2026 это подтвердил: в меню Office, Utilities, Power /
    Session -- и ничего больше. Значит запись `favoriteApps` при создании
    виджета ничего не рисовала, и её убрали 12.09.2026.

    Полноэкранное меню ради третьего пути к тем же приложениям не берут:
    разрешённое доступно из панели и ярлыком на столе, оба пути подтверждены
    живыми загрузками обоих семейств 12.09.2026.

    Что остаётся -- вычистка чужого избранного из уже существующей
    конфигурации: она не рисует ничего, а убирает то, чего пользователю видеть
    нельзя. На РЕД ОС там оказывались «Параметры системы».
    """
    from portable_os_admin.install.kiosk.environments import kde

    script = kde.plasma_layout_script(make_kiosk_config(desktop_environment="kde"))

    assert "menu.writeConfig('favoriteApps'" not in script, (
        "мёртвая запись избранного вернулась: kicker её не рисует"
    )
    assert "menu.writeConfig('favoritesPortedToKAstats'" not in script
    # Вычистка чужого избранного -- остаётся.
    assert "favoriteApps=" in script and "favorites=" in script, (
        "перестали вычищать чужое избранное из существующей конфигурации"
    )


def test_the_install_log_names_the_restrictions_that_are_in_force():
    """Что запрещено на носителе -- видно в журнале, а не по памяти.

    Прогон 04.09.2026: ярлык на рабочем столе Plasma отвечал «You are not
    authorized to execute this file», и первым делом пришлось выяснять, какие
    ограничения вообще действуют, -- монтировать корень и читать kdeglobals
    глазами. Установка теперь называет их сама, тем же приёмом «проверять на
    месте, а не предполагать», что и остальные проверки целевой системы.
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config(desktop_environment="kde")
    fragment = kde.Kde5Environment().apply_fragment(config)

    assert "poa-kiosk: ограничения KDE:" in fragment, "перечень не попадает в журнал"
    # Читается из того же файла, который мы пишем, а не из списка в коде:
    # список в коде отвечает на вопрос «что мы собирались», а нужен ответ
    # «что оказалось на носителе».
    assert "/etc/xdg/kdeglobals" in fragment



# ----------------------------------------------------------------------
# Задача 29: места и служебные разделы
# ----------------------------------------------------------------------
def test_the_places_list_hides_every_group():
    """Список мест пуст, и наполнить его заново KIO не может.

    Прогон 04.09.2026 показал в меню Plasma «Home», «Desktop», «Trash»,
    «Network», «Recent» и служебный раздел носителя `POA_EXCHANGE`. Их даёт
    `KFilePlacesModel`, и закрывается он не поштучно, а группами: фильтр меню
    (`computermodel.cpp:53`) спрашивает `isHidden()`, а тот -- «скрыт элемент
    или скрыта его группа» (`kfileplacesmodel.cpp:486`). Групповой ключ живёт
    в метаданных корня и действует на записи, которых в файле ещё нет, -- в
    том числе на устройство, которое подключат завтра.

    Два условия проверяются здесь потому, что нарушение любого возвращает
    список целиком:

    * скрыты все семь групп, а не те, что вспомнились;
    * в файле есть закладка и объявлена версия выше той, которую KIO чинит.
      Файл без закладок считается новым (`newFile = root.first().isNull()`),
      а файл со старой версией KIO дополняет умолчаниями по своему списку.
    """
    from portable_os_admin.install.kiosk.environments import kde

    places = kde.user_places(make_kiosk_config(desktop_environment="kde"))

    for group in ("Places", "Remote", "RecentlySaved", "SearchFor",
                  "Devices", "RemovableDevices", "Tags"):
        assert f"<GroupState-{group}-IsHidden>true</GroupState-{group}-IsHidden>" in places, (
            f"группа {group} осталась видимой"
        )
    assert "<bookmark href=" in places, (
        "в файле нет ни одной закладки -- KIO сочтёт его новым и наполнит умолчаниями"
    )
    assert kde.PLACES_VERSION > 4, (
        "версия не выше той, что KIO чинит (в 6.13 s_currentVersion = 4)"
    )
    assert f"<kde_places_version>{kde.PLACES_VERSION}</kde_places_version>" in places


def test_the_places_list_reaches_the_account_that_already_exists():
    """Файл мест кладётся и в /etc/skel, и в готовые домашние каталоги.

    Тот же разрыв, на котором дважды обжигались панель (задача 8) и избранное
    (задача 31): при установке домашнего каталога может ещё не быть -- тогда
    работает /etc/skel, -- а может уже быть, и тогда `useradd -m` файл не
    копировал, потому что в /etc/skel его на тот момент не было.
    """
    from portable_os_admin.install.kiosk.environments import kde

    config = make_kiosk_config(desktop_environment="kde")
    files = kde.Kde5Environment().files(config)
    script = kde.plasma_layout_script(config)

    assert kde.USER_PLACES_PATH in files, "файл мест не попадает на носитель"
    assert kde.USER_PLACES_PATH.startswith("etc/skel/"), "файл мимо /etc/skel"
    assert f'cp "/{kde.USER_PLACES_PATH}"' in script, (
        "готовые домашние каталоги остаются со своим списком мест"
    )


def test_the_menu_has_no_places_tab():
    """Меню киоска -- kicker: у kickoff вкладку мест отключить нечем.

    Ключа нет ни в одном поколении: прочитано в `contents/config/main.xml`
    плазмоида из самих образов -- Plasma 5.27.11 РЕД ОС и 6.3.6 Debian, --
    вкладка задана прямо в `Footer.qml`. У kicker её нет вовсе: ни `Places`,
    ни `ComputerModel` в его QML не встречаются, проверено в обоих
    поколениях.

    Kickoff при этом удаляется из системы. Сам он не вернётся -- панель
    собирается нашим сценарием, а виджеты заперты, -- но пока он лежит на
    месте, он остаётся тем меню, которое покажет служебный раздел носителя,
    стоит раскладке однажды не примениться.
    """
    from portable_os_admin.install.kiosk.environments import kde

    script = kde.plasma_layout_script(make_kiosk_config(desktop_environment="kde"))

    assert "panel.addWidget('org.kde.plasma.kicker')" in script, "меню без вкладки мест не поставлено"
    assert "panel.addWidget('org.kde.plasma.kickoff')" not in script, "в панель ставится меню с местами"
    assert "org.kde.plasma.kickoff" in kde.REMOVED_PLASMOIDS, "меню с местами остаётся в системе"


def test_the_menu_does_not_remember_what_was_launched():
    """Недавние приложения и документы в киоске не показываются.

    Носитель переходит из рук в руки, и список «что открывали» -- это
    сведения о предыдущем пользователе, выданные следующему. У kickoff таких
    ключей нет вовсе; у kicker есть, и это вторая причина, по которой меню
    именно он.

    `useExtraRunners` -- о том же, но про строку поиска: с ним она подключает
    krunner_shell и три других runner'а (умолчание прочитано в main.xml).
    Сам shell-runner в киоске молчит -- он спрашивает `run_command` и
    `shell_access`, а оба запрещены (`shellrunner.cpp:31`), -- но запрет не
    должен быть единственным, что стоит между поиском и запуском команды.
    """
    from portable_os_admin.install.kiosk.environments import kde

    script = kde.plasma_layout_script(make_kiosk_config(desktop_environment="kde"))

    assert "menu.writeConfig('showRecentApps', 'false')" in script
    assert "menu.writeConfig('showRecentDocs', 'false')" in script
    assert "menu.writeConfig('useExtraRunners', 'false')" in script


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

    files = kiosk_mode.config_files(make_kiosk_config(desktop_environment="kde"))
    assert common.HIDDEN_VOLUMES_RULES_PATH in files, "правило не попадает на носитель"
    assert common.HIDDEN_VOLUMES_RULES_PATH.startswith("etc/udev/rules.d/"), (
        "правило не в /etc: положенное в /run оно переживёт только текущую загрузку"
    )


def test_the_kde_desktop_shortcut_is_a_link_to_the_system_entry():
    """Ярлык KDE -- ссылка на системную запись, а не её копия.

    Прогон 07.09.2026, живая загрузка: двойной щелчок по копии спрашивал
    «This will start the program…», а после согласия отвечал «You are not
    authorized to execute this file». Главный подозреваемый прошлого разбора
    -- `shell_access` -- **проверен и отвергнут**: запрет сняли прямо на
    носителе, загрузились снова, и отказ остался слово в слово.

    Настоящий механизм прочитан в исходниках: доверие к `.desktop` решает
    `KDesktopFile::isAuthorizedDesktopFile` (kf6-kconfig 6.13,
    `kdesktopfile.cpp:98`), и первый её критерий -- лежит ли **канонический**
    путь файла в каталоге приложений XDG. Символическая ссылка на
    `/usr/share/applications/…` проходит эту проверку; копия в домашнем
    каталоге до неё не доходит и попадает в путь с предупреждением о
    недоверенной программе (`applicationlauncherjob.cpp:145`) и повторной
    проверкой уже после согласия (`kprocessrunner.cpp:238`).

    У MATE ответ обратный, и это не непоследовательность: Caja запускает
    ярлык, если файл исполняемый, а ссылку на удалённый пакетом файл он
    показывает битой.
    """
    from portable_os_admin.install.kiosk import script
    from portable_os_admin.install.kiosk.environments import kde, mate

    assert kde.Kde5Environment.desktop_shortcuts_are_links is True
    assert mate.MateEnvironment.desktop_shortcuts_are_links is False

    kde_script = script.apply_script(make_kiosk_config(desktop_environment="kde"))
    assert 'DESKTOP_SHORTCUT_LINKS="1"' in kde_script, "KDE кладёт ярлык копией"
    assert 'ln -s "$src"' in kde_script, "в сценарии нет ссылки"

    mate_script = script.apply_script(make_kiosk_config(desktop_environment="mate"))
    assert 'DESKTOP_SHORTCUT_LINKS=""' in mate_script, "MATE кладёт ярлык ссылкой"
    assert 'cp "$src"' in mate_script, "копия убрана из сценария целиком"
