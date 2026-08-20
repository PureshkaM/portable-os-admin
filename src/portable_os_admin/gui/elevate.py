"""Asking for the administrator password before the application opens.

Access to this program is access to every portable medium the organisation
issued: it holds the admin container, hands out recovery codes, and erases
disks. Requiring the privileged-user password up front makes that explicit
instead of leaving the program open on an unattended desktop, and it also
solves a practical problem -- installation needs root, and without this the
user found out only after filling in the whole wizard.

The password is not verified by this application and never stored. It is
handed to `sudo` on standard input, which does the checking through PAM, and
a privileged copy of the same program is then started in place of this one.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

#: Set to "1" in the re-executed child so it does not ask a second time.
ELEVATED_MARKER = "POA_ELEVATED"
#: Escape hatch for automated runs and for development on a machine where
#: sudo is not usable. Deliberately not documented in the UI.
SKIP_MARKER = "POA_NO_ELEVATE"


def is_root() -> bool:
    return getattr(os, "geteuid", lambda: 0)() == 0


def needs_elevation() -> bool:
    if os.environ.get(SKIP_MARKER) == "1":
        return False
    if os.environ.get(ELEVATED_MARKER) == "1":
        return False
    if is_root():
        return False
    return shutil.which("sudo") is not None


class PasswordPrompt(tk.Tk):
    """A small window that collects the password and reports the outcome.

    Deliberately its own Tk root, shown before the main application exists:
    if authentication fails there is nothing to tear down.
    """

    def __init__(self) -> None:
        super().__init__()
        self.title("Portable OS Admin")
        self.resizable(False, False)
        self.password: str | None = None

        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text="Требуются права администратора", font=("TkDefaultFont", 12, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                "Программа управляет переносными носителями и размечает диски, "
                "поэтому запускается только с правами суперпользователя.\n"
                f"Введите пароль для sudo (пользователь {_current_user()})."
            ),
            wraplength=420,
            justify="left",
            foreground="#555555",
        ).pack(anchor="w", pady=(6, 12))

        self.password_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=self.password_var, show="*", width=36)
        entry.pack(anchor="w")
        entry.focus_set()
        entry.bind("<Return>", lambda _event: self._submit())

        self.error_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.error_var, foreground="#c0362c", wraplength=420,
                  justify="left").pack(anchor="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(14, 0))
        ttk.Button(buttons, text="Отмена", command=self._cancel).pack(side="left")
        ttk.Button(buttons, text="Продолжить", command=self._submit).pack(side="left", padx=(8, 0))

        self.protocol("WM_DELETE_WINDOW", self._cancel)

    def _submit(self) -> None:
        password = self.password_var.get()
        if not password:
            self.error_var.set("Введите пароль.")
            return
        if not _sudo_accepts(password):
            self.error_var.set("Пароль не подошёл. Попробуйте ещё раз.")
            self.password_var.set("")
            return
        self.password = password
        self.destroy()

    def _cancel(self) -> None:
        self.password = None
        self.destroy()


def invoking_user_home() -> Path:
    """Home directory of the person who started the program, not of root.

    The application relaunches itself under sudo, after which `Path.home()`
    is /root -- so a file dialog would open somewhere the administrator does
    not keep anything, instead of where they actually store the container.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            import pwd

            return Path(pwd.getpwnam(sudo_user).pw_dir)
        except (KeyError, ImportError):
            pass
    return Path.home()


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001 - a missing username is cosmetic here
        return "?"


def _sudo_accepts(password: str) -> bool:
    """Validates the password without running anything else.

    `sudo -v` only refreshes the credential timestamp, so a wrong password
    costs nothing and a right one leaves the session ready for the re-exec
    below. `-k` first so a timestamp cached by some earlier sudo cannot make
    an empty or wrong password look correct.
    """
    try:
        subprocess.run(["sudo", "-k"], capture_output=True, timeout=10, check=False)
        proc = subprocess.run(
            ["sudo", "-S", "-p", "", "-v"],
            input=(password + "\n").encode("utf-8"),
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def elevate_or_exit() -> None:
    """Prompts, then hands over to a privileged copy of the program.

    Returns normally only when the program is already running with the rights
    it needs. On cancellation the program exits: continuing unprivileged would
    give a window that cannot perform its main function.
    """
    if not needs_elevation():
        return

    prompt = PasswordPrompt()
    prompt.mainloop()
    if prompt.password is None:
        sys.exit(0)

    sys.exit(_run_privileged(prompt.password))


def build_privileged_argv(environment: dict[str, str] | None = None) -> list[str]:
    """The command that re-launches this program with administrator rights.

    DISPLAY/XAUTHORITY and friends are forwarded through `env` explicitly:
    sudo strips most of the environment by default, and without them the
    privileged copy has no screen to draw on and dies with a Tk error the user
    would have no way to interpret.
    """
    environment = dict(os.environ if environment is None else environment)
    environment[ELEVATED_MARKER] = "1"
    passthrough = [
        f"{name}={environment[name]}"
        for name in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", ELEVATED_MARKER)
        if name in environment
    ]
    return ["sudo", "-S", "-p", "", "env", *passthrough, sys.executable, "-m", "portable_os_admin.gui.app"]


def _run_privileged(password: str) -> int:
    """Starts the privileged copy, handing sudo the password on its stdin.

    Deliberately not `execvp`: replacing this process leaves no way to feed
    sudo the password, so it would fall back on the credential cached by the
    check above -- and that cache does not always exist. `timestamp_timeout=0`
    is a normal hardening setting, and with it the re-launch failed outright
    with "пароль не предоставлен" (observed, not hypothetical). Passing the
    password explicitly makes the launch independent of sudo's caching policy.
    """
    try:
        proc = subprocess.Popen(build_privileged_argv(), stdin=subprocess.PIPE)
    except OSError as exc:  # pragma: no cover - only on a broken sudo install
        sys.exit(f"не удалось получить права администратора: {exc}")
    try:
        assert proc.stdin is not None
        proc.stdin.write((password + "\n").encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()
    except OSError:
        # sudo may already have authenticated from its cache and closed stdin.
        pass
    return proc.wait()
