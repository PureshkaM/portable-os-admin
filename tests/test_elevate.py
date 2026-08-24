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
    # session variables that are absent are still passed, as empty strings: the
    # launcher reads them positionally, so dropping one would shift the rest
    assert argv == ["pkexec", elevate.PACKAGED_LAUNCHER, ":1", "/tmp/xa", "", ""]


def test_desktop_is_recognised_inside_a_compound_value(monkeypatch):
    """XDG_CURRENT_DESKTOP is not one word: Ubuntu sets "ubuntu:GNOME", and a
    naive read of the first element gets the wrong answer."""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    assert elevate.current_desktop() == "gnome"


def test_every_mainstream_desktop_is_recognised(monkeypatch):
    for value, expected in (
        ("KDE", "kde"),
        ("MATE", "mate"),
        ("XFCE", "xfce"),
        ("LXQt", "lxqt"),
        ("X-Cinnamon", "cinnamon"),
    ):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", value)
        assert elevate.current_desktop() == expected, value


def test_an_unknown_desktop_is_not_guessed(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "SomeWindowManager")
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    assert elevate.current_desktop() == ""


def test_missing_agent_message_names_the_package_for_this_desktop(monkeypatch):
    """The whole point: a MATE user must not be told to install the KDE agent."""
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "MATE")
    message = elevate.missing_agent_message()
    assert "mate-polkit" in message
    assert "polkit-kde-agent-1" not in message


def test_missing_agent_message_still_helps_when_the_desktop_is_unknown(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "SomeWindowManager")
    monkeypatch.delenv("XDG_SESSION_DESKTOP", raising=False)
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    message = elevate.missing_agent_message()
    assert "mate-polkit" in message and "policykit-1-gnome" in message


def test_a_running_agent_is_detected_by_name(tmp_path):
    (tmp_path / "1234").mkdir()
    (tmp_path / "1234" / "comm").write_text("polkit-mate-authentication-agent-1\n", encoding="utf-8")
    (tmp_path / "notapid").mkdir()

    assert elevate.authentication_agent_running(str(tmp_path)) is True


def test_no_agent_is_detected_when_none_is_running(tmp_path):
    (tmp_path / "1234").mkdir()
    (tmp_path / "1234" / "comm").write_text("bash\n", encoding="utf-8")

    assert elevate.authentication_agent_running(str(tmp_path)) is False


def test_desktop_specific_agent_names_are_detected(tmp_path):
    """xfce-polkit and lxqt-policykit-agent do not contain both "polkit" and
    "agent" in the way the generic rule expects."""
    for index, name in enumerate(("xfce-polkit", "lxqt-policykit-agent")):
        directory = tmp_path / str(1000 + index)
        directory.mkdir()
        (directory / "comm").write_text(name + "\n", encoding="utf-8")
        assert elevate.authentication_agent_running(str(tmp_path)) is True, name
        (directory / "comm").unlink()
        directory.rmdir()


def test_the_session_display_is_handed_to_the_packaged_launcher(monkeypatch):
    """polkit clears the environment, so a Wayland session needs its runtime
    directory passed explicitly -- Tk reaches the screen through XWayland, but
    the privileged copy still has nowhere to put its session files without it."""
    monkeypatch.setattr(elevate.os.path, "exists", lambda path: path == elevate.PACKAGED_LAUNCHER)
    argv = elevate.build_privileged_argv(
        {
            "DISPLAY": ":1",
            "XAUTHORITY": "/run/user/1000/xauth",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        }
    )
    assert argv[:2] == ["pkexec", elevate.PACKAGED_LAUNCHER]
    assert argv[2:] == [":1", "/run/user/1000/xauth", "wayland-0", "/run/user/1000"]
