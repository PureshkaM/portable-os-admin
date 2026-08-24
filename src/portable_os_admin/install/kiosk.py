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


def is_enabled(config: InstallConfig) -> bool:
    return bool(getattr(config, "kiosk", None) and config.kiosk.enabled)


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
                ("enable-program-list", "false"),
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
    """
    return (
        "# generated by portable-os-admin\n"
        'Section "ServerFlags"\n'
        '    Option "DontVTSwitch" "true"\n'
        '    Option "DontZap" "true"\n'
        '    Option "AllowClosedownGrabs" "false"\n'
        "EndSection\n"
    )


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
    same-named entry with Hidden=true wins while the packaged file stays intact
    and a later `kiosk off` is a matter of removing one directory.

    The allowed list is baked into the script instead of passed as arguments --
    a desktop-entry name is administrator input, and text that never crosses a
    shell command line cannot be quoted wrong.
    """
    kiosk = config.kiosk
    allowed = " ".join(sorted(set(kiosk.allowed_apps)))
    firejail_opts = kiosk.firejail_options.strip()
    use_firejail = "1" if kiosk.firejail else ""
    return f"""#!/bin/sh
# generated by portable-os-admin
# Применяет список разрешённых приложений режима киоска.
set -eu

ALLOWED="{allowed}"
USE_FIREJAIL="{use_firejail}"
FIREJAIL_OPTS="{firejail_opts}"
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
    # Hidden=true — штатный для freedesktop способ сказать «этой записи нет».
    # NoDisplay=true добавлен для сред, которые читают только его.
    {{
        printf '[Desktop Entry]\\n'
        printf 'Type=Application\\n'
        printf 'Name=%s\\n' "$name"
        printf 'NoDisplay=true\\n'
        printf 'Hidden=true\\n'
    }} > "$OVERRIDE_DIR/$name.desktop"
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

# Скомпилировать системную базу dconf из файлов, записанных установщиком.
# Без этого шага и настройки, и блокировки остаются просто текстом.
if command -v dconf >/dev/null 2>&1; then
    dconf update
else
    echo "poa-kiosk: dconf не установлен, настройки рабочей среды не применены" >&2
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

    files: dict[str, tuple[str, int | None]] = {
        DCONF_PROFILE_PATH: (dconf_profile(), None),
        DCONF_KEYFILE_PATH: (dconf_keyfile(config), None),
        DCONF_LOCKS_PATH: (dconf_locks(config), None),
        XORG_CONF_PATH: (xorg_conf(), None),
        POLKIT_RULES_PATH: (polkit_rules(config), None),
        APPLY_SCRIPT_PATH: (apply_script(config), 0o755),
        LAUNCHER_PATH: (launcher_script(), 0o755),
    }
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
    display_manager = backend.display_manager_service
    if display_manager:
        commands.append(backend.systemctl_enable(mount_root, display_manager))

    # The text consoles. DontVTSwitch already keeps X from handing them over,
    # and masking the units means there is nothing listening even if it did.
    for unit in ("getty@.service", "serial-getty@.service"):
        commands.append(
            backend.chroot(mount_root, "env", "SYSTEMD_OFFLINE=1", "systemctl", "mask", unit)
        )

    commands.append(backend.chroot(mount_root, "/bin/sh", f"/{APPLY_SCRIPT_PATH}"))
    return commands
