"""The privilege gate that runs before the GUI opens.

Only the decision logic and the command construction are covered here: whether
a given password is accepted is PAM's job, and testing it would mean creating
a real user with a known password on the machine running the suite.
"""
from portable_os_admin.gui import elevate


def test_no_prompt_when_already_privileged(monkeypatch):
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 0)
    assert elevate.needs_elevation() is False


def test_no_second_prompt_in_the_relaunched_copy(monkeypatch):
    """The privileged copy is started by this same code path, so without the
    marker it would ask for the password again, forever."""
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 1000)
    monkeypatch.setenv(elevate.ELEVATED_MARKER, "1")
    assert elevate.needs_elevation() is False


def test_prompts_for_an_unprivileged_user(monkeypatch):
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 1000)
    monkeypatch.delenv(elevate.ELEVATED_MARKER, raising=False)
    monkeypatch.delenv(elevate.SKIP_MARKER, raising=False)
    monkeypatch.setattr(elevate.shutil, "which", lambda name: "/usr/bin/sudo")
    assert elevate.needs_elevation() is True


def test_relaunch_forwards_the_display_or_the_privileged_copy_has_no_screen():
    """sudo strips the environment by default. Losing DISPLAY/XAUTHORITY means
    the privileged process dies with a Tk connection error that says nothing
    about what actually went wrong."""
    argv = elevate.build_privileged_argv(
        {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_ab", "PATH": "/usr/bin"}
    )
    assert argv[0] == "sudo"
    assert "DISPLAY=:1" in argv
    assert "XAUTHORITY=/run/user/1000/xauth_ab" in argv
    assert f"{elevate.ELEVATED_MARKER}=1" in argv
    assert argv[-2:] == ["-m", "portable_os_admin.gui.app"]


def test_relaunch_reads_the_password_from_stdin():
    """-S is what makes the password reachable at all. Without it sudo opens
    the terminal directly, and a GUI launched from a desktop menu has none --
    the launch then fails with "пароль не предоставлен"."""
    argv = elevate.build_privileged_argv({})
    assert "-S" in argv
