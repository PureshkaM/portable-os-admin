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

Requires a polkit authentication agent in the session; every mainstream desktop
runs one. Without it (a bare X session, a remote shell) `pkexec` falls back to
its text-mode agent, which is why the program can still be started directly as
root instead.
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

    if os.path.exists(PACKAGED_LAUNCHER):
        return ["pkexec", PACKAGED_LAUNCHER, display, xauthority]

    environment[ELEVATED_MARKER] = "1"
    passthrough = [
        f"{name}={environment[name]}"
        for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", ELEVATED_MARKER)
        if name in environment
    ]
    return ["pkexec", "env", *passthrough, sys.executable, "-m", "portable_os_admin.gui.app"]


NO_PKEXEC = (
    "Не найдена программа pkexec, через которую запрашиваются права администратора.\n"
    "Установите пакет pkexec (Debian/Astra) или polkit (RedOS/RHEL), либо запустите "
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
