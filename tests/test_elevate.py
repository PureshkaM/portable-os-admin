"""The privilege gate that runs before the GUI opens.

Authentication itself belongs to polkit, so there is nothing here about
passwords -- only the decision to elevate and the command handed to pkexec.
"""
from portable_os_admin.gui import elevate


def test_no_prompt_when_already_privileged(monkeypatch):
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 0)
    assert elevate.needs_elevation() is False


def test_no_second_prompt_in_the_relaunched_copy(monkeypatch):
    """The privileged copy is started by this same code path, so without the
    marker it would ask polkit again, forever."""
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 1000)
    monkeypatch.setenv(elevate.ELEVATED_MARKER, "1")
    assert elevate.needs_elevation() is False


def test_prompts_for_an_unprivileged_user(monkeypatch):
    monkeypatch.setattr(elevate.os, "geteuid", lambda: 1000)
    monkeypatch.delenv(elevate.ELEVATED_MARKER, raising=False)
    monkeypatch.delenv(elevate.SKIP_MARKER, raising=False)
    assert elevate.needs_elevation() is True


def test_authentication_is_delegated_to_polkit(monkeypatch):
    """No password ever passes through this application. An earlier version
    drew its own dialog and drove sudo, which on a machine with an empty sudo
    group rejected every password and blamed the password."""
    monkeypatch.setattr(elevate.os.path, "exists", lambda path: False)
    argv = elevate.build_privileged_argv({"DISPLAY": ":1"})
    assert argv[0] == "pkexec"
    assert not hasattr(elevate, "PasswordPrompt")


def test_relaunch_forwards_the_display_or_the_privileged_copy_has_no_screen(monkeypatch):
    """polkit clears the environment of what it starts. Losing
    DISPLAY/XAUTHORITY means the privileged process dies with a Tk connection
    error that explains nothing."""
    monkeypatch.setattr(elevate.os.path, "exists", lambda path: False)
    argv = elevate.build_privileged_argv(
        {"DISPLAY": ":1", "XAUTHORITY": "/run/user/1000/xauth_ab", "PATH": "/usr/bin"}
    )
    assert "DISPLAY=:1" in argv
    assert "XAUTHORITY=/run/user/1000/xauth_ab" in argv
    assert f"{elevate.ELEVATED_MARKER}=1" in argv
    assert argv[-2:] == ["-m", "portable_os_admin.gui.app"]


def test_packaged_launcher_is_preferred_so_the_dialog_names_the_application(monkeypatch):
    """With the packaged launcher present, pkexec is pointed at the path the
    shipped polkit action describes -- otherwise the desktop asks permission to
    "run /usr/bin/env as the super user", which tells the user nothing."""
    monkeypatch.setattr(
        elevate.os.path, "exists", lambda path: path == elevate.PACKAGED_LAUNCHER
    )
    argv = elevate.build_privileged_argv({"DISPLAY": ":1", "XAUTHORITY": "/tmp/xa"})
    assert argv == ["pkexec", elevate.PACKAGED_LAUNCHER, ":1", "/tmp/xa"]
