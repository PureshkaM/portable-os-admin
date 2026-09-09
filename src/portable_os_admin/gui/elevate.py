"""Obtaining administrator rights before the application opens.

Access to this program is access to every portable medium the organisation
issued: it holds the admin container, hands out recovery codes, and erases
disks. It also cannot do its main job without root -- partitioning, cryptsetup
and mkfs are not available to an ordinary user.

Authentication is delegated entirely to **polkit** (`pkexec`), the mechanism
the desktop already provides. That is deliberate and was learned the hard way:
an earlier version of this module drew its own password dialog and drove
`sudo` behind it. On a normal Debian desktop -- root password set, no user in
the `sudo` group -- that dialog rejected every password, because no password
could have worked, and reported "пароль не подошёл" instead of the truth.
polkit knows the machine's own policy on who counts as an administrator, shows
the desktop's native dialog, and reports the real reason. Nothing here handles
a password.

polkit itself is desktop-agnostic, but the piece that actually shows the
password prompt -- the *authentication agent* -- is not: KDE, GNOME, MATE,
XFCE and LXQt each ship their own, and a session that runs none of them gets
no dialog at all. `pkexec` then falls back to a text-mode agent, which needs a
terminal; started from a desktop menu there is none, so the program would
appear to do nothing.

So the agent is detected before asking, and when it is missing the message
names the package for *this* desktop instead of a generic complaint. The
program can also be started directly as root, which is the honest fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

#: Set in the privileged copy so it does not try to elevate a second time.
ELEVATED_MARKER = "POA_ELEVATED"
#: Escape hatch for automated runs and for development where polkit is not
#: usable. Deliberately not surfaced in the UI.
SKIP_MARKER = "POA_NO_ELEVATE"

#: Installed by the .deb/.rpm. Its presence is what lets polkit show a dialog
#: describing *this* application rather than "запустить /usr/bin/env" -- the
#: shipped policy file points at exactly this path.
PACKAGED_LAUNCHER = "/usr/lib/portable-os-admin/launch-root"

#: pkexec's own exit codes (see pkexec(1)).
DISMISSED = 126
NOT_AUTHORISED = 127

#: Authentication agents by desktop: what the process is called, and which
#: package provides it on each family. Used only to make the "no agent" message
#: specific -- nothing here is required for the normal path, and a desktop
#: missing from this table simply gets the generic advice.
POLKIT_AGENTS = {
    "kde": ("polkit-kde-authentication-agent-1", "polkit-kde-agent-1", "polkit-kde"),
    "plasma": ("polkit-kde-authentication-agent-1", "polkit-kde-agent-1", "polkit-kde"),
    "gnome": ("polkit-gnome-authentication-agent-1", "policykit-1-gnome", "polkit-gnome"),
    "cinnamon": ("polkit-gnome-authentication-agent-1", "policykit-1-gnome", "polkit-gnome"),
    "mate": ("polkit-mate-authentication-agent-1", "mate-polkit", "mate-polkit"),
    "xfce": ("xfce-polkit", "xfce-polkit", "xfce-polkit"),
    "lxqt": ("lxqt-policykit-agent", "lxqt-policykit", "lxqt-policykit"),
}


def current_desktop() -> str:
    """Lowercased desktop name, or "" when there is nothing to go on.

    XDG_CURRENT_DESKTOP may hold several colon-separated names ("ubuntu:GNOME"),
    so every part is considered rather than just the first.
    """
    for variable in ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION"):
        value = os.environ.get(variable, "")
        for part in value.replace(";", ":").split(":"):
            name = part.strip().lower()
            if name in POLKIT_AGENTS:
                return name
            if name:
                for known in POLKIT_AGENTS:
                    if known in name:
                        return known
    return ""


def _process_names(proc_dir: str, pid: str) -> "tuple[str, ...]":
    """Как процесс называется -- по обоим источникам, какие даёт ядро.

    `comm` обрезан до пятнадцати знаков (`TASK_COMM_LEN` в ядре), и агенты
    именно на этом и теряются: `polkit-kde-authentication-agent-1` виден там
    как `polkit-kde-auth`, а `polkit-gnome-…` -- как `polkit-gnome-au`. Ни в
    одном из них нет слова, по которому агента искали.

    Полное имя лежит в `cmdline`, поэтому берутся оба: `cmdline` отвечает на
    вопрос «что это за программа», `comm` остаётся запасным -- у ядерных
    потоков `cmdline` пуст.
    """
    names = []
    try:
        with open(os.path.join(proc_dir, pid, "cmdline"), "rb") as handle:
            raw = handle.read()
    except OSError:
        raw = b""
    if raw:
        first = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        if first:
            names.append(os.path.basename(first).lower())
    try:
        with open(os.path.join(proc_dir, pid, "comm"), encoding="utf-8", errors="replace") as handle:
            names.append(handle.read().strip().lower())
    except OSError:
        pass
    return tuple(name for name in names if name)


def _is_authentication_agent(name: str) -> bool:
    """Похоже ли имя процесса на агента аутентификации polkit.

    `auth` наравне с `agent`, потому что обрезанное `comm` до второго слова
    не доживает.
    """
    if "polkit" in name and ("agent" in name or "auth" in name):
        return True
    return name.startswith(("xfce-polkit", "lxqt-policykit"))


def _is_agent_helper(name: str) -> bool:
    """`polkit-agent-helper-1` -- не агент, хотя называется почти так же.

    Это вспомогательный процесс, который сам агент запускает на время
    проверки пароля. Принять его за агента значило бы решить, что диалог
    будет показан, ровно в тот момент, когда его уже показывает кто-то
    другой. Сравнение по обрезанному началу: в `comm` от него остаётся
    `polkit-agent-he`.
    """
    return name.startswith("polkit-agent-he")


def authentication_agent_running(proc_dir: str = "/proc") -> bool:
    """Whether some polkit authentication agent is alive in this session.

    Found by name in /proc rather than over D-Bus: this must work with no
    optional Python packages installed, on both families, and a false negative
    only costs an extra hint in an error message.

    Ложное «нет» стоило дороже, чем здесь было написано. Проверка требовала
    в имени процесса и `polkit`, и `agent`, а читала только `comm` -- то есть
    обрезанное ядром до пятнадцати знаков. На живой сессии Plasma
    (09.09.2026) агент работал, а программа объявляла, что его нет, и
    отказывалась запускаться из меню вовсе. Тест этого не поймал, потому что
    писал в поддельный `comm` полное имя -- то, чего ядро туда никогда не
    кладёт.

    `proc_dir` exists so a test can point at a directory of its own instead of
    monkeypatching os.listdir and open, which would affect every other thing
    running in the same process.
    """
    try:
        pids = [name for name in os.listdir(proc_dir) if name.isdigit()]
    except OSError:
        return False
    for pid in pids:
        names = _process_names(proc_dir, pid)
        # Решение принимается по процессу целиком, а не по каждому имени в
        # отдельности: у вспомогательного процесса обрезанное имя выглядит
        # как агентское, и отвергнуть его можно только зная полное.
        if any(_is_agent_helper(name) for name in names):
            continue
        if any(_is_authentication_agent(name) for name in names):
            return True
    return False


def missing_agent_message() -> str:
    """Explains what to install, for the desktop actually in use."""
    desktop = current_desktop()
    if desktop:
        process, deb, rpm = POLKIT_AGENTS[desktop]
        specific = (
            f"Рабочий стол определён как {desktop}, для него нужен агент {process}: "
            f"пакет {deb} (Debian) или {rpm} (RedOS/RHEL)."
        )
    else:
        specific = (
            "Определить рабочий стол не удалось. Нужный агент зависит от среды: "
            + ", ".join(sorted({deb for _, deb, _ in POLKIT_AGENTS.values()}))
            + " — установите тот, что соответствует вашей."
        )
    return (
        "В текущем сеансе не запущена служба, которая показывает системный запрос "
        "пароля (агент аутентификации polkit), поэтому окно не появится.\n\n"
        + specific
        + "\n\nЛибо запустите программу из терминала — тогда пароль будет запрошен "
        "текстом, — либо напрямую от root."
    )


def is_root() -> bool:
    return getattr(os, "geteuid", lambda: 0)() == 0


def needs_elevation() -> bool:
    if os.environ.get(SKIP_MARKER) == "1":
        return False
    if os.environ.get(ELEVATED_MARKER) == "1":
        return False
    return not is_root()


def invoking_user_home() -> Path:
    """Home directory of the person who started the program, not of root.

    After the relaunch `Path.home()` is /root, so a file dialog would open in a
    directory where the administrator keeps nothing.
    """
    for variable in ("PKEXEC_UID", "SUDO_UID"):
        raw = os.environ.get(variable)
        if raw and raw.isdigit():
            try:
                import pwd

                return Path(pwd.getpwuid(int(raw)).pw_dir)
            except (KeyError, ImportError):
                pass
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd

            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, ImportError):
            pass
    return Path.home()


def build_privileged_argv(environment: dict[str, str] | None = None) -> list[str]:
    """The pkexec command that relaunches this program with administrator rights.

    polkit clears the environment of the program it starts, so the session's
    display has to be handed over explicitly or the privileged copy has no
    screen to draw on. When the packaged launcher is present it takes them as
    arguments (which lets the shipped policy file describe this application by
    name); otherwise the standard `pkexec env VAR=... command` form is used,
    which works from a source checkout too.
    """
    environment = dict(os.environ if environment is None else environment)
    display = environment.get("DISPLAY", "")
    xauthority = environment.get("XAUTHORITY", "")
    wayland = environment.get("WAYLAND_DISPLAY", "")
    runtime_dir = environment.get("XDG_RUNTIME_DIR", "")

    if os.path.exists(PACKAGED_LAUNCHER):
        return ["pkexec", PACKAGED_LAUNCHER, display, xauthority, wayland, runtime_dir]

    environment[ELEVATED_MARKER] = "1"
    passthrough = [
        f"{name}={environment[name]}"
        for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", ELEVATED_MARKER)
        if name in environment
    ]
    return ["pkexec", "env", *passthrough, sys.executable, "-m", "portable_os_admin.gui.app"]


NO_PKEXEC = (
    "Не найдена программа pkexec, через которую запрашиваются права администратора.\n"
    "Установите пакет pkexec (Debian) или polkit (RedOS/RHEL), либо запустите "
    "приложение напрямую от root."
)

NOT_AUTHORISED_MESSAGE = (
    "Права администратора не получены.\n\n"
    "Если пароль вводился и не подошёл — это пароль администратора машины "
    "(root либо учётной записи из группы sudo/wheel), а не пароль контейнера.\n\n"
    "Если окно с запросом пароля вообще не появилось, значит в текущем сеансе нет "
    "службы аутентификации polkit. Так бывает при запуске по ssh или из окружения без "
    "рабочего стола — запустите приложение из графического сеанса либо напрямую от root."
)


def elevate_or_exit() -> None:
    """Hands over to a privileged copy of the program, or exits.

    Returns normally only when the program already has the rights it needs.
    """
    if not needs_elevation():
        return

    if shutil.which("pkexec") is None:
        _report(NO_PKEXEC)
        sys.exit(1)

    # Without an agent and without a terminal there is nothing that could ask
    # for a password: pkexec's text-mode fallback needs a tty, and a program
    # started from a desktop menu has none. Saying so beats a window that
    # silently never appears.
    if not authentication_agent_running() and not sys.stdin.isatty():
        _report(missing_agent_message())
        sys.exit(1)

    try:
        returncode = subprocess.run(build_privileged_argv()).returncode
    except OSError as exc:  # pragma: no cover - only on a broken polkit install
        _report(f"Не удалось запросить права администратора: {exc}")
        sys.exit(1)

    if returncode == DISMISSED:
        # The person closed the system dialog. That is an answer, not an error.
        sys.exit(0)
    if returncode == NOT_AUTHORISED:
        _report(NOT_AUTHORISED_MESSAGE)
    sys.exit(returncode)


def _report(message: str) -> None:
    """Shows the message in a dialog, falling back to stderr.

    Only ever used to explain a failure -- the authentication itself belongs to
    the desktop, not to this application.
    """
    print(message, file=sys.stderr)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Portable OS Admin", message)
        root.destroy()
    except Exception:  # noqa: BLE001 - no display is a normal case here
        pass
