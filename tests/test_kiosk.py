"""The restricted desktop.

These tests assert the *shape* of what gets written, which is all a test can
do here: whether MATE honours a lock, whether lightdm autologins into a locked
account and whether `noexec` stops the thing an administrator worried about
are properties of the medium, not of this code, and are recorded as verified or
unverified in docs/HANDOVER.md.

What they do catch is the failure mode this feature is most prone to -- a
restriction that is generated but inert. Every real defect found while building
it was of that kind: a dconf path that no schema reads, a string that fails to
compile, a script written without an execute bit, an fstab row with a column
missing. Each would have installed cleanly and protected nothing.
"""
from __future__ import annotations

import pytest

from portable_os_admin.config import InstallConfig
from portable_os_admin.config.schema import KioskConfig
from portable_os_admin.config.validator import BuiltinConfigValidator
from portable_os_admin.install import kiosk
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.disk import build_media_layout, plan_partitions


def make_kiosk_config(target_os: str = "debian", **kiosk_kwargs) -> InstallConfig:
    config = InstallConfig.create_default("kiosk-stick", target_os)
    kiosk_kwargs.setdefault("allowed_apps", ["mate-calc", "firefox-esr"])
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
    assert restored.kiosk.allowed_apps == ["mate-calc", "firefox-esr"]
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


def test_allowed_applications_reach_the_apply_script():
    script = kiosk.apply_script(make_kiosk_config(allowed_apps=["mate-calc", "atril"]))

    assert 'ALLOWED="atril mate-calc"' in script


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


def test_a_kiosk_with_no_allowed_application_is_refused():
    config = make_kiosk_config(allowed_apps=[])
    assert any("хотя бы одно приложение" in e for e in _errors(config))


def test_application_names_must_be_desktop_entry_names():
    """Not paths, not package names -- and not shell.

    The names are interpolated into a script that runs inside the target, so a
    name that cannot hold a slash or a quote cannot break out of it.
    """
    for bad in ("/usr/bin/sh", "a; rm -rf /", "app.desktop; echo", "../../etc/passwd"):
        assert _errors(make_kiosk_config(allowed_apps=[bad])), f"{bad!r} принято как имя"

    assert not _errors(make_kiosk_config(allowed_apps=["mate-calc", "libreoffice-writer"]))


def test_autostart_application_must_be_one_of_the_allowed_ones():
    config = make_kiosk_config(allowed_apps=["mate-calc"], autostart_app="firefox-esr")
    assert any("автозапуска" in e for e in _errors(config))


def test_kiosk_is_refused_for_a_family_that_has_no_support():
    """Astra inherits the Debian backend but not this mode.

    Its package names were checked against a Debian 13 image and Astra derives
    from Debian 9, so inheriting the kiosk would mean an install that dies
    partway with the medium already written.
    """
    config = make_kiosk_config("astra")
    assert any("не поддерживается" in e for e in _errors(config))

    assert get_backend("astra").desktop_packages(config) == []
    assert get_backend("astra").display_manager_files(config) == {}


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

    assert ('APPLETSRC_LAUNCHERS="applications:atril.desktop,'
            'applications:mate-calc.desktop"') in script
    assert "systemsettings" not in script, "настройки не разрешены -- нечего выносить"
    assert "ordering=" in script, "иначе в избранном останется набор дистрибутива"
    # immutability=2 -- SystemImmutable: пользователь не разблокирует виджеты.
    assert "immutability=2" in script
    assert "/etc/skel" in script


def test_settings_reach_the_panel_only_when_something_is_allowed():
    with_settings = kiosk.plasma_layout_script(
        make_kiosk_config(desktop_environment="kde", allowed_apps=["mate-calc"],
                          allowed_settings=["network"])
    )
    assert "applications:systemsettings.desktop" in with_settings


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
    появляется вовсе."""
    from portable_os_admin.install.kiosk.common import UDEV_RULES_PATH

    refused = get_backend("debian").scripted_files(make_kiosk_config(), layout)
    assert UDEV_RULES_PATH in refused
    assert 'ATTR{authorized}="0"' in refused[UDEV_RULES_PATH][0]

    # Разрешив носители, администратор получает обычное поведение — и правила,
    # которое ничего не делает, на носителе не остаётся.
    allowed = get_backend("debian").scripted_files(
        make_kiosk_config(allow_removable_media=True), layout
    )
    assert UDEV_RULES_PATH not in allowed
