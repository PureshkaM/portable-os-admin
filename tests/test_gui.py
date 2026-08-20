"""Headless coverage for the Tkinter GUI (gui/), mirroring the flows already
covered for the Textual TUI in test_tui.py / test_tui_new_features.py.

No real display is required to drive Tkinter widgets programmatically, but a
Tcl/Tk runtime must be available and able to open (at least) an offscreen
root window; environments with neither are skipped rather than failed.
"""
from __future__ import annotations

import time
import tkinter as tk

import pytest

from portable_os_admin.config.repo_browser import RepoPackage
from portable_os_admin.gui import screens as _screens_pkg  # noqa: F401 -- import-ability check
from portable_os_admin.gui.app import PortableOSAdminGUI
from portable_os_admin.gui.screens import config_wizard as config_wizard_module
from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame
from portable_os_admin.gui.screens.configs_list import ConfigsListFrame
from portable_os_admin.gui.screens.install_wizard import InstallWizardFrame
from portable_os_admin.gui.screens.startup import StartupFrame


@pytest.fixture
def gui_app():
    try:
        app = PortableOSAdminGUI()
    except tk.TclError as exc:
        pytest.skip(f"no usable Tk display in this environment: {exc}")
    app.withdraw()
    yield app
    app.destroy()


def _pump(app, condition, timeout=5.0, interval=0.05):
    """Drives Tkinter's event loop until `condition()` is true or times out
    -- needed wherever a background thread (run_in_background) is involved,
    since nothing updates until the queue is polled via app.after()."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if condition():
            return
        time.sleep(interval)
    app.update()
    assert condition(), "timed out waiting for background operation to complete"


def _open_container(app, tmp_path):
    startup = app._current_frame
    assert isinstance(startup, StartupFrame)
    startup.path_var.set(str(tmp_path / "admin.container"))
    startup.password_var.set("CorrectHorse123")
    startup._create()
    app.update()
    assert app.session is not None


def test_startup_wrong_password_shows_error(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    app.close_session()
    app.update()

    startup = app._current_frame
    startup.path_var.set(str(tmp_path / "admin.container"))
    startup.password_var.set("totally-wrong")
    startup._open()
    app.update()

    assert "красн" not in startup.message_bar.var.get()  # sanity: no markup leaked into text
    assert startup.message_bar.var.get() != ""
    assert app.session is None


def test_config_wizard_creates_and_validates(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)

    app.show_frame(ConfigsListFrame)
    app._current_frame._create_new()
    app.update()

    wizard = app._current_frame
    assert isinstance(wizard, ConfigWizardFrame)
    wizard.name_var.set("stick1")
    wizard.target_os_var.set("redos")
    wizard._on_target_os_changed()
    assert wizard.release_var.get() == "8.0"

    for _ in range(6):  # basics -> account, disk, remote, tasks, packages -> review
        wizard._go_next()
        app.update()
    assert wizard.step_titles[wizard.step_index] == "Проверка и сохранение"

    wizard._run_validation()
    app.update()
    assert wizard.config.validation_status == "valid", wizard.config.validation_errors

    wizard._go_next()  # finish: save + navigate to configs list
    app.update()

    configs = app._current_frame
    assert isinstance(configs, ConfigsListFrame)
    assert [p.name for p in configs._paths] == ["stick1.json"]


def test_config_wizard_account_step_blocks_invalid_username(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame

    wizard.name_var.set("stick1")
    wizard._go_next()  # -> account step
    app.update()

    wizard.username_var.set("1nvalid name!")
    wizard._go_next()
    app.update()

    assert wizard.step_titles[wizard.step_index] == "Учётная запись пользователя"
    assert "имя пользователя" in wizard.message_bar.var.get()


def test_config_wizard_package_picker_add_and_remove(gui_app, tmp_path, monkeypatch):
    class FakeSource:
        def list_packages(self, mirror, release):
            assert mirror == "https://deb.debian.org/debian"
            assert release == "bookworm"
            return [RepoPackage(name="htop", version="3.2.0", summary="interactive process viewer")]

    monkeypatch.setattr(config_wizard_module, "get_package_source", lambda target_os: FakeSource())

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    wizard.name_var.set("stick1")

    for _ in range(5):  # basics -> account, disk, remote, tasks -> packages
        wizard._go_next()
        app.update()
    assert wizard.step_titles[wizard.step_index] == "Пакеты для предустановки"

    wizard._connect_repo()
    _pump(app, lambda: bool(wizard._catalog))
    assert len(wizard._catalog) == 1

    wizard.catalog_table.selection_set(wizard.catalog_table.get_children()[0])
    wizard._add_package()
    app.update()
    assert wizard.config.packages == ["htop"]

    wizard.selected_list.selection_set(0)
    wizard._remove_package()
    app.update()
    assert wizard.config.packages == []


def _make_validated_debian_config(app):
    config = app.config_manager.create(app.configs_dir, "stick1", "debian")
    app.config_manager.validate(config)
    path = app.configs_dir / "stick1.json"
    app.config_manager.save(config, path)
    return path


def test_install_wizard_dry_run_records_media(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    assert isinstance(wizard, InstallWizardFrame)

    children = wizard.config_table.get_children()
    assert len(children) == 1
    wizard.config_table.selection_set(children[0])
    wizard._go_next()
    app.update()

    wizard.device_path_var.set("/dev/sdb")
    wizard.device_size_var.set("32")
    wizard._go_next()
    app.update()

    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard._go_next()
    app.update()

    assert wizard.real_run_var.get() is False
    wizard._start_install()
    _pump(app, lambda: not wizard._running)

    assert len(app.session.container.data.media_records) == 1
    assert "dry-run" in wizard.message_bar.var.get()


def test_install_wizard_passes_the_real_device_not_the_form_fields(gui_app, tmp_path, monkeypatch):
    """The wizard used to hand OSInstaller a device invented from its own text
    fields, with no mountpoints. The installer unmounts the medium using
    exactly those, so an automounted stick -- the normal state of a stick a
    desktop has just seen appear -- got repartitioned underneath a live
    filesystem, and the container recorded "(введено вручную)" as its label.
    """
    from portable_os_admin.install.devices import BlockDevice
    from portable_os_admin.gui.screens import install_wizard as install_wizard_module

    real_device = BlockDevice(
        name="sdb",
        path="/dev/sdb",
        size_bytes=32_000_000_000,
        model="Cruzer Blade",
        removable=True,
        mountpoints=("/media/user/flash",),
    )

    class FakeLister:
        def list_removable(self):
            return [real_device]

        def describe(self, path):
            return real_device if path == real_device.path else None

    monkeypatch.setattr(install_wizard_module, "DeviceLister", FakeLister)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()

    # the size field is deliberately left at a rounded, slightly wrong value:
    # the real byte count has to come from the system, not from this text
    wizard.device_path_var.set("/dev/sdb")
    wizard.device_size_var.set("29")
    wizard._go_next()
    app.update()
    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard._go_next()
    app.update()

    wizard._start_install()
    _pump(app, lambda: not wizard._running)

    log = wizard.log_text.get("1.0", "end")
    assert "umount /media/user/flash" in log, "the medium is never unmounted before repartitioning"
    assert app.session.container.data.media_records[0].label == "Cruzer Blade"


def test_install_wizard_says_root_is_needed_instead_of_failing_inside_parted(gui_app, tmp_path, monkeypatch):
    """Without this the user fills in every step, confirms an irreversible
    operation, and only then gets a permission error from a tool they never
    invoked -- exactly the "errors in your face" behaviour the wizard exists to
    avoid. Nothing has been written yet at that point, so refusing is free."""
    from portable_os_admin.gui.screens import install_wizard as install_wizard_module

    monkeypatch.setattr(install_wizard_module, "_is_root", lambda: False)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()
    wizard.device_path_var.set("/dev/sdb")
    wizard.device_size_var.set("32")
    wizard._go_next()
    app.update()
    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard._go_next()
    app.update()

    # the warning appears as soon as real execution is requested...
    wizard.real_run_var.set(True)
    wizard._on_real_run_toggle()
    app.update()
    assert "root" in wizard.message_bar.var.get()

    # ...and starting anyway is refused without touching the device
    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_install()
    app.update()
    assert "root" in wizard.message_bar.var.get()
    assert wizard._running is False
    assert app.session.container.data.media_records == []

    # the plan mode remains usable without root
    wizard.real_run_var.set(False)
    wizard._on_real_run_toggle()
    wizard._start_install()
    _pump(app, lambda: not wizard._running)
    assert len(app.session.container.data.media_records) == 1


def test_install_wizard_blocks_mismatched_confirmation_before_real_run(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()
    wizard.device_path_var.set("/dev/sdb")
    wizard.device_size_var.set("32")
    wizard._go_next()
    app.update()
    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard._go_next()
    app.update()

    wizard.real_run_var.set(True)
    wizard._on_real_run_toggle()
    wizard.confirm_device_var.set("/dev/sdX-wrong")
    wizard._start_install()
    app.update()

    assert "не совпадает" in wizard.message_bar.var.get()
    # must refuse before OSInstaller.run() is ever called
    assert len(app.session.container.data.media_records) == 0
    assert wizard._running is False


def test_install_wizard_shows_no_valid_configs_guidance(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    assert wizard._valid_paths == []
    ok = wizard.validate_step(0)
    assert ok is False
    assert "конфигурац" in wizard.message_bar.var.get()


def test_configs_list_delete(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    path = _make_validated_debian_config(app)
    assert path.exists()

    app.show_frame(ConfigsListFrame)
    configs = app._current_frame
    configs.table.selection_set(configs.table.get_children()[0])
    configs._delete_selected()
    app.update()

    assert not path.exists()
    assert configs._paths == []
