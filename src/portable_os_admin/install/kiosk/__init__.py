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

from ...config.schema import InstallConfig
from .common import (
    HARDENING_KERNEL_PARAMETERS,
    UNWANTED_SERVICES,
    APPLY_SCRIPT_PATH,
    AUTOSTART_PATH,
    DESKTOP_MATE,
    EXCHANGE_DIR,
    HARDENED_OPTS,
    HIDDEN_VOLUMES_RULES_PATH,
    LAUNCHER_PATH,
    LOGIND_CONF_PATH,
    OVERRIDE_DIR,
    POLKIT_RULES_PATH,
    USB_LOCKDOWN_UNIT_PATH,
    SESSION_NAME,
    XORG_CONF_PATH,
    autostart_entry,
    desktop_of,
    hardening_fstab_rows,
    hidden_volumes_udev_rules,
    is_enabled,
    launcher_script,
    logind_conf,
    polkit_rules,
    session_entry,
    udev_storage_rules,
    usb_lockdown_unit,
    xorg_conf,
)
from .environments import base as environments
from .environments.mate import (
    DCONF_KEYFILE_PATH,
    DCONF_LOCKS_PATH,
    DCONF_PROFILE_PATH,
    dconf_keyfile,
    dconf_locks,
    dconf_profile,
)
from .script import apply_script

#: Что этот пакет отдаёт наружу.
#:
#: Список нужен не ради стиля: имена выше импортируются, чтобы вызывающие брали
#: их отсюда, а не из внутренних модулей, — и статический анализатор без `__all__`
#: считает каждое такое имя неиспользованным импортом. Тридцать ложных
#: замечаний в выводе прячут настоящее, поэтому граница объявлена явно.
__all__ = [
    "APPLY_SCRIPT_PATH", "AUTOSTART_PATH", "DESKTOP_MATE",
    "EXCHANGE_DIR", "HARDENED_OPTS", "HARDENING_KERNEL_PARAMETERS",
    "HIDDEN_VOLUMES_RULES_PATH", "LAUNCHER_PATH", "LOGIND_CONF_PATH",
    "OVERRIDE_DIR", "POLKIT_RULES_PATH", "SESSION_NAME", "UNWANTED_SERVICES", "USB_LOCKDOWN_UNIT_PATH",
    "XORG_CONF_PATH", "autostart_entry", "desktop_of", "hardening_fstab_rows",
    "hidden_volumes_udev_rules", "is_enabled", "launcher_script",
    "logind_conf", "polkit_rules", "session_entry", "udev_storage_rules",
    "usb_lockdown_unit", "xorg_conf",
    "environments",
    "DCONF_KEYFILE_PATH", "DCONF_LOCKS_PATH", "DCONF_PROFILE_PATH",
    "dconf_keyfile", "dconf_locks", "dconf_profile",
    "apply_script",
    "config_files", "chroot_commands",
]


# ----------------------------------------------------------------------
# What the installer asks for
# ----------------------------------------------------------------------
def config_files(config: InstallConfig, image=None) -> dict[str, tuple[str, int | None]]:
    """Relative path -> (content, mode) for everything written into the target.

    Deliberately excludes the display-manager configuration: which one is
    installed is a property of the distribution family, so the backend supplies
    that file while everything here stays shared.

    Разделено надвое по тому, что чем ограничивается: сначала общее -- оно
    запирает систему и держится независимо от того, какая оболочка рисует
    экран, -- затем то, что умеет только сама среда.
    """
    if not is_enabled(config):
        return {}

    files: dict[str, tuple[str, int | None]] = {
        XORG_CONF_PATH: (xorg_conf(), None),
        LOGIND_CONF_PATH: (logind_conf(), None),
        POLKIT_RULES_PATH: (polkit_rules(config), None),
        APPLY_SCRIPT_PATH: (apply_script(config, image), 0o755),
        LAUNCHER_PATH: (launcher_script(), 0o755),
        # Служебные разделы носителя -- мимо глаз рабочего стола. В /etc, а не
        # в /run, потому что правило ничего не деавторизует: оно помечает том
        # переменной, и coldplug, на котором обжигался запрет USB, ему
        # безразличен.
        HIDDEN_VOLUMES_RULES_PATH: (hidden_volumes_udev_rules(), None),
    }
    session_path, session_content = session_entry(config)
    files[session_path] = (session_content, 0o644)

    # Правило кладёт служба, и только после того как система поднялась. В
    # /etc ему не место: coldplug при переходе из initramfs шлёт ACTION=add
    # и для загрузочного носителя, и тот отключает сам себя -- прогон
    # 28.08.2026 закончился отказом ввода-вывода и корнем в read-only.
    unit = usb_lockdown_unit(config)
    if unit:
        files[USB_LOCKDOWN_UNIT_PATH] = (unit, 0o644)

    files.update(environments.for_config(config, image).files(config))

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

    # Запрет на чужие накопители включается службой, а не файлом в /etc:
    # положенное туда правило срабатывает на coldplug при переходе из
    # initramfs и отключает загрузочный носитель. Без `enable` служба просто
    # лежала бы в системе -- ровно та ошибка, которую в этом проекте уже
    # ловили с модулем dracut.
    if not config.kiosk.allow_removable_media:
        commands.append(backend.systemctl_enable(mount_root, "poa-usb-lockdown.service"))

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

    # Службы, пришедшие с дистрибутивом и киоску не нужные. Отказ здесь не
    # должен ронять сборку: набор служб зависит от образа, и отсутствие
    # какой-то из них -- норма, а не ошибка.
    for unit in UNWANTED_SERVICES:
        masking = backend.chroot(
            mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "mask", unit
        )
        commands.append(["sh", "-c", f"{shlex.join(masking)} || true"])

    commands.append(backend.chroot(mount_root, "/bin/sh", f"/{APPLY_SCRIPT_PATH}"))
    return commands
