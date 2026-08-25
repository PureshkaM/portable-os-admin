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


def _pump(app, condition, timeout=20.0, interval=0.05):
    """Drives Tkinter's event loop until `condition()` is true or times out
    -- needed wherever a background thread (run_in_background) is involved,
    since nothing updates until the queue is polled via app.after().

    The timeout is generous on purpose: it exists so a broken test fails
    instead of hanging forever, not to measure how fast the machine is. A
    tighter limit made the first run after a checkout fail while Python was
    still byte-compiling, which says nothing about the code under test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.update()
        if condition():
            return
        time.sleep(interval)
    app.update()
    assert condition(), "timed out waiting for background operation to complete"


def _allow_an_application(app, wizard, name: str = "mate-calc"):
    """Fills in the one thing a new configuration cannot guess.

    A new configuration is a kiosk with no applications allowed, and validation
    refuses that on purpose -- so any test that walks the wizard to the end has
    to make the same decision an administrator would.
    """
    _advance_to(app, wizard, "Режим системы и обновления")
    wizard.app_entry_var.set(name)
    wizard._add_allowed_app()
    app.update()


def _advance_to(app, wizard, title: str, limit: int = 12):
    """Walks the wizard forward until the named step, then stops.

    By title rather than by a count of clicks: the step list grows, and a test
    that says `range(5)` starts failing the moment a step is inserted -- which
    tells you nothing about the step that was added and hides whatever the test
    was actually checking.
    """
    for _ in range(limit):
        if wizard.step_titles[wizard.step_index] == title:
            return
        wizard._go_next()
        app.update()
    raise AssertionError(
        f"шаг {title!r} не достигнут; остановились на "
        f"{wizard.step_titles[wizard.step_index]!r}"
    )


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

    _allow_an_application(app, wizard)
    _advance_to(app, wizard, "Проверка и сохранение")

    wizard._run_validation()
    app.update()
    assert wizard.config.validation_status == "valid", wizard.config.validation_errors

    wizard._go_next()  # finish: save + navigate to configs list
    app.update()

    configs = app._current_frame
    assert isinstance(configs, ConfigsListFrame)
    assert [p.name for p in configs._paths] == ["stick1.json"]


def test_config_wizard_final_step_edits_are_applied_or_refused(gui_app, tmp_path):
    """The last step lets the administrator hand-edit the configuration. Two
    things must hold: an applied edit really changes what gets saved, and text
    left unapplied is not silently thrown away with a success message."""
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    wizard.name_var.set("stick1")
    _allow_an_application(app, wizard)
    _advance_to(app, wizard, "Проверка и сохранение")

    text = wizard.config_text.get("1.0", "end")
    wizard.config_text.delete("1.0", "end")
    wizard.config_text.insert("1.0", text.replace('"release": "bookworm"', '"release": "trixie"'))

    # not applied yet -> saving must refuse rather than discard the edit
    wizard._go_next()
    app.update()
    assert "неприменённые" in wizard.message_bar.var.get()
    assert not (app.configs_dir / "stick1.json").exists()

    wizard._apply_edits()
    app.update()
    assert wizard.config.release == "trixie"
    # editing invalidates the previous verdict, since the installer only
    # accepts configurations that passed validation
    assert wizard.config.validation_status == "unchecked"

    wizard._run_validation()
    wizard._go_next()
    app.update()
    saved = app.config_manager.open(app.configs_dir / "stick1.json")
    assert saved.release == "trixie"


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


class _FakeSource:
    """Three packages whose alphabetical and version orders differ, so a sort
    test cannot pass by accident."""

    def list_packages(self, mirror, release):
        return [
            RepoPackage(name="mc", version="4.8", summary="midnight commander"),
            RepoPackage(name="alpha", version="1.0", summary="first package"),
            RepoPackage(name="htop", version="3.2.0", summary="interactive process viewer"),
        ]


def test_config_wizard_package_picker_add_and_remove(gui_app, tmp_path, monkeypatch):
    monkeypatch.setattr(config_wizard_module, "get_package_source", lambda target_os: _FakeSource())

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    wizard.name_var.set("stick1")

    _advance_to(app, wizard, "Пакеты для предустановки")

    wizard._connect_repo()
    _pump(app, lambda: bool(wizard._catalog))
    assert len(wizard._catalog) == 3

    # several rows at once, the way Ctrl/Shift-clicking leaves the table
    rows = wizard.catalog_table.get_children()
    wizard.catalog_table.selection_set(rows[0], rows[2])
    wizard._add_package()
    app.update()
    assert sorted(wizard.config.packages) == ["htop", "mc"]

    wizard.selected_list.selection_set(0, "end")
    wizard._remove_package()
    app.update()
    assert wizard.config.packages == []


def test_config_wizard_package_list_sorts_by_any_column(gui_app, tmp_path, monkeypatch):
    """Sorting has to reach the whole fetched catalogue, not just the rows
    currently drawn: the table shows the first N of what can be tens of
    thousands of packages, so sorting only the visible page would order an
    arbitrary slice and call it sorted."""
    monkeypatch.setattr(config_wizard_module, "get_package_source", lambda target_os: _FakeSource())

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    wizard.name_var.set("stick1")
    _advance_to(app, wizard, "Пакеты для предустановки")

    wizard._connect_repo()
    _pump(app, lambda: bool(wizard._catalog))

    def column(name):
        return [wizard.catalog_table.item(r)["values"][{"name": 0, "version": 1}[name]]
                for r in wizard.catalog_table.get_children()]

    wizard._sort_catalog("name")
    assert column("name") == ["alpha", "htop", "mc"]
    wizard._sort_catalog("name")  # same column again -> reverse
    assert column("name") == ["mc", "htop", "alpha"]

    wizard._sort_catalog("version")
    assert column("version") == ["1.0", "3.2.0", "4.8"]


def _make_validated_debian_config(app, iso_path=None):
    config = app.config_manager.create(app.configs_dir, "stick1", "debian")
    # A configuration without an installation image cannot validate: the system
    # is installed from a local image, never downloaded.
    config.image_path = iso_path or _throwaway_image(app)
    app.config_manager.validate(config)
    path = app.configs_dir / "stick1.json"
    app.config_manager.save(config, path)
    return path


class _StubInstaller:
    """Stands in for OSInstaller so wizard tests never touch a disk.

    Delegates to the real orchestration in planning mode, so everything the
    wizard itself is responsible for -- resolving the device, the confirmation
    gate, saving the container, rendering the log -- runs for real. The wizard
    no longer offers a planning mode to users; it survives here only as a test
    seam.
    """

    last_device = None
    last_dry_run = None
    last_image_path = None

    def __init__(self, session):
        from portable_os_admin.install import OSInstaller

        self._real = OSInstaller(session)

    def run(self, device, config, passphrase, *, image_path="", dry_run, confirm_device, on_step=None):
        _StubInstaller.last_device = device
        _StubInstaller.last_dry_run = dry_run
        _StubInstaller.last_image_path = image_path
        return self._real.run(
            device, config, passphrase, image_path=image_path, dry_run=True,
            confirm_device=confirm_device, on_step=on_step,
        )


def _stub_install(monkeypatch):
    """Patches out the installer and the root check, so these tests behave the
    same on a developer machine as on the root shell of a test VM."""
    from portable_os_admin.gui.screens import install_wizard as install_wizard_module

    monkeypatch.setattr(install_wizard_module, "OSInstaller", _StubInstaller)
    monkeypatch.setattr(install_wizard_module, "_is_root", lambda: True)
    # The wizard refuses to start while an unfinished install is on the machine
    # -- correctly, since a second run on top of an open LUKS mapping corrupts
    # both. That check reads global system state, so on a machine where a real
    # install happens to be running these tests would fail for a reason that
    # has nothing to do with them. Observed exactly that way, while a genuine
    # install was writing to a stick in the next process.
    monkeypatch.setattr(install_wizard_module.leftovers, "find", lambda: None)
    return install_wizard_module


def _fill_install_wizard(app, wizard, device="/dev/sdb", size="32"):
    # the image comes first: it decides which configurations can be used at all
    wizard.image_path_var.set(_throwaway_image(app))
    wizard._go_next()
    app.update()

    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()

    wizard.device_path_var.set(device)
    wizard.device_size_var.set(size)
    wizard._go_next()
    app.update()
    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard._go_next()
    app.update()


def _throwaway_image(app):
    path = app.configs_dir.parent / "image.iso"
    path.write_bytes(b"not a real image")
    return str(path)


def test_install_wizard_records_media_and_recovery(gui_app, tmp_path, monkeypatch):
    _stub_install(monkeypatch)
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    assert isinstance(wizard, InstallWizardFrame)

    _fill_install_wizard(app, wizard)
    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_install()
    _pump(app, lambda: not wizard._running)

    # the wizard asks for a real install -- there is no longer a preview mode
    assert _StubInstaller.last_dry_run is False
    assert len(app.session.container.data.media_records) == 1
    assert len(app.session.container.data.recovery_records) == 1
    assert "код восстановления" in wizard.message_bar.var.get()


def test_install_wizard_passes_the_real_device_not_the_form_fields(gui_app, tmp_path, monkeypatch):
    """The wizard used to hand OSInstaller a device invented from its own text
    fields, with no mountpoints. The installer unmounts the medium using
    exactly those, so an automounted stick -- the normal state of a stick a
    desktop has just seen appear -- got repartitioned underneath a live
    filesystem, and the container recorded "(введено вручную)" as its label.
    """
    from portable_os_admin.install.devices import BlockDevice, Partition

    install_wizard_module = _stub_install(monkeypatch)
    real_device = BlockDevice(
        name="sdb",
        path="/dev/sdb",
        size_bytes=32_000_000_000,
        model="Cruzer Blade",
        removable=True,
        mountpoints=("/media/user/flash",),
        partitions=(Partition("/dev/sdb1", 32_000_000_000, "ext4", "DATA", "/media/user/flash"),),
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
    # the size field is deliberately left at a rounded, slightly wrong value:
    # the real byte count has to come from the system, not from this text
    _fill_install_wizard(app, wizard, size="29")

    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_install()
    _pump(app, lambda: not wizard._running)

    assert _StubInstaller.last_device.size_bytes == 32_000_000_000
    log = wizard.log_text.get("1.0", "end")
    assert "umount /media/user/flash" in log, "the medium is never unmounted before repartitioning"
    # the old partition's own signatures must go before the table is replaced
    assert "wipefs --all --force /dev/sdb1" in log
    assert app.session.container.data.media_records[0].label == "Cruzer Blade"


def test_install_wizard_says_root_is_needed_instead_of_failing_inside_parted(gui_app, tmp_path, monkeypatch):
    """Without this the user fills in every step, confirms an irreversible
    operation, and only then gets a permission error from a tool they never
    invoked -- exactly the "errors in your face" behaviour the wizard exists to
    avoid. Nothing has been written yet at that point, so refusing is free."""
    install_wizard_module = _stub_install(monkeypatch)
    monkeypatch.setattr(install_wizard_module, "_is_root", lambda: False)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    # the warning is already on screen when the last step opens...
    assert "root" in wizard.message_bar.var.get()

    # ...and starting anyway is refused without touching the device
    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_install()
    app.update()
    assert "root" in wizard.message_bar.var.get()
    assert wizard._running is False
    assert app.session.container.data.media_records == []


def test_install_wizard_blocks_mismatched_confirmation(gui_app, tmp_path, monkeypatch):
    _stub_install(monkeypatch)
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    wizard.confirm_device_var.set("/dev/sdX-wrong")
    wizard._start_install()
    app.update()

    assert "не совпадает" in wizard.message_bar.var.get()
    # must refuse before OSInstaller.run() is ever called
    assert len(app.session.container.data.media_records) == 0
    assert wizard._running is False


def test_install_wizard_lists_what_will_be_destroyed(gui_app, tmp_path, monkeypatch):
    """An irreversible action needs to say what it destroys. "Устройство будет
    очищено" is not something a person can check themselves against; the
    partitions, their sizes and labels are."""
    from portable_os_admin.install.devices import BlockDevice, Partition

    install_wizard_module = _stub_install(monkeypatch)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer Blade",
        removable=True, mountpoints=("/media/user/photos",),
        partitions=(
            Partition("/dev/sdb1", 1_000_000_000, "vfat", "BOOT", ""),
            Partition("/dev/sdb2", 31_000_000_000, "ext4", "ФОТО", "/media/user/photos"),
        ),
    )

    class FakeLister:
        def list_removable(self):
            return [device]

        def describe(self, path):
            return device

    monkeypatch.setattr(install_wizard_module, "DeviceLister", FakeLister)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)
    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    shown = _visible_text(wizard)
    assert "/dev/sdb1" in shown and "/dev/sdb2" in shown
    assert "ФОТО" in shown
    assert "уничтожено" in shown
    assert "/media/user/photos" in shown


def _visible_text(widget) -> str:
    """Every label caption under `widget`, so a test can assert on what the
    screen actually says rather than on internal state."""
    found = []
    for child in widget.winfo_children():
        try:
            text = child.cget("text")
        except tk.TclError:
            text = ""
        if text:
            found.append(str(text))
        found.append(_visible_text(child))
    return "\n".join(found)


def test_install_wizard_shows_no_valid_configs_guidance(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    wizard.image_path_var.set(_throwaway_image(app))
    wizard._go_next()  # image -> configuration
    app.update()

    assert wizard._valid_paths == []
    assert wizard.validate_step(1) is False
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


def test_configurations_are_marked_against_the_chosen_image(gui_app, tmp_path, monkeypatch):
    """The image is chosen first because it decides what can be installed at
    all. A configuration written for another family cannot be used with it, and
    saying so in the list is more useful than an error after the choice."""
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.gui.screens import install_wizard as install_wizard_module
    from portable_os_admin.install.image_source import ImageInfo

    app = gui_app
    _open_container(app, tmp_path)
    for name, target_os in (("debian-stick", "debian"), ("redos-stick", "redos")):
        config = InstallConfig.create_default(name, target_os)
        app.config_manager.validate(config)
        app.config_manager.save(config, app.configs_dir / f"{name}.json")

    debian_image = ImageInfo(
        path="/images/debian.iso", family="debian", release="trixie",
        architectures=("amd64",), label="Debian 13.6 DVD",
    )
    monkeypatch.setattr(
        install_wizard_module.image_source, "probe", lambda path: debian_image
    )

    app.show_frame(InstallWizardFrame)
    wizard = app._current_frame
    assert wizard.step_titles[0] == "Образ ОС"

    wizard.image_path_var.set(_throwaway_image(app))
    wizard._examine_image(wizard.image_path_var.get())
    _pump(app, lambda: wizard.image_info is not None)
    wizard._go_next()
    app.update()

    rows = {
        wizard.config_table.item(r)["values"][0]: wizard.config_table.item(r)
        for r in wizard.config_table.get_children()
    }
    assert rows["debian-stick.json"]["values"][2] == "да"
    assert "нет" in str(rows["redos-stick.json"]["values"][2])
    # the unusable one stays visible but marked, rather than vanishing
    assert "unfit" in rows["redos-stick.json"]["tags"]

    # and choosing it is refused, with the reason
    redos_row = next(
        r for r in wizard.config_table.get_children()
        if wizard.config_table.item(r)["values"][0] == "redos-stick.json"
    )
    wizard.config_table.selection_set(redos_row)
    wizard._go_next()
    app.update()
    assert wizard.step_titles[wizard.step_index] == "Конфигурация"
    assert "не подходит" in wizard.message_bar.var.get()


def test_leaving_a_screen_does_not_leave_tk_variables_for_another_thread(gui_app, tmp_path):
    """A tk.Variable finalises by calling into Tcl, and Python's cyclic
    collector runs on whichever thread trips its threshold. An orphan Variable
    left by a destroyed screen can therefore be finalised on a worker thread,
    which calls Tcl from the wrong thread and blocks that thread permanently --
    observed as an install parked in tkinter's __del__ while the window went on
    responding, so the install neither finished nor failed.

    The fix is to finalise them on the UI thread, right where they are
    orphaned. This checks that nothing of Tk's is left uncollected afterwards.
    """
    import gc
    import tkinter as tk

    app = gui_app
    _open_container(app, tmp_path)

    def live_variables():
        return sum(1 for obj in gc.get_objects() if isinstance(obj, tk.Variable))

    app.show_dashboard()
    before = live_variables()
    for _ in range(3):
        app.show_dashboard()
    after = live_variables()

    # Screens are rebuilt, so their variables must not pile up: without the
    # sweep each round leaves another set behind for some thread to finalise.
    assert after <= before, f"{after - before} tk.Variable objects left behind by screen changes"


def test_dashboard_state_check_reports_instead_of_crashing(gui_app, tmp_path, monkeypatch):
    """Regression: the button raised instead of answering.

    `_check_leftovers` called `self.message_bar`, `leftovers` and `messagebox`
    -- none of which the dashboard created or imported. So the one control that
    exists to tell an administrator whether an interrupted install left the
    medium mounted and its encrypted volume open failed with an AttributeError
    the moment it was pressed, and did so in the situation where it was needed
    most.
    """
    from portable_os_admin.gui.screens import dashboard as dashboard_module

    app = gui_app
    _open_container(app, tmp_path)
    app.show_dashboard()
    app.update()
    frame = app._current_frame

    monkeypatch.setattr(dashboard_module.leftovers, "find", lambda: None)
    frame._check_leftovers()
    app.update()

    assert "Незавершённых установок нет" in frame.message_bar.var.get()


def test_new_configuration_opens_as_a_locked_down_kiosk(gui_app, tmp_path):
    """The wizard's defaults are a statement about what this tool builds.

    Opening on an ordinary console system meant a restricted medium was
    something you had to know to ask for.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame

    assert wizard.config.profile == "kiosk"
    assert wizard.config.kiosk.enabled is True
    assert wizard.config.kiosk.allow_removable_media is False
    assert wizard.config.account.sudo is False


