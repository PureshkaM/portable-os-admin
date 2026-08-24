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