def test_architecture_can_be_chosen_in_the_interface(gui_app, tmp_path):
    """Regression: it was only reachable by editing the JSON.

    Empty means "same as this machine", so an administrator on arm64 building
    for an amd64 fleet was refused at install time with no way, in the
    interface, to say what they meant.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame

    assert hasattr(wizard, "arch_var"), "в мастере нет выбора архитектуры"
    wizard.arch_var.set("amd64")
    wizard._on_arch_changed()
    app.update()
    # Whatever this machine is, the interface must say something about whether
    # the build is possible rather than leave the question open.
    assert wizard.arch_status_var.get().strip()

    wizard._save_step_basics()
    assert wizard.config.arch == "amd64"


def test_same_as_host_is_stored_as_an_empty_architecture(gui_app, tmp_path):
    """The label is for reading; the configuration keeps the schema's spelling."""
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame

    wizard.arch_var.set(config_wizard_module.ARCH_SAME_AS_HOST)
    wizard._save_step_basics()

    assert wizard.config.arch == ""


def test_final_step_lists_what_the_administrator_changed(gui_app, tmp_path):
    """The file says what will be installed; this says what they did to it."""
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    wizard.name_var.set("stick1")
    wizard.arch_var.set("amd64")
    _allow_an_application(app, wizard)
    wizard.allow_media_var.set(True)
    _advance_to(app, wizard, "Проверка и сохранение")

    changes = dict(
        (label, (before, after)) for label, before, after in wizard.config.diff_from(wizard._baseline)
    )
    assert changes["Разрешённые приложения"] == ("(пусто)", "mate-calc")
    assert changes["Съёмные носители"] == ("нет", "да")
    assert changes["Архитектура носителя"] == ("(пусто)", "amd64")


def test_building_a_distribution_is_reachable_and_asks_for_no_medium(gui_app, tmp_path):
    """The two halves are separate screens, not modes of one.

    Building erases nothing and needs no device, so the screen must not ask
    for one -- no device step, no confirmation, no passphrase. That absence is
    the point: it is what makes this operation safe to run at any time.
    """
    from portable_os_admin.gui.screens.build_wizard import BuildWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(BuildWizardFrame)
    app.update()
    wizard = app._current_frame

    assert isinstance(wizard, BuildWizardFrame)
    titles = " ".join(wizard.step_titles).lower()
    for medium_word in ("носитель", "пароль", "подтверждение"):
        assert medium_word not in titles, f"в сборке дистрибутива спрашивается «{medium_word}»"
    assert "образ" in titles
