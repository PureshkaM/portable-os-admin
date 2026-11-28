"""Exercise the shared desktop workflows using the chosen widget backend."""
from __future__ import annotations

import time

import pytest

from portable_os_admin.config import InstallConfig
from portable_os_admin.config.repo_browser import RepoPackage
from portable_os_admin.config.schema import KioskConfig
from portable_os_admin.gui import theme  # noqa: F401 -- import-ability check
from portable_os_admin.gui.ui import filedialog, messagebox, simpledialog, tk, ttk, using_gtk
from portable_os_admin.gui.app import PortableOSAdminGUI
from portable_os_admin.gui.screens import config_wizard as config_wizard_module
from portable_os_admin.gui.screens import device_step as device_step_module
from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame
from portable_os_admin.gui.screens.configs_list import ConfigsListFrame
from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame
from portable_os_admin.gui.screens.startup import StartupFrame
from portable_os_admin.install import BlockDevice, machine_binding


@pytest.fixture
def gui_app():
    try:
        app = PortableOSAdminGUI()
    except tk.TclError as exc:
        pytest.skip(f"no usable display in this environment: {exc}")
    if not using_gtk():
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


@pytest.mark.skipif(not using_gtk(), reason="GTK entry and button signals")
def test_gtk_password_fields_and_button_create_a_container(gui_app, tmp_path):
    from gi.repository import Gtk
    from portable_os_admin.gui.screens.startup import MODE_CREATE

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    startup = gui_app._current_frame
    destination = tmp_path / "new.container"
    startup._chosen(str(destination), MODE_CREATE)
    gui_app.update()
    masked_entries = [
        item.widget for item in descendants(startup)
        if isinstance(item.widget, Gtk.Entry) and not item.widget.get_visibility()
    ]
    assert len(masked_entries) == 2
    for entry in masked_entries:
        entry.set_text("FixturePass123")
    create = next(
        item.widget for item in descendants(startup)
        if isinstance(item.widget, Gtk.Button) and item.cget("text") == "Создать"
    )
    create.emit("clicked")
    _pump(gui_app, lambda: gui_app.container is not None)
    assert destination.is_file()


def _allow_an_application(app, wizard, name: str = "mate-calc"):
    """Fills in the one thing a new configuration cannot guess.

    A new configuration is a kiosk with nothing installed on top of the
    minimal system, and validation refuses that on purpose -- so any test that
    walks the wizard to the end has to make the same decision an administrator
    would: name a package.

    Отдельного списка «разрешённых приложений» больше нет: то, что доступно
    пользователю, выводится из пакетов администратора. Но добавляются они
    выбором строки в каталоге репозитория, а каталога в тестах нет -- за ним
    нужна сеть и зеркало, -- поэтому пакет кладётся туда же, куда его положил
    бы `_add_package`.
    """
    _advance_to(app, wizard, "Пакеты для предустановки")
    if name not in wizard.config.packages:
        wizard.config.packages.append(name)
        wizard.selected_list.insert("end", name)
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


def test_recovery_screen_tells_the_administrator_what_is_being_asked(gui_app, tmp_path):
    """Забытый пароль и исчерпанный носитель приходят одной строкой.

    Различает их вид, упакованный в сам запрос. Администратору это надо
    сказать до того, как код уйдёт пользователю: во втором случае носитель
    забудет все машины и начнёт набирать их заново, и предупредить об этом
    задним числом уже поздно.
    """
    import base64

    from portable_os_admin import crypto
    from portable_os_admin.container import MediaRecord, RecoveryRecord
    from portable_os_admin.gui.screens.recovery import RecoveryFrame
    from portable_os_admin.recovery.request import KIND_MACHINES, RecoveryRequest

    app = gui_app
    _open_container(app, tmp_path)

    media_id = "abcd1234"
    app.session.container.add_media_record(
        MediaRecord(
            media_id=media_id, luks_uuid="u-1", label="stick",
            config_name="cfg", target_os="redos",
            owner="Иванов Иван Иванович",
        )
    )
    for slot in (3, 4):
        app.session.container.add_recovery_record(
            RecoveryRecord(
                media_id=media_id, slot=slot, code=crypto.generate_recovery_code(),
                salt_b64=base64.b64encode(crypto.new_salt()).decode(),
            )
        )

    app.show_frame(RecoveryFrame)
    screen = app._current_frame

    screen.request_var.set(RecoveryRequest(media_id=media_id, slot=3).encode())
    # Нажатий два: первое называет владельца, второе выдаёт код. Администратор
    # обязан услышать имя от звонящего прежде, чем код уйдёт в трубку.
    screen._issue()
    app.update()
    assert "Иванов Иван Иванович" in screen.message_bar.var.get()
    screen._issue()
    app.update()
    assert "забыл пароль" in screen.message_bar.var.get()

    screen.request_var.set(
        RecoveryRequest(media_id=media_id, slot=4, kind=KIND_MACHINES).encode()
    )
    screen._issue()
    app.update()
    screen._issue()
    app.update()
    message = screen.message_bar.var.get()
    assert "исчерпал список машин" in message
    assert "забудет все машины" in message, "администратора не предупредили о последствии"


def test_the_code_is_not_issued_until_the_owner_has_been_named(gui_app, tmp_path):
    """Выдача кода опознаёт человека, а не только носитель.

    До 11.09.2026 код уходил по первому нажатию, и проверялись три вещи:
    разбирается ли запрос, есть ли запись, свободен ли слот. О звонящем не
    спрашивалось ничего -- а звонить может любой, кто нашёл чужую флешку:
    носитель показывает код запроса сам, стоит трижды ошибиться паролем.
    Проверяется здесь именно то, что стоит денег: recovery-запись после первого
    нажатия обязана остаться **неиспользованной**, иначе носитель терял бы слот
    на каждом звонке неизвестно кому.
    """
    import base64

    from portable_os_admin import crypto
    from portable_os_admin.container import MediaRecord, RecoveryRecord
    from portable_os_admin.gui.screens.recovery import RecoveryFrame
    from portable_os_admin.recovery.request import RecoveryRequest

    app = gui_app
    _open_container(app, tmp_path)

    media_id = "abcd1234"
    app.session.container.add_media_record(
        MediaRecord(
            media_id=media_id, luks_uuid="u-1", label="stick",
            config_name="cfg", target_os="redos",
            owner="Петрова Анна Сергеевна",
        )
    )
    app.session.container.add_recovery_record(
        RecoveryRecord(
            media_id=media_id, slot=3, code=crypto.generate_recovery_code(),
            salt_b64=base64.b64encode(crypto.new_salt()).decode(),
        )
    )

    app.show_frame(RecoveryFrame)
    screen = app._current_frame
    screen.request_var.set(RecoveryRequest(media_id=media_id, slot=3).encode())

    screen._issue()
    app.update()
    first = screen.message_bar.var.get()
    assert "Петрова Анна Сергеевна" in first, "администратору не назвали владельца"
    record = app.session.container.find_recovery(media_id, 3)
    assert record.status == "available", "слот сожжён до того, как звонящего опознали"
    assert record.code not in first, "код ушёл раньше опознания"

    screen._issue()
    app.update()
    assert app.session.container.find_recovery(media_id, 3).status == "used"


def test_the_handover_step_refuses_a_medium_without_an_owner(gui_app, tmp_path):
    """Владельца спрашивают при выдаче, потому что потом спросить не у кого.

    Через полгода никто не помнит, кому ушла двадцатая флешка из тиража, а
    именно тогда владелец и понадобится: с носителя попросят код
    восстановления, и опознать звонящего будет нечем. Поэтому поле обязательное
    -- единственное обязательное на этом шаге.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    # Шаг принадлежит ветви готового дистрибутива: файлы в раздел обмена
    # установщик принимает только при развёртывании. Поэтому источник
    # выбирается по-настоящему, а не подставляется в обход мастера -- иначе
    # тест проверял бы состояние, в которое интерфейс не приводит.
    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    assert wizard.validate_step(0) is True, wizard.message_bar.var.get()

    # Дальше шаг открывается напрямую: проверяется он сам, а не дорога к нему.
    wizard.step_index = wizard.step_titles.index("Кому и что")
    wizard._render_step()
    app.update()

    wizard.media_name_var.set("Иванов, бухгалтерия")
    wizard.media_owner_var.set("")
    wizard.save_step(wizard.step_index)
    assert wizard.validate_step(wizard.step_index) is False, "носитель выдан без владельца"
    assert "владельц" in wizard.message_bar.var.get().lower()

    wizard.media_owner_var.set("Сидоров Пётр Петрович")
    wizard.save_step(wizard.step_index)
    assert wizard.validate_step(wizard.step_index) is True
    assert wizard.media_owner == "Сидоров Пётр Петрович"


def test_a_mistyped_new_password_is_refused_before_the_container_is_made(gui_app, tmp_path):
    """Опечатка при создании запирает контейнер навсегда.

    Опечатка в пароле к существующему контейнеру обнаруживается сразу -- он
    просто не откроется. Опечатка при создании не обнаруживается ничем: узнают
    о ней при следующем запуске, когда в контейнере уже лежит работа.
    """
    from portable_os_admin.gui.screens.startup import MODE_CREATE

    app = gui_app
    startup = app._current_frame
    target = tmp_path / "новый.container"

    startup._chosen(target, MODE_CREATE)
    startup.password_var.set("CorrectHorse123")
    startup.confirm_var.set("CorrectHorse124")
    startup._confirm()
    app.update()

    assert app.session is None, "контейнер создан с неподтверждённым паролем"
    assert not target.exists()
    assert "не совпадают" in startup.message_bar.var.get()

    startup.confirm_var.set("CorrectHorse123")
    startup._confirm()
    app.update()

    assert app.session is not None
    assert target.exists()


def test_closing_the_session_ends_the_program(gui_app, tmp_path, monkeypatch):
    """«Закрыть сеанс» завершает программу, а не возвращает на начальный экран.

    Иначе затирание неполно: ключ контейнера лежит в bytearray и затирается, а
    секрет администратора -- строкой, которую в Python не затереть. Пока
    процесс жив, она читается через /proc/PID/mem и может уйти в своп.
    """
    app = gui_app
    _open_container(app, tmp_path)
    quits = []
    monkeypatch.setattr(app, "_quit", lambda: quits.append(True))

    app.close_session()
    app.update()

    assert quits, "программа осталась работать после конца сеанса"
    assert app.session is None


def test_the_window_cross_ends_the_program_the_same_way(gui_app, tmp_path, monkeypatch):
    """Крестик -- такой же конец работы, и путь у него тот же.

    Раньше он сохранял контейнер и уничтожал окно, оставляя секреты в памяти
    умирающего процесса на волю случая.
    """
    app = gui_app
    _open_container(app, tmp_path)
    quits = []
    monkeypatch.setattr(app, "_quit", lambda: quits.append(True))

    app._on_close_window()
    app.update()

    assert quits, "крестик не завершил программу"
    assert app.session is None


def test_both_ways_out_ask_while_a_medium_is_being_written(gui_app, tmp_path, monkeypatch):
    """Прерывание записи стоит носителя, поэтому спрашивают оба пути.

    До 17.09.2026 вопрос задавал только крестик, а «Закрыть сеанс» посреди
    записи убивало программу молча — и человек узнавал о непригодном носителе
    от флешки, а не от программы.
    """
    app = gui_app
    _open_container(app, tmp_path)
    quits: list = []
    asked: list = []
    monkeypatch.setattr(app, "_quit", lambda: quits.append(True))
    monkeypatch.setattr(app, "install_in_progress", lambda: True)
    monkeypatch.setattr(
        messagebox, "askyesno", lambda *a, **k: asked.append(True) or False
    )

    app.close_session()
    assert asked, "кнопка не спросила посреди записи"
    assert not quits, "программа закрылась вопреки отказу"
    assert app.session is not None, "сеанс закрыт вопреки отказу"

    asked.clear()
    app._on_close_window()
    assert asked, "крестик не спросил посреди записи"
    assert not quits


def test_a_kill_signal_is_handled_like_the_window_cross(gui_app):
    """Выход из системы и `kill` — тоже нештатное закрытие.

    Без обработчика процесс умирает мгновенно: секреты остаются незатёртыми, а
    служебные тома — открытыми устройствами /dev/mapper, то есть доступом к
    журналам и разделу обмена носителя без всякого пароля.

    Проверяется, что обработчик поставлен, а не что он делает: вызвать его
    по-настоящему значило бы завершить процесс вместе с набором.
    """
    import signal

    app = gui_app
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        handler = signal.getsignal(sig)
        assert getattr(handler, "__self__", None) is app, (
            f"сигнал {sig.name} не ведёт в корректное завершение"
        )
        assert handler.__func__ is type(app)._on_termination


def test_startup_wrong_password_shows_error(gui_app, tmp_path):
    app = gui_app
    _open_container(app, tmp_path)
    app.end_session()
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
    assert wizard.release_var.get() == "8.0c"

    _allow_an_application(app, wizard)
    _advance_to(app, wizard, "Проверка и сохранение")

    wizard._run_validation()
    app.update()
    assert wizard.config.validation_status == "valid", wizard.config.validation_errors

    wizard._go_next()  # finish: save + navigate to configs list
    app.update()

    configs = app._current_frame
    assert isinstance(configs, ConfigsListFrame)
    assert configs._names == ["stick1"]


def test_config_wizard_final_step_edits_are_applied_or_refused(gui_app, tmp_path):
    """The last step lets the administrator hand-edit the configuration. Two
    things must hold: an applied edit really changes what gets saved, and text
    left unapplied is not silently thrown away with a success message."""
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    # Текст конфигурации показывают только в экспертном режиме: в упрощённом
    # на последнем шаге стоит список изменений по-русски, и править там
    # нечего. Режим запоминается между запусками, поэтому тест называет его
    # сам, а не полагается на то, каким программу оставили в прошлый раз.
    wizard._mode_var.set(True)
    wizard._on_mode_toggled()
    app.update()
    wizard.name_var.set("stick1")
    _allow_an_application(app, wizard)
    _advance_to(app, wizard, "Проверка и сохранение")

    # Правка выбрана так, чтобы она была правкой при любом дистрибутиве:
    # прежняя меняла «bookworm» на «trixie», а с тех пор trixie стал выпуском
    # Debian по умолчанию -- замена не меняла ничего, и тест проверял пустоту.
    text = wizard.config_text.get("1.0", "end")
    assert '"timezone": "UTC"' in text, "поменялся текст конфигурации"
    wizard.config_text.delete("1.0", "end")
    wizard.config_text.insert(
        "1.0", text.replace('"timezone": "UTC"', '"timezone": "Asia/Yekaterinburg"')
    )

    # not applied yet -> saving must refuse rather than discard the edit
    wizard._go_next()
    app.update()
    assert "неприменённые" in wizard.message_bar.var.get()
    assert app.config_manager.list_configs(app.container) == []

    wizard._apply_edits()
    app.update()
    assert wizard.config.localisation.timezone == "Asia/Yekaterinburg"
    # editing invalidates the previous verdict, since the installer only
    # accepts configurations that passed validation
    assert wizard.config.validation_status == "unchecked"

    wizard._run_validation()
    wizard._go_next()
    app.update()
    saved = app.config_manager.open(app.container, "stick1")
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


def test_config_wizard_wheel_over_a_combobox_does_not_change_it(gui_app, tmp_path):
    """Прокрутка формы колесом не должна менять настройку под курсором.

    Найдено живым прогоном 28.08.2026: при листании шага «Диск и шифрование»
    курсор прошёл над списком «Шифр LUKS», ttk::combobox обработал колесо
    раньше прокрутки формы, и шифр молча съехал с aes-xts-plain64 на
    serpent-xts-plain64. Увидеть это удалось только на сводке последнего шага
    -- том был бы зашифрован не тем алгоритмом, который выбирал администратор.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame

    wizard.name_var.set("stick1")
    wizard._go_next()  # -> учётная запись
    app.update()
    wizard._go_next()  # -> диск и шифрование
    app.update()

    before = wizard.cipher_var.get()
    # Button-4/5 -- колесо в X11, то самое событие, что пришло от живой мыши.
    for _ in range(3):
        wizard.cipher_combo.event_generate("<Button-5>", x=5, y=5)
        app.update()
    assert wizard.cipher_var.get() == before

    for _ in range(3):
        wizard.cipher_combo.event_generate("<Button-4>", x=5, y=5)
        app.update()
    assert wizard.cipher_var.get() == before


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
    """Проверенная конфигурация в контейнере; возвращает её имя.

    Имя, а не путь: с 09.09.2026 конфигурации живут внутри контейнера, и
    экраны адресуют их именем.
    """
    config = app.config_manager.create(app.container, "stick1", "debian")
    config.kiosk.enabled = True
    config.profile = "kiosk"
    config.packages = ["mate-calc"]
    # A configuration without an installation image cannot validate: the system
    # is installed from a local image, never downloaded.
    config.image_path = iso_path or _throwaway_image(app)
    app.config_manager.validate(config)
    app.config_manager.save(app.container, config)
    return config.name


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
    last_config = None

    def __init__(self, session):
        from portable_os_admin.install import OSInstaller

        self._real = OSInstaller(session)

    def run(
        self, device, config, passphrase, *, image_path="", dry_run, confirm_device,
        on_step=None, media_name="", media_owner="",
    ):
        _StubInstaller.last_device = device
        _StubInstaller.last_dry_run = dry_run
        _StubInstaller.last_image_path = image_path
        _StubInstaller.last_config = config
        return self._real.run(
            device, config, passphrase, image_path=image_path, dry_run=True,
            confirm_device=confirm_device, on_step=on_step,
            media_name=media_name, media_owner=media_owner,
        )


def _stub_install(monkeypatch):
    """Patches out the installer and the root check, so these tests behave the
    same on a developer machine as on the root shell of a test VM."""
    from portable_os_admin.admin import media_release as media_release_module
    from portable_os_admin.gui.screens import release_wizard as release_wizard_module

    # Подменяется одно место на все три пути выпуска: мастер больше не создаёт
    # установщик сам -- его создаёт служба выпуска (зона И3).
    monkeypatch.setattr(media_release_module, "OSInstaller", _StubInstaller)
    monkeypatch.setattr(release_wizard_module, "is_root", lambda: True)
    # The wizard refuses to start while an unfinished install is on the machine
    # -- correctly, since a second run on top of an open LUKS mapping corrupts
    # both. That check reads global system state, so on a machine where a real
    # install happens to be running these tests would fail for a reason that
    # has nothing to do with them. Observed exactly that way, while a genuine
    # install was writing to a stick in the next process.
    monkeypatch.setattr(release_wizard_module.leftovers, "find", lambda: None)
    disk = BlockDevice("sdb", "/dev/sdb", 32_000_000_000, "Test USB", True, (), diskseq=10, mounts_known=True)
    monkeypatch.setattr(device_step_module.DeviceLister, "describe", lambda self, path: disk if path == disk.path else None)
    return release_wizard_module


def _fill_install_wizard(app, wizard, device="/dev/sdb"):
    # Источник идёт первым: из него выводится и путь выпуска, и то, какие
    # конфигурации вообще годятся.
    wizard.source_var.set(_throwaway_image(app))
    wizard._go_next()
    app.update()

    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()

    wizard.device_path_var.set(device)
    wizard._go_next()
    app.update()
    wizard.passphrase_var.set("user-passphrase-123")
    wizard.passphrase_confirm_var.set("user-passphrase-123")
    wizard.media_name_var.set("Иванов, бухгалтерия")
    wizard.media_owner_var.set("Иванов Иван Иванович")
    wizard._go_next()
    app.update()


def _throwaway_image(app):
    path = app.container_path.parent / "image.iso"
    path.write_bytes(b"not a real image")
    return str(path)


def test_install_wizard_records_media_and_recovery(gui_app, tmp_path, monkeypatch):
    _stub_install(monkeypatch)
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    assert isinstance(wizard, ReleaseWizardFrame)

    _fill_install_wizard(app, wizard)
    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_write()
    _pump(app, lambda: not wizard._running)

    # the wizard asks for a real install -- there is no longer a preview mode
    assert _StubInstaller.last_dry_run is False
    assert len(app.session.container.data.media_records) == 1
    # Владелец обязателен на обоих путях записи носителя, а не только в
    # мастере выдачи: по нему на экране восстановления опознают звонящего.
    assert app.session.container.data.media_records[0].owner == "Иванов Иван Иванович"
    # По записи на каждый recovery-keyslot, а не одна: остаток заголовка LUKS
    # уходит под одноразовые коды целиком, и сколько их -- зависит от того,
    # сколько машин разрешил администратор. Считается из той же функции, что и
    # раскладка слотов: зашитое число разошлось бы с ней при первой же правке.
    expected = machine_binding.recovery_keyslots(_StubInstaller.last_config)
    assert len(app.session.container.data.recovery_records) == len(expected)

    # Кода на экране быть не должно. Он выводится из секрета администратора
    # тогда, когда пользователь позвонит, и до этого момента не существует
    # нигде: ни в контейнере, ни здесь. Прежняя приписка «запишите его»
    # выносила содержимое контейнера наружу на каждом собранном носителе.
    message = wizard.message_bar.var.get()
    # Слова общие на обе ветви записи: установка с образа и запись готового
    # дистрибутива дают один и тот же результат -- готовый носитель. Разные
    # слова про одно и то же были наследием трёх мастеров (зона И5).
    assert "Носитель готов" in message
    assert "запишите его" not in message
    assert all(not record.code for record in app.session.container.data.recovery_records)


def test_install_wizard_passes_the_real_device_not_the_form_fields(gui_app, tmp_path, monkeypatch):
    """План записи использует модель и разделы подтверждённого устройства."""
    from portable_os_admin.install.devices import BlockDevice, Partition

    _stub_install(monkeypatch)
    real_device = BlockDevice(
        name="sdb",
        path="/dev/sdb",
        size_bytes=32_000_000_000,
        model="Cruzer Blade",
        removable=True,
        mountpoints=(),
        partitions=(Partition("/dev/sdb1", 32_000_000_000, "ext4", "DATA", ""),),
        diskseq=9, mounts_known=True,
    )

    class FakeLister:
        def list_removable(self):
            return [real_device]

        def describe(self, path):
            return real_device if path == real_device.path else None

    monkeypatch.setattr(device_step_module, "DeviceLister", FakeLister)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_write()
    _pump(app, lambda: not wizard._running)

    assert _StubInstaller.last_device.size_bytes == 32_000_000_000
    log = wizard.log_text.get("1.0", "end")
    # the old partition's own signatures must go before the table is replaced
    assert "wipefs --all --force /dev/sdb1" in log
    assert app.session.container.data.media_records[0].label == "Cruzer Blade"


def test_install_wizard_says_root_is_needed_instead_of_failing_inside_parted(gui_app, tmp_path, monkeypatch):
    """Without this the user fills in every step, confirms an irreversible
    operation, and only then gets a permission error from a tool they never
    invoked -- exactly the "errors in your face" behaviour the wizard exists to
    avoid. Nothing has been written yet at that point, so refusing is free."""
    release_wizard_module = _stub_install(monkeypatch)
    monkeypatch.setattr(release_wizard_module, "is_root", lambda: False)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    # the warning is already on screen when the last step opens...
    assert "root" in wizard.message_bar.var.get()

    # ...and starting anyway is refused without touching the device
    wizard.confirm_device_var.set("/dev/sdb")
    wizard._start_write()
    app.update()
    assert "root" in wizard.message_bar.var.get()
    assert wizard._running is False
    assert app.session.container.data.media_records == []


def test_install_wizard_blocks_mismatched_confirmation(gui_app, tmp_path, monkeypatch):
    _stub_install(monkeypatch)
    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    _fill_install_wizard(app, wizard)

    wizard.confirm_device_var.set("/dev/sdX-wrong")
    wizard._start_write()
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

    _stub_install(monkeypatch)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer Blade",
        removable=True, mountpoints=("/media/user/photos",), diskseq=9, mounts_known=True,
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

    monkeypatch.setattr(device_step_module, "DeviceLister", FakeLister)

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)
    app.show_frame(ReleaseWizardFrame)
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

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    wizard.source_var.set(_throwaway_image(app))
    wizard._go_next()  # image -> configuration
    app.update()

    assert wizard._valid_names == []
    assert wizard.validate_step(1) is False
    assert "конфигурац" in wizard.message_bar.var.get()


def test_the_wizard_can_open_system_settings_to_the_user(gui_app, tmp_path):
    """Разрешённые настройки задаются в мастере и доезжают до конфигурации.

    Поля не было вовсе -- найдено 12.09.2026. Механизм в коде есть с обеих
    сторон, а задать его администратору было нечем: носитель с разрешёнными
    настройками приложением не собирался, и пункт «пять назначений» был
    непроверяем не из-за нехватки прогона.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame
    # Шаг открывается напрямую: проверяется он сам, а не дорога к нему.
    wizard.step_index = wizard.step_titles.index("Режим системы и отзыв")
    wizard._render_step()
    app.update()

    wizard.settings_vars["network"].set(True)
    wizard.settings_vars["keyboard"].set(True)
    wizard.save_step(wizard.step_index)

    assert wizard.config.kiosk.allowed_settings == ["keyboard", "network"]


def test_configs_list_delete(gui_app, tmp_path, monkeypatch):
    app = gui_app
    _open_container(app, tmp_path)
    name = _make_validated_debian_config(app)
    assert app.config_manager.list_configs(app.container) == [name]

    app.show_frame(ConfigsListFrame)
    configs = app._current_frame
    configs.table.selection_set(configs.table.get_children()[0])
    # С 11.09.2026 удаление спрашивает подтверждение. Без подмены модальный
    # диалог под Xvfb ждал бы ответа, которого никто не даст: прогон вставал
    # молча на середине, без единой строки о причине -- так это и выглядело.
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    configs._delete(name)
    app.update()

    assert app.config_manager.list_configs(app.container) == []
    assert configs._names == []


def test_a_configuration_survives_a_refused_confirmation(gui_app, tmp_path, monkeypatch):
    """«Нет» обязано означать «нет».

    Подтверждение, которое спрашивает и удаляет в любом случае, хуже
    отсутствующего: оно приучает нажимать не глядя. Конфигурацию вернуть
    неоткуда -- с 09.09.2026 она живёт только внутри контейнера, файла рядом
    больше нет, -- поэтому проверяется именно отказ, а не согласие.
    """
    app = gui_app
    _open_container(app, tmp_path)
    name = _make_validated_debian_config(app)

    app.show_frame(ConfigsListFrame)
    configs = app._current_frame
    configs.table.selection_set(configs.table.get_children()[0])
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: False)
    configs._delete(name)
    app.update()

    assert app.config_manager.list_configs(app.container) == [name], "удалено вопреки отказу"
    assert configs._names == [name]


def test_configurations_are_marked_against_the_chosen_image(gui_app, tmp_path, monkeypatch):
    """The image is chosen first because it decides what can be installed at
    all. A configuration written for another family cannot be used with it, and
    saying so in the list is more useful than an error after the choice."""
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.gui.screens import release_wizard as release_wizard_module
    from portable_os_admin.install.image_source import ImageInfo

    app = gui_app
    _open_container(app, tmp_path)
    for name, target_os in (("debian-stick", "debian"), ("redos-stick", "redos")):
        config = InstallConfig.create_default(name, target_os)
        app.config_manager.validate(config)
        app.config_manager.save(app.container, config)

    debian_image = ImageInfo(
        path="/images/debian.iso", family="debian", release="trixie",
        architectures=("amd64",), label="Debian 13.6 DVD",
    )
    monkeypatch.setattr(
        release_wizard_module.image_source, "probe", lambda path: debian_image
    )

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    assert wizard.step_titles[0] == "Источник"

    wizard.source_var.set(_throwaway_image(app))
    wizard._examine_image(wizard.source_var.get())
    _pump(app, lambda: wizard.image_info is not None)
    wizard._go_next()
    app.update()

    rows = {
        wizard.config_table.item(r)["values"][0]: wizard.config_table.item(r)
        for r in wizard.config_table.get_children()
    }
    assert rows["debian-stick"]["values"][2] == "да"
    assert "нет" in str(rows["redos-stick"]["values"][2])
    # the unusable one stays visible but marked, rather than vanishing
    assert "unfit" in rows["redos-stick"]["tags"]

    # and choosing it is refused, with the reason
    redos_row = next(
        r for r in wizard.config_table.get_children()
        if wizard.config_table.item(r)["values"][0] == "redos-stick"
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


def test_state_check_reports_instead_of_crashing(gui_app, tmp_path, monkeypatch):
    """Regression: the button raised instead of answering.

    `_check_leftovers` called `self.message_bar`, `leftovers` и `messagebox` --
    ничего из этого экран не создавал и не импортировал. Единственный
    элемент управления, который отвечает администратору, осталась ли после
    прерванной установки смонтированная файловая система и открытый том,
    падал с AttributeError при нажатии -- ровно тогда, когда он нужнее всего.

    Проверка переехала с главного экрана в раздел «Обслуживание» вместе с
    остальными действиями над самим контейнером, но отвечать она обязана
    по-прежнему.
    """
    from portable_os_admin.gui.screens import maintenance as maintenance_module
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(MaintenanceFrame)
    app.update()
    frame = app._current_frame

    monkeypatch.setattr(maintenance_module.leftovers, "find", lambda: None)
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
    # Флажок живёт на шаге киоска и появляется только когда шаг построен.
    _advance_to(app, wizard, "Режим системы и отзыв")
    wizard.allow_media_var.set(True)
    _advance_to(app, wizard, "Проверка и сохранение")

    changes = dict(
        (label, (before, after)) for label, before, after in wizard.config.diff_from(wizard._baseline)
    )
    assert changes["Предустановленные пакеты"] == ("(пусто)", "mate-calc")
    assert changes["Съёмные носители"] == ("нет", "да")
    assert changes["Архитектура носителя"] == ("(пусто)", "amd64")


def test_building_a_distribution_is_reachable_and_asks_for_no_medium(gui_app, tmp_path):
    """The two halves are separate screens, not modes of one.

    Building erases nothing and needs no device, so the screen must not ask
    for one -- no device step, no confirmation, no passphrase. That absence is
    the point: it is what makes this operation safe to run at any time.
    """
    from portable_os_admin.gui.screens.release_wizard import (
        TARGET_DISTRIBUTION,
        ReleaseWizardFrame,
    )

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame
    assert isinstance(wizard, ReleaseWizardFrame)

    # Ветвь выбирается так же, как её выбирает человек: образ на первом шаге и
    # «собрать дистрибутив» рядом с ним.
    wizard.source_var.set(_throwaway_image(app))
    wizard.target_var.set(TARGET_DISTRIBUTION)
    wizard._go_next()
    app.update()

    titles = " ".join(wizard.step_titles).lower()
    for medium_word in ("носитель", "пароль", "подтверждение"):
        assert medium_word not in titles, f"в сборке дистрибутива спрашивается «{medium_word}»"
    assert "сборка" in titles


# ----------------------------------------------------------------------
# Развёртывание дистрибутива на носитель
# ----------------------------------------------------------------------
def _write_distribution(directory, secret=None, **overrides):
    """Каталог, который мастер развёртывания должен принять за дистрибутив.

    `secret` -- секрет контейнера, которым дистрибутив подписывается, как это
    делает сборка. Без подписи мастер спрашивает согласия модальным окном, и
    тест под Xvfb висит на нём вечно: ответить некому (найдено 15.09.2026 --
    полный прогон простоял двенадцать минут).
    """
    from portable_os_admin.install import distribution

    directory.mkdir(parents=True, exist_ok=True)
    config = InstallConfig.create_baseline("kiosk-stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    config.packages = ["mate-calc"]
    config.kiosk = KioskConfig(enabled=True, desktop_environment="mate")
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive, only its name matters here")
    metadata = distribution.DistributionMetadata.for_config(
        config, release="8.0", arch="x86_64", rootfs_bytes=3 * 1024**3, emulated=True
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    for key, value in overrides.items():
        setattr(metadata, key, value)
    distribution.write_metadata(str(directory), metadata)
    if secret is not None:
        distribution.sign_distribution(str(directory), secret)
    return metadata


def _deploy_wizard(app, tmp_path):
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    return app._current_frame


def test_an_unsigned_distribution_is_refused(gui_app, tmp_path):
    wizard = _deploy_wizard(gui_app, tmp_path)
    _write_distribution(tmp_path / "dist")
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    assert wizard.validate_step(0) is False


def test_a_broken_signature_is_refused_without_asking(gui_app, tmp_path, monkeypatch):
    """Испорченная подпись -- не вопрос к человеку, а отказ."""
    import json

    from portable_os_admin.install import distribution

    monkeypatch.setattr(
        messagebox, "askyesno",
        lambda *a, **k: pytest.fail("испорченную подпись предложили принять"),
    )
    wizard = _deploy_wizard(gui_app, tmp_path)
    _write_distribution(tmp_path / "dist", secret=gui_app.container.admin_secret)
    described = tmp_path / "dist" / distribution.METADATA_NAME
    data = json.loads(described.read_text(encoding="utf-8"))
    data["name"] = "not-what-was-signed"
    described.write_text(json.dumps(data), encoding="utf-8")

    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    assert wizard.validate_step(0) is False


def test_deploy_wizard_reads_the_distribution_before_asking_for_a_medium(gui_app, tmp_path):
    """Отказ должен случиться до того, как выбран носитель.

    Порядок шагов здесь и есть защита: каталог, который не дистрибутив,
    отвергается на первом шаге, когда флешку ещё даже не спрашивали.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame
    assert isinstance(wizard, ReleaseWizardFrame)
    # Первый шаг спрашивает источник, и он же решает путь выпуска. Пока
    # источник не прочитан, носителя в плане нет вовсе -- это и есть защита:
    # каталог, который не дистрибутив, отвергается раньше, чем спросят флешку.
    #
    # Проверяется именно здесь, до выбора: после того как дистрибутив принят,
    # носитель в плане появляется законно, и та же проверка ниже отрицала бы
    # правильное поведение.
    assert wizard.step_titles[0] == "Источник"
    assert "носитель" not in " ".join(wizard.step_titles).lower()
    assert wizard.metadata is None

    wizard.source_var.set(str(tmp_path / "пусто"))
    (tmp_path / "пусто").mkdir()
    wizard.save_step(0)
    assert wizard.validate_step(0) is False
    assert wizard.metadata is None

    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    assert wizard.validate_step(0) is True
    assert wizard.metadata.target_os == "redos"


def test_deploy_wizard_shows_what_it_is_about_to_write(gui_app, tmp_path):
    """Перед стиранием видно, что именно ляжет на носитель.

    Контрольная сумма показывается целиком: её сверяют глазами с файлом рядом
    с архивом. Пометка об эмуляции едет вместе с дистрибутивом, потому что
    забыть про неё — значит выдать носитель, ни разу не выполнявшийся на
    настоящем процессоре своей архитектуры.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    metadata = _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    wizard.validate_step(0)
    wizard.device_path = "/dev/sdz"
    wizard.passphrase = "CorrectHorse123"
    # По названию, а не по номеру: шаг «Кому и что» вставлен между паролем и
    # подтверждением, и `step_index = 3` с тех пор открывал не тот шаг --
    # молча, потому что рисуется он так же.
    wizard.step_index = wizard.step_titles.index("Подтверждение и запись")
    wizard._render_step()
    app.update()

    shown = _visible_text(wizard)
    # Показана целиком, но разбита пополам — иначе строка без пробелов уезжает
    # за край окна.
    assert metadata.sha256[:32] in shown and metadata.sha256[32:] in shown
    assert "redos 8.0" in shown
    assert "mate" in shown
    assert "/dev/sdz" in shown
    assert "эмуляц" in shown


def test_deploy_refuses_when_the_device_confirmation_does_not_match(gui_app, tmp_path):
    """Последняя защита от записи не на тот диск, и она проверяется до root."""
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    wizard.validate_step(0)
    wizard.device_path = "/dev/sdz"
    wizard.passphrase = "CorrectHorse123"
    # По названию, а не по номеру: шаг «Кому и что» вставлен между паролем и
    # подтверждением, и `step_index = 3` с тех пор открывал не тот шаг --
    # молча, потому что рисуется он так же.
    wizard.step_index = wizard.step_titles.index("Подтверждение и запись")
    wizard._render_step()
    app.update()

    wizard.confirm_device_var.set("/dev/sda")
    wizard._start_write()
    app.update()

    assert wizard._running is False
    assert "не совпадает" in wizard.message_bar.var.get()


def test_dashboard_offers_both_halves_and_fits_its_buttons(gui_app, tmp_path):
    """Обе половины разделённой установки доступны с панели.

    И обе кнопки должны быть видны: ряд из семи кнопок в одну строку не
    помещался — «Восстановление» обрезалось, «Состояние» уезжало за край.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.update()
    dashboard = app._current_frame

    shown = _visible_text(dashboard)
    # Одна кнопка вместо трёх: путь выпуска выводится из источника, а не
    # выбирается кнопкой до начала работы (зона И5).
    assert "Выпустить носитель" in shown

    app.update_idletasks()
    # Минимальная ширина окна, а не текущая: пользователь вправе сжать окно до
    # неё, и ряд кнопок обязан помещаться и тогда. В тестовой среде окно к тому
    # же не отображается, и winfo_width() вернул бы единицу.
    minimum_width = app.minsize()[0]
    rows: dict = {}

    def collect(widget):
        for child in widget.winfo_children():
            if isinstance(child, ttk.Button):
                rows.setdefault(str(child.winfo_parent()), []).append(child)
            collect(child)

    collect(dashboard)
    assert rows, "на панели не осталось кнопок — тест потерял смысл"
    for parent, buttons in rows.items():
        needed = sum(b.winfo_reqwidth() for b in buttons) + theme.SPACE_SM * len(buttons)
        assert needed <= minimum_width - 2 * theme.SPACE_LG, (
            "ряд кнопок не помещается в окно минимальной ширины: "
            + ", ".join(str(b.cget("text")) for b in buttons)
        )


def test_a_long_write_can_be_aborted_from_the_interface(gui_app, tmp_path, monkeypatch):
    """Прервать многочасовую запись должно быть чем.

    Пользователь заметил: во время записи заблокированы все кнопки, включая
    «Отмену». Блокировка навигации осмысленна — уход с экрана уничтожит
    виджеты, и код восстановления от уже записанного носителя не попадёт в
    контейнер. Но вместе с ней отключалась и единственная кнопка, которой
    можно было остановить работу: оставалось закрыть программу целиком.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    wizard.validate_step(0)
    wizard.device_path = "/dev/sdz"
    wizard.passphrase = "CorrectHorse123"
    # По названию, а не по номеру: шаг «Кому и что» вставлен между паролем и
    # подтверждением, и `step_index = 3` с тех пор открывал не тот шаг --
    # молча, потому что рисуется он так же.
    wizard.step_index = wizard.step_titles.index("Подтверждение и запись")
    wizard._render_step()
    app.update()

    aborted = []

    class FakeRelease:
        """Мастер держит службу выпуска, а не установщик (зона И3)."""

        installer = None

        def abort(self):
            aborted.append(True)

    wizard._release = FakeRelease()
    wizard._running = True
    wizard.cancel_btn.configure(text="Прервать запись", command=wizard._abort)

    wizard._abort()
    app.update()

    assert aborted, "кнопка не останавливает работу"
    assert "рерыв" in wizard.message_bar.var.get()


def test_an_abort_is_reported_as_a_decision_not_a_failure(gui_app, tmp_path):
    """«Ошибка» там, где человек сам нажал «Прервать», сбивает с толку."""
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame
    from portable_os_admin.install.command_runner import InstallAborted

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    wizard.validate_step(0)
    wizard.device_path = "/dev/sdz"
    wizard.passphrase = "CorrectHorse123"
    # По названию, а не по номеру: шаг «Кому и что» вставлен между паролем и
    # подтверждением, и `step_index = 3` с тех пор открывал не тот шаг --
    # молча, потому что рисуется он так же.
    wizard.step_index = wizard.step_titles.index("Подтверждение и запись")
    wizard._render_step()
    app.update()

    wizard._write_failed(InstallAborted("работа прервана администратором"))
    app.update()

    shown = wizard.message_bar.var.get()
    assert "рервана" in shown
    assert "Непредвиденная ошибка" not in shown


def test_localisation_is_editable_in_the_wizard_not_only_in_json(gui_app, tmp_path):
    """Поле, доступное только правкой JSON, для администратора не существует.

    Язык, раскладка, часовой пояс и имя в сети — то, что меняют в первую
    очередь, и заставлять ради этого редактировать файл значит признать, что
    мастер неполон.
    """
    from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame
    assert isinstance(wizard, ConfigWizardFrame)

    wizard.name_var.set("kiosk-ru")
    wizard.hostname_var.set("kiosk-07")
    wizard.locale_var.set("ru_RU.UTF-8")
    wizard.keymap_var.set("ru")
    wizard.x11_layouts_var.set("us,ru")
    wizard.timezone_var.set("Europe/Moscow")
    wizard.save_step(0)

    assert wizard.config.hostname == "kiosk-07"
    assert wizard.config.localisation.locale == "ru_RU.UTF-8"
    assert wizard.config.localisation.keymap == "ru"
    assert wizard.config.localisation.x11_layouts == "us,ru"
    assert wizard.config.localisation.timezone == "Europe/Moscow"


def test_an_empty_hostname_does_not_produce_a_nameless_system(gui_app, tmp_path):
    """Пустое поле — не повод оставить систему без имени."""
    from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    app.update()
    wizard = app._current_frame

    wizard.name_var.set("stick")
    wizard.hostname_var.set("   ")
    wizard.save_step(0)

    assert wizard.config.hostname == "portable-os"


# ----------------------------------------------------------------------
# Свои пакеты администратора: путь от кнопки до сборки
# ----------------------------------------------------------------------
def _build_wizard_with_config(app, tmp_path, target_os="redos"):
    """Единый мастер в ветви тиража, с уже выбранной проверенной конфигурацией.

    Ветвь выбирается не подстановкой поля в обход мастера, а так же, как её
    выбирает человек: образ на первом шаге плюс «собрать дистрибутив». Иначе
    тест проверял бы состояние, в которое интерфейс не приводит.
    """
    from portable_os_admin.gui.screens.release_wizard import (
        TARGET_DISTRIBUTION,
        ReleaseWizardFrame,
    )

    config = app.config_manager.create(app.container, "stick1", target_os)
    config.validation_status = "valid"
    app.config_manager.save(app.container, config)

    app.show_frame(ReleaseWizardFrame)
    app.update()
    frame = app._current_frame
    frame.source_var.set(_throwaway_image(app))
    frame.target_var.set(TARGET_DISTRIBUTION)
    frame._go_next()
    app.update()
    frame.selected_config_name = config.name
    return frame


def test_the_wizard_has_a_step_for_the_administrators_own_packages(gui_app, tmp_path):
    """Ради этого шага инструмент во многом и существует.

    Клиентов VPN и удалённого рабочего стола в базовой системе нет намеренно:
    у каждой организации свой шлюз и свой клиент, и выбор за администратора не
    сделать. Последний шаг до готового рабочего места он делает сам, своими
    `.deb` и `.rpm`, — а сделать это через интерфейс было нельзя:
    `build_distribution` принимал `extra_packages` с самого начала, и ни одна
    кнопка их не заполняла. По правилу этого проекта то, чего нет в
    интерфейсе, не считается сделанным.
    """
    app = gui_app
    _open_container(app, tmp_path)
    frame = _build_wizard_with_config(app, tmp_path)
    assert "Свои пакеты" in frame.step_titles


def test_a_package_for_the_wrong_family_is_refused_at_once(gui_app, tmp_path):
    """`.deb` для РЕД ОС отвергается при выборе, а не через полчаса сборки.

    Проверка была и раньше, но только в установщике: узнать об ошибке
    администратор мог лишь после распаковки образа. Расширение берётся из
    `OSInstaller.PACKAGE_SUFFIX`, а не пишется в интерфейсе второй раз —
    разойдись эти списки, и мастер принимал бы файл, который сборка отвергнет.
    """
    app = gui_app
    _open_container(app, tmp_path)
    frame = _build_wizard_with_config(app, tmp_path)
    frame.step_index = frame.step_titles.index("Свои пакеты")
    frame._render_step()
    app.update()

    assert frame._package_suffix() == ".rpm"

    wrong = tmp_path / "rudesktop.deb"
    wrong.write_bytes(b"not a real package")
    right = tmp_path / "rudesktop.rpm"
    right.write_bytes(b"not a real package either")

    from portable_os_admin.gui.screens import release_wizard as module

    module.filedialog.askopenfilenames = lambda **kwargs: (str(wrong), str(right))
    frame._browse_packages()
    app.update()

    assert frame.extra_packages == [str(right)], "принят пакет чужого семейства"


def test_the_chosen_packages_reach_the_build(gui_app, tmp_path, monkeypatch):
    """То, что выбрано кнопкой, доезжает до `build_distribution`."""
    app = gui_app
    _open_container(app, tmp_path)
    frame = _build_wizard_with_config(app, tmp_path)

    captured = {}

    class _FakeInstaller:
        PACKAGE_SUFFIX = {"debian": ".deb", "redos": ".rpm"}

        def __init__(self, session):
            pass

        def build_distribution(self, config, destination, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("дальше не идём: проверяется только передача")

    package = tmp_path / "rudesktop.rpm"
    package.write_bytes(b"not a real package")
    frame.extra_packages = [str(package)]
    frame.image_path = str(tmp_path / "image.iso")
    frame.destination = str(tmp_path)

    from portable_os_admin.admin import media_release as module

    # Установщик создаёт служба выпуска, а не мастер: подменять надо её (зона И3).
    monkeypatch.setattr(module, "OSInstaller", _FakeInstaller)
    frame.step_index = frame.step_titles.index("Сборка")
    frame._render_step()
    app.update()
    frame._start_build()
    app.update()

    assert captured.get("extra_packages") == [str(package)], (
        "выбранные пакеты не доехали до сборки"
    )


def test_the_summary_names_the_packages_before_anything_starts(gui_app, tmp_path):
    """Последний экран перечисляет их: сборка идёт четверть часа."""
    app = gui_app
    _open_container(app, tmp_path)
    frame = _build_wizard_with_config(app, tmp_path)

    package = tmp_path / "rudesktop.rpm"
    package.write_bytes(b"not a real package")
    frame.extra_packages = [str(package)]
    frame.image_path = str(tmp_path / "image.iso")
    frame.destination = str(tmp_path)

    frame.step_index = frame.step_titles.index("Сборка")
    frame._render_step()
    app.update()

    assert "rudesktop.rpm" in _visible_text(frame)


def test_a_successful_choice_takes_down_the_previous_refusal(gui_app, tmp_path):
    """Список пополнился — значит красного отказа под ним быть не должно.

    Замечено прогоном через интерфейс 01.09.2026: после отказа «Нужен .rpm»
    администратор выбирал правильный файл, тот появлялся в списке, а сообщение
    оставалось висеть. Экран противоречил сам себе.
    """
    app = gui_app
    _open_container(app, tmp_path)
    frame = _build_wizard_with_config(app, tmp_path)
    frame.step_index = frame.step_titles.index("Свои пакеты")
    frame._render_step()
    app.update()

    from portable_os_admin.gui.screens import release_wizard as module

    wrong = tmp_path / "client.deb"
    wrong.write_bytes(b"not a real package")
    module.filedialog.askopenfilenames = lambda **kwargs: (str(wrong),)
    frame._browse_packages()
    app.update()
    assert "Нужен .rpm" in _visible_text(frame)

    right = tmp_path / "client.rpm"
    right.write_bytes(b"not a real package either")
    module.filedialog.askopenfilenames = lambda **kwargs: (str(right),)
    frame._browse_packages()
    app.update()

    assert "Нужен .rpm" not in _visible_text(frame), "отказ пережил удачный выбор"

def test_the_wizard_offers_two_volumes_of_questions():
    """Упрощённый режим -- не «мастер попроще», а ответ на вопрос, от чего
    действительно зависит судьба собираемого образа.

    Программа начинает с минимальной защищённой системы и сама знает, каким
    должен быть шифр, как разбит диск и что удалённый доступ выключен.
    Спрашивать об этом того, кому нужен обычный киоск, значит требовать
    решения там, где оно уже принято и обосновано.
    """
    from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame

    expert = [key for key, _title, _hint in ConfigWizardFrame._expert_plan()]
    assert "remote" not in expert
    assert expert[0] == "basics" and expert[-1] == "review"


def test_the_simple_mode_drops_only_what_the_program_decides_itself():
    """Шаги упрощённого режима -- подмножество экспертных, без перестановок.

    NN/g советует ровно один уровень раскрытия, а не лестницу: экспертный
    режим добавляет, но не переставляет -- иначе переключатель заставлял бы
    заново искать то, что уже было найдено.
    """
    from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame

    expert = [key for key, _title, _hint in ConfigWizardFrame._expert_plan()]

    # step_plan() -- метод экземпляра, но зависит только от self.expert_mode,
    # поэтому проверяется без построения окна: тесты идут и там, где нет X.
    simple = [
        step
        for step in ConfigWizardFrame.step_plan(
            type("Fake", (), {"expert_mode": False, "_expert_plan": staticmethod(
                ConfigWizardFrame._expert_plan)})()
        )
    ]
    simple_keys = [key for key, _title, _hint in simple]

    assert simple_keys == [key for key in expert if key in simple_keys], "порядок шагов сохранён"
    assert "remote" not in simple_keys, "удалённый доступ выключен по умолчанию"
    for required in ("basics", "packages", "profile", "review"):
        assert required in simple_keys, f"без шага {required} носитель не собрать"


def test_the_chosen_mode_survives_a_restart(tmp_path, monkeypatch):
    """Режим, сбрасывающийся при каждом запуске, -- не выбор, а требование."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from portable_os_admin.gui import preferences

    assert preferences.get("config_wizard_expert", False) is False
    preferences.set_value("config_wizard_expert", True)
    assert preferences.get("config_wizard_expert") is True


def test_interface_preferences_survive_a_broken_file(tmp_path, monkeypatch):
    """Испорченный файл настроек не должен мешать программе открыться."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from portable_os_admin.gui import preferences

    path = tmp_path / "portable-os-admin" / "interface.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{это не json", encoding="utf-8")

    assert preferences.load() == {}
    assert preferences.get("config_wizard_expert", False) is False


def test_no_screen_spells_a_colour_or_a_font_by_hand():
    """Один и тот же смысл должен выглядеть одинаково на всех экранах.

    До 04.09.2026 подсказка была то `#555555`, то `#777777`, то `#a0a0a0`, а
    заголовок экрана набирался вручную на половине экранов и на пиксель
    отличался от остальных. Разница не видна на одном экране и заметна, когда
    администратор ходит между ними, -- а чинить её приходится в пятнадцати
    местах, если не запретить писать так вовсе.

    Тема остаётся единственным местом, где цвета и размеры названы: см.
    gui/theme.py и три правила в его заголовке.
    """
    import pathlib
    import re

    screens = pathlib.Path(__file__).resolve().parent.parent / "src/portable_os_admin/gui"
    offenders = []
    for path in sorted(screens.rglob("*.py")):
        if path.name == "theme.py":
            continue  # ему и положено называть цвета
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r'(foreground|background)\s*=\s*"#', line):
                offenders.append(f"{path.name}:{number} цвет вписан вручную")
            if re.search(r'font\s*=\s*\("Tk', line):
                offenders.append(f"{path.name}:{number} шрифт вписан вручную")

    assert not offenders, "мимо темы: " + "; ".join(offenders)


def test_every_wizard_ends_with_a_verb_not_with_ok():
    """Последний шаг называет действие, а не соглашается неизвестно с чем.

    Практика работы с опасными действиями (PatternFly, Smashing) сходится в
    одном: кнопка, завершающая мастер, должна говорить, что произойдёт.
    «Готово» на шаге, где стирается носитель, -- это согласие вслепую.
    """
    from portable_os_admin.gui.screens.config_wizard import ConfigWizardFrame
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    import inspect

    # Мастеров два, а не четыре: три мастера выпуска слились в один (зона И5).
    for wizard in (ConfigWizardFrame, ReleaseWizardFrame):
        source = inspect.getsource(wizard.__init__)
        assert "finish_label" in source, f"{wizard.__name__}: кнопка финала не названа"
        # «Готово» -- то самое слово, которое ничего не говорит.
        assert 'finish_label="Готово"' not in source, (
            f"{wizard.__name__}: кнопка финала должна называть действие"
        )


def test_sections_use_the_shared_header_and_card():
    """Раздел не набирает раскладку сам.

    До 04.09.2026 каждый экран сам ставил заголовок и сам решал, лежит ли
    содержимое на белом поле. Расходилось это не сразу, а по одному пикселю
    за правку, и видно становилось при переходе между разделами: где-то
    отступ 16, где-то 24; где-то серая подпись, где-то чёрная; где-то ряд
    кнопок белой полосой поперёк серого фона.

    Оба помощника живут в gui/widgets.py и меняются вместе.
    """
    import pathlib
    import re

    screens = pathlib.Path("src/portable_os_admin/gui/screens")
    # Журналы, обмен, целостность и отзыв стали вкладками окна свойств
    # (17.09.2026), и своих файлов-экранов у них больше нет. Заголовок и
    # карточку набирают экраны, а вкладки берут отступы у окна свойств.
    sections = (
        "dashboard.py",
        "configs_list.py",
        "recovery.py",
        "maintenance.py",
    )
    offenders = []
    for name in sections:
        text = (screens / name).read_text(encoding="utf-8")
        if "section_header(" not in text:
            offenders.append(f"{name}: заголовок набран вручную")
        if "section_card(" not in text:
            offenders.append(f"{name}: содержимое не на общем поле")
        # Белое поле красит своих детей само. Экран, который называет
        # оконный стиль внутри карточки, ставит серый прямоугольник на белом
        # -- и это видно сразу, в отличие от разъехавшихся отступов.
        for number, line in enumerate(text.splitlines(), start=1):
            if "card" in line and re.search(r'style="Window\.T(Frame|Label)"', line):
                offenders.append(f"{name}:{number} оконный стиль внутри белого поля")

    assert not offenders, "разнобой разделов: " + "; ".join(offenders)


def test_every_table_explains_itself_when_empty():
    """Пустая таблица не молчит.

    Сетка со шапкой и без строк читается как экран, который не загрузился.
    До 04.09.2026 это было сказано словами только на главном экране, и то
    строкой внизу; список конфигураций и список файлов раздела обмена
    молчали.

    Текст обязан говорить, что сделать, а не только что пусто, -- поэтому
    проверяется и наличие заглушки, и то, что в ней есть глагол.

    Задаётся она теперь одним местом -- `empty_text` помощника `data_table`,
    -- потому что и сама таблица собирается им: до 07.09.2026 каждый экран
    строил её сам, и заглушка была одной из вещей, которые при этом легко
    забыть.
    """
    import pathlib
    import re

    screens = pathlib.Path("src/portable_os_admin/gui/screens")
    # Таблица раздела обмена переехала в окно свойств носителя вместе со своим
    # объяснением пустоты (17.09.2026).
    for name in ("dashboard.py", "configs_list.py", "media_properties.py"):
        text = (screens / name).read_text(encoding="utf-8")
        said = re.findall(r'empty_text="(.+?)"', text, re.S)
        assert said, f"{name}: у таблицы нет объяснения пустоты"
        assert all(word.strip() for word in said), f"{name}: объяснение пустоты без текста"


def test_the_empty_table_hint_appears_and_disappears(gui_app, tmp_path):
    """Заглушка живёт по числу строк, а не по случаю.

    Проверяется на живом виджете, а не по исходному тексту: `place` и
    `place_forget` -- то самое место, где легко оставить объяснение висеть
    поверх заполненной таблицы.
    """
    from portable_os_admin.gui.widgets import TablePlaceholder

    app = gui_app
    _open_container(app, tmp_path)
    app.show_dashboard()
    app.update()
    frame = app._current_frame

    assert isinstance(frame.placeholder, TablePlaceholder)
    # Контейнер пуст -- объяснение показано.
    assert frame.placeholder.label.winfo_manager() == "place"

    frame.table.insert("", "end", values=("stick1", "", "", "", "", ""))
    frame.placeholder.update(False)
    app.update()
    assert frame.placeholder.label.winfo_manager() == ""


def test_the_erase_button_stays_disabled_until_the_path_is_repeated(gui_app, tmp_path):
    """Пока путь не повторён верно, стирать нечем.

    Повтор пути к устройству -- последняя защита от записи не на тот диск, и
    до 04.09.2026 он сверялся только при нажатии: кнопка «Стереть носитель и
    записать дистрибутив» всё время была готова сработать, в том числе от
    пробела и Enter -- клавиш, которыми пролистывают мастер.

    Условие доступности надёжнее сверки постфактум: пока набрано не то,
    нажимать нечего, и это видно глазом. Сверка внутри `_start` остаётся --
    она отвечает за случай, когда устройство подменили между набором и
    нажатием, -- и её проверяет соседний тест.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    app.update()
    wizard = app._current_frame

    _write_distribution(tmp_path / "dist", secret=app.container.admin_secret)
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard.save_step(0)
    wizard.validate_step(0)
    wizard.device_path = "/dev/sdz"
    wizard._selected_device = BlockDevice("sdz", "/dev/sdz", 16 * 1024**3, "Test USB", True, (), diskseq=11, mounts_known=True)
    wizard.passphrase = "CorrectHorse123"
    wizard.step_index = wizard.step_titles.index("Подтверждение и запись")
    wizard._render_step()
    app.update()

    assert str(wizard.start_btn["state"]) == "disabled", "шаг открылся с готовой кнопкой"

    wizard.confirm_device_var.set("/dev/sda")
    app.update()
    assert str(wizard.start_btn["state"]) == "disabled", "чужой путь включил кнопку"

    wizard.confirm_device_var.set("/dev/sdz")
    app.update()
    assert str(wizard.start_btn["state"]) == "normal", "верный повтор не включил кнопку"


# ----------------------------------------------------------------------
# Пункт 23: поведение -- таблицы, обход по Tab, фокус
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Задача 32: отзыв носителя
# ----------------------------------------------------------------------
def _issue_a_medium(app, media_id: str = "46aed677"):
    """Носитель, выданный этим контейнером, -- то, что отзывают."""
    from portable_os_admin.container import MediaRecord

    record = MediaRecord(
        media_id=media_id,
        luks_uuid="523e0993-4dac-4eee-9f72-a272ce456d34",
        label="Kingston DT",
        name="Иванов, бухгалтерия",
        config_name="kiosk",
        target_os="debian",
    )
    app.session.container.add_media_record(record)
    return record


def _open_revoke(app, media_id: str = "46aed677"):
    """Отзыв -- вкладка окна свойств носителя, а не экран (17.09.2026).

    Помощник возвращает само окно: вкладка живёт в нём, и проверять её надо
    там, где она открывается у человека — двойным щелчком по строке списка.
    """
    from portable_os_admin.gui.screens.media_properties import open_media_properties

    app.show_dashboard()
    app.update()
    window = open_media_properties(app._current_frame, app, media_id)
    app.update()
    return window


def test_closing_media_properties_unmounts_the_exchange_volume(gui_app, tmp_path):
    from portable_os_admin.gui.screens.media_properties import open_media_properties

    _open_container(gui_app, tmp_path)
    medium = _issue_a_medium(gui_app)
    window = open_media_properties(gui_app, gui_app, medium.media_id)
    closed = []

    class Volume:
        def close(self):
            closed.append(True)

    window._volume = Volume()
    window.destroy()
    gui_app.update()

    assert closed == [True]


def test_the_revoke_screen_does_not_pretend_a_mark_locks_the_medium(gui_app, tmp_path):
    """Без подключённого сервера отзыв остаётся локальной записью."""
    app = gui_app
    _open_container(app, tmp_path)
    _issue_a_medium(app)

    # Отзыв -- вкладка окна свойств, и по умолчанию открыты «Сведения»:
    # чтобы прочитать сказанное на ней, её надо выбрать (17.09.2026).
    window = _open_revoke(app)
    window.notebook.select(window.notebook.tabs()[-1])
    app.update()
    shown = _visible_text(window)

    assert "Сервер не подключён" in shown
    assert "в этом контейнере до публикации" in shown
    assert "доступ носителя может сохраняться" in shown
    # Физическое стирание по-прежнему требует отдельного действия.
    assert "не открыть ничем" in shown


def test_the_revoke_tab_scrolls_so_erasing_is_always_reachable(gui_app, tmp_path):
    """Вкладка отзыва длиннее окна -- и стирание обязано быть досягаемо.

    Живой прогон 15.09.2026: в штатном окне 1040x700 кнопка стирания уходила
    за нижний край, а прокрутки не было. Проверка «ни один элемент не сплющен»
    этого не ловит: кнопка не сплющена, она просто за краем.

    После переселения отзыва во вкладку (17.09.2026) дефект вернулся — я снял
    прокрутку, решив, что вкладка даёт достаточную высоту, — и был пойман
    снимком. Теперь проверяется то же обещание на новом месте: досягаема
    должна быть кнопка стирания, «Назад» во вкладке нет вовсе — окно
    закрывается своей кнопкой в подвале и по Esc.
    """
    from portable_os_admin.gui.widgets import ScrollableArea

    app = gui_app
    _open_container(app, tmp_path)
    _issue_a_medium(app)
    window = _open_revoke(app)
    window.notebook.select(window.notebook.tabs()[-1])
    app.update()

    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)

    scrollers = [w for w in descendants(window) if isinstance(w, ScrollableArea)]
    assert scrollers, "у вкладки отзыва нет прокрутки"
    inside = set(descendants(scrollers[0].content))
    erase = [
        w for w in inside
        if w.winfo_class() == "TButton" and "Стереть" in str(w.cget("text"))
    ]
    assert erase, "кнопка стирания лежит вне прокручиваемой области"


def test_a_mark_without_a_reason_is_refused(gui_app, tmp_path):
    """Через полгода «отозван» без причины не отвечает ни на один вопрос,
    ради которого в этот список смотрят."""
    app = gui_app
    _open_container(app, tmp_path)
    record = _issue_a_medium(app)
    screen = _open_revoke(app)

    screen.reason_var.set("   ")
    screen._mark()
    app.update()

    assert "причину" in screen.message_bar.var.get()
    assert not record.is_revoked, "носитель отозван без причины"


def test_marking_a_medium_revoked_records_the_fact_and_says_what_it_did_not_do(gui_app, tmp_path, monkeypatch):
    app = gui_app
    _open_container(app, tmp_path)
    record = _issue_a_medium(app)
    screen = _open_revoke(app)

    screen.reason_var.set("сотрудник уволен 11.09.2026")
    screen._mark()
    app.update()

    assert record.is_revoked
    assert record.revoked_reason == "сотрудник уволен 11.09.2026"
    # Успех тоже не должен обещать лишнего: сказано ровно то, что случилось.
    assert "записан в контейнер" in screen.message_bar.var.get()
    assert "Отзыв не опубликован" in screen.message_bar.var.get()

    journal = app.session.container.journal_for("46aed677")
    assert [entry.action for entry in journal] == ["media_revoked"]

    # Регистрация на сервере и последний контакт сами по себе
    # не подтверждают, что USB успела исполнить решение.
    from portable_os_admin.admin import revocation_client
    from portable_os_admin.errors import ContainerError
    container = app.session.container
    container.data.revocation_server_url = "https://server.example"
    monkeypatch.setattr(revocation_client, "query_media", lambda *args: revocation_client.MediaStatus(
        "revoked", "2026-09-15T12:00:00+00:00", "1.2.0", "ab12cd34"))
    window = _open_revoke(app)
    _pump(app, lambda: "Сервер принял отзыв" in window.contact_var.get())
    assert "Исполнение носителем не подтверждено" in window.contact_var.get()
    assert window.restore_btn.instate(["disabled"])
    container.data.revocation_server_url = ""
    with pytest.raises(ContainerError, match="перевыпуск"):
        container.restore_media(record.media_id)


def test_a_revoked_medium_stays_in_the_list_and_is_told_apart_at_a_glance(gui_app, tmp_path):
    """Отзыв -- факт учёта, а не удаление истории.

    Строка остаётся, но гаснет и называет своё состояние: список выданных
    носителей не должен редеть оттого, что часть из них отозвана.
    """
    app = gui_app
    _open_container(app, tmp_path)
    _issue_a_medium(app)
    app.session.container.revoke_media("46aed677", "сотрудник уволен")

    app.show_dashboard()
    app.update()
    table = app._current_frame.table

    assert table.exists("46aed677"), "отозванный носитель пропал из списка"
    values = table.item("46aed677", "values")
    # Колонка ищется по имени, а не по номеру: номер сдвигается от каждого
    # нового столбца, и тест ловил бы не то, ради чего написан -- так и вышло
    # 11.09.2026, когда перед «Состоянием» встал «Владелец».
    status_at = list(table["columns"]).index("status")
    assert str(values[status_at]).startswith("отозван"), f"состояние не показано: {values}"
    assert "revoked" in table.item("46aed677", "tags"), "отозванная строка ничем не отличается"


def test_the_integrity_check_says_when_the_medium_is_not_plugged_in(gui_app, tmp_path, monkeypatch):
    """Отсутствие носителя -- не ошибка программы, а сообщение человеку.

    Носитель ищется по UUID своих томов, а не по пути устройства: та же флешка
    на этой машине `/dev/sdb`, на соседней `/dev/sdc`, и ошибка здесь означала
    бы сверку описи одного носителя с загрузчиком другого.
    """
    import pytest as _pytest

    from portable_os_admin.admin import media_detect as media_detect_module
    from portable_os_admin.errors import PortableOSAdminError
    from portable_os_admin.gui.screens.media_properties import open_media_properties
    from portable_os_admin.install import integrity

    app = gui_app
    _open_container(app, tmp_path)
    record = _issue_a_medium(app)
    # Опись и подпись к ней -- иначе проверка остановится раньше, на «описи нет».
    record.boot_manifest = {"esp:EFI/BOOT/BOOTX64.EFI": "0" * 64}
    record.boot_manifest_signature = integrity.sign(
        record.boot_manifest, app.session.container.admin_secret
    )

    # Целостность -- вкладка окна свойств, а не отдельный экран (17.09.2026).
    app.show_dashboard()
    app.update()
    screen = open_media_properties(app._current_frame, app, record.media_id)
    app.update()

    monkeypatch.setattr(media_detect_module, "detect_map", lambda records: {})

    with _pytest.raises(PortableOSAdminError) as caught:
        screen._compare()
    assert "не найден среди подключённых" in str(caught.value)


def test_a_revocation_can_be_undone_from_the_screen(gui_app, tmp_path):
    """Отозвать можно не ту строку, а «уволенный» сотрудник -- вернуться из
    отпуска. Состояние возвращается, история остаётся."""
    app = gui_app
    _open_container(app, tmp_path)
    record = _issue_a_medium(app)
    app.session.container.revoke_media("46aed677", "ошибся строкой")
    screen = _open_revoke(app)

    screen._restore()
    app.update()

    assert not record.is_revoked
    assert [entry.action for entry in app.session.container.journal_for("46aed677")] == [
        "media_restored",
        "media_revoked",
    ]
    # Журнал показан на том же экране: иначе о нём знал бы только контейнер.
    assert len(screen.journal_table.get_children()) == 2


def test_the_erase_button_stays_disabled_until_the_device_is_repeated(gui_app, tmp_path):
    """Та же последняя защита, что стоит перед записью носителя.

    Здесь она нужна не меньше: ошибка в устройстве стоит чужих данных, и
    обратного действия у стирания заголовка нет.
    """
    app = gui_app
    _open_container(app, tmp_path)
    _issue_a_medium(app)
    screen = _open_revoke(app)

    screen.device_var.set("/dev/sdz")
    app.update()
    assert str(screen.erase_btn["state"]) == "disabled", "экран открылся с готовой кнопкой"

    screen.confirm_var.set("/dev/sda")
    app.update()
    assert str(screen.erase_btn["state"]) == "disabled", "чужой путь включил кнопку"

    screen.confirm_var.set("/dev/sdz")
    app.update()
    assert str(screen.erase_btn["state"]) == "normal", "верный повтор не включил кнопку"


def test_every_table_is_built_by_the_same_helper(gui_app):
    """Таблицы собирает `data_table`, а не каждый экран по-своему.

    Та же болезнь, что лечили `section_header` и `section_card`: девять
    таблиц задавали ширины, выравнивание и прокрутку каждая сама, и знание
    расходилось. `minwidth` знала одна из девяти, размер в байтах
    выравнивался вправо в одной из трёх.

    Проверяется по исходному тексту, потому что проверяется именно это --
    что экран не строит таблицу сам.
    """
    import pathlib

    screens = pathlib.Path("src/portable_os_admin/gui/screens")
    offenders = [
        path.name
        for path in screens.glob("*.py")
        if "ttk.Treeview(" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "таблица собрана мимо data_table: " + ", ".join(offenders)


@pytest.mark.skipif(using_gtk(), reason="Tk font metrics")
def test_narrow_columns_keep_a_minimum_and_get_a_scrollbar(gui_app, tmp_path):
    """Узкое окно не съедает колонку до нечитаемого.

    До 04.09.2026 заголовок «Идентификатор» обрезался до «Идентификат», и
    это чинили подбором ширин. Ширина помогает, пока окно не станет уже той,
    под которую подбирали, -- поэтому у каждой колонки есть минимум, а когда
    сумма минимумов не помещается, снизу появляется горизонтальная прокрутка.

    Проверяется на живой таблице: минимум объявлен, полоса создана и
    привязана к `xview` -- то есть прокручивает, а не украшает.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_dashboard()
    app.update()
    table = app._current_frame.table

    # Минимум меряется не на глаз, а шрифтом заголовка: ровно этот дефект и
    # ловится -- «Идентификатор» обрезался до «Идентификат». Запас в 16
    # пикселей -- отступы ячейки по обе стороны.
    import tkinter.font as tkfont

    heading_font = tkfont.Font(font=theme.FONT_SECTION)
    for column in table["columns"]:
        title = table.heading(column, "text")
        minimum = int(table.column(column, "minwidth"))
        needed = heading_font.measure(title) + 16
        assert minimum >= needed, (
            f"«{title}»: минимум {minimum} меньше самого заголовка ({needed}) -- он обрежется"
        )

    frame = table.master
    bars = [
        child for child in frame.winfo_children()
        if child.winfo_class() == "TScrollbar" and str(child.cget("orient")) == "horizontal"
    ]
    assert bars, "горизонтальной прокрутки нет -- в узком окне правый край не дочитать"
    assert table.cget("xscrollcommand"), "полоса не связана с таблицей"


def test_numbers_are_aligned_right(gui_app, tmp_path):
    """Числа стоят разрядом под разрядом.

    Левое выравнивание отнимает у столбца чисел единственное, ради чего его
    читают столбиком, -- возможность сравнить размеры взглядом, не читая
    каждую цифру.
    """
    app = gui_app
    _open_container(app, tmp_path)
    # Обмен открывается так, как до него доходит человек: действием над
    # выбранным носителем. Разделом слева он быть перестал (зона И4), и тест,
    # открывающий его прежним путём, остался бы зелёным, проверяя дорогу,
    # которой больше не ходят.
    _issue_a_medium(app)
    dashboard = app._current_frame
    dashboard._refresh_table()
    dashboard.table.selection_set("46aed677")
    # Раздел обмена стал вкладкой окна свойств носителя (17.09.2026): выбор
    # передаётся тем, кто окно открыл, а само окно открывается двойным щелчком
    # по строке — так, как до него доходит человек.
    from portable_os_admin.gui.screens.media_properties import open_media_properties

    # Вкладка «Обмен» есть только у носителей прежней разметки (с томом
    # обмена); разметка v3 его не имеет. Таблица с размерами -- там.
    for record in app.session.container.data.media_records:
        if record.media_id.endswith("46aed677"):
            record.exchange_luks_uuid = "legacy-exchange"
    window = open_media_properties(dashboard, app, "46aed677")
    app.update()
    table = window.files_table

    # str(): Tk возвращает выравнивание объектом-индексом, а не строкой.
    assert str(table.column("size", "anchor")) == "e", "размер выровнен не по правому краю"
    assert str(table.heading("size", "anchor")) == "e", (
        "заголовок числовой колонки смотрит в другую сторону"
    )
    assert str(table.column("name", "anchor")) == "w", "текст должен остаться слева"


def test_a_focused_table_shows_where_the_focus_is(gui_app, tmp_path):
    """Таблица, получившая фокус, выделяет первую строку.

    ttk не рисует фокус у `Treeview` вовсе -- в его раскладке нет элемента
    фокуса, проверено на живом Tk. Пользователь, дошедший до таблицы табом,
    не видел, где он, и стрелки казались неработающими.

    Выделение не навязывается поверх выбранного: если строка уже выбрана,
    приход фокуса ничего не меняет.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_dashboard()
    app.update()
    table = app._current_frame.table
    table.insert("", "end", values=("первый", "", "aaaa", "", "", ""))
    second = table.insert("", "end", values=("второй", "", "bbbb", "", "", ""))

    table.event_generate("<FocusIn>")
    app.update()
    assert table.selection() == (table.get_children()[0],), "фокус не виден: строка не выделена"

    table.selection_set(second)
    table.event_generate("<FocusIn>")
    app.update()
    assert table.selection() == (second,), "приход фокуса переставил выбор пользователя"


def test_the_wizard_footer_comes_after_the_step_in_tab_order(gui_app, tmp_path):
    """Tab идёт по содержимому шага, а потом по кнопкам, а не наоборот.

    Подвал создаётся раньше содержимого намеренно -- иначе слишком высокий
    шаг выталкивает кнопки за нижний край, -- а порядок обхода в Tk это
    порядок виджетов у родителя. Из-за этого первым, что получало фокус в
    мастере, была «Отмена».

    Внутри подвала порядок тоже свой: «Далее» упакована правее и создана
    раньше «Назад», поэтому её поднимают в конец -- чтобы Tab шёл так, как
    читают, слева направо.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    app.update()

    order = wizard.winfo_children()
    body_area = wizard._scroller.master.master
    assert order.index(wizard._footer) > order.index(body_area), (
        "подвал обходится раньше содержимого шага"
    )
    footer_order = wizard._footer.winfo_children()
    assert footer_order.index(wizard.next_btn) > footer_order.index(wizard.back_btn), (
        "«Далее» обходится раньше «Назад»"
    )


def test_the_wizard_opens_with_focus_off_the_buttons(gui_app, tmp_path):
    """Фокус при открытии шага стоит на содержимом, а не на кнопке.

    Мастер пролистывают пробелом и Enter, и это делает важным, на чём стоит
    фокус в первый момент: на кнопке подвала случайное нажатие уводит с
    шага, а на последнем шаге запускает работу.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ConfigWizardFrame)
    wizard = app._current_frame
    # Окно показывается намеренно: в спрятанном Tk фокус не назначается
    # вовсе -- виджеты не выведены на экран, и `focus_set` откладывается.
    # Проверять здесь нечего без дисплея, и это ровно тот случай, ради
    # которого прогон идёт под Xvfb.
    app.deiconify()
    app.update()
    _pump(app, lambda: app.focus_get() is not None, timeout=5.0)

    focused = str(app.focus_get())
    assert focused not in (str(wizard.next_btn), str(wizard.cancel_btn)), (
        "фокус на кнопке: пробел уводит с шага, а на последнем шаге -- запускает работу"
    )
    assert focused.startswith(str(wizard.body)), f"фокус вне содержимого шага: {focused}"


@pytest.mark.skipif(using_gtk(), reason="Tk focus style")
def test_the_accent_button_shows_its_focus(gui_app):
    """Рамка фокуса на главной кнопке видима.

    Производные стили наследуют `focuscolor` от `TButton`, а там он равен
    ACCENT -- то есть фону самой акцентной кнопки. Фокус на ней был
    нарисован её же цветом, и пользователь, дошедший до неё табом, этого не
    видел.
    """
    from tkinter import ttk

    from portable_os_admin.gui import theme

    style = ttk.Style()
    assert style.lookup("Accent.TButton", "focuscolor") != style.lookup("Accent.TButton", "background"), (
        "рамка фокуса цвета фона -- то есть её нет"
    )
    assert style.lookup("Accent.TButton", "focuscolor") == theme.TEXT_ON_ACCENT


def test_no_control_is_squashed_at_the_smallest_window(gui_app, tmp_path):
    """Ни одна кнопка не обрезана в окне наименьшего размера.

    Кнопка, которой не хватило места, не сжимается и не сокращает подпись --
    Tk перестаёт её рисовать. Выглядит это как пустой прямоугольник или как
    отсутствующая кнопка, и в этом проекте ловилось уже трижды, каждый раз
    глазами и каждый раз в другом месте.

    Проверка идёт по всем разделам сразу и в том размере, который назначен
    минимальным (900x600): именно там дефект и появляется, а в окне, с
    которым программа открывается, его не видно.

    Найденное этой проверкой 07.09.2026: на главном экране ряд действий над
    носителем сжимался по высоте до 27 пикселей вместо 42 -- кнопки
    рисовались пустыми прямоугольниками; на экране обмена три кнопки
    схлопывались до одного пикселя; «Назад» на экране конфигураций получала
    37 пикселей от нужных 126; «Сохранить в файл…» на экране журналов не
    хватало восьми.
    """
    app = gui_app
    _open_container(app, tmp_path)
    app.geometry("900x600")
    app.deiconify()

    def squashed(widget, found):
        for child in widget.winfo_children():
            # Неразмещённое не меряется.
            #
            # Tk не размещает содержимое неоткрытых вкладок `Notebook`: у него
            # размер 1x1, пока вкладку не открыли. Без этой проверки экран с
            # вкладками объявлялся обрезанным весь целиком («Проверить
            # состояние» 1x1 вместо 189x42), и настоящие обрезания -- ради
            # которых проверка и написана -- утонули бы в этом шуме.
            if not child.winfo_ismapped():
                squashed(child, found)
                continue
            if child.winfo_class() in ("TButton", "TEntry", "TCombobox"):
                if (child.winfo_height() < child.winfo_reqheight()
                        or child.winfo_width() < child.winfo_reqwidth()):
                    label = child.cget("text") if "text" in child.keys() else child.winfo_class()
                    found.append(f"«{label}» {child.winfo_width()}x{child.winfo_height()} "
                                 f"вместо {child.winfo_reqwidth()}x{child.winfo_reqheight()}")
            squashed(child, found)
        return found

    complaints = []
    # Разделы берутся из самой навигации, а не переписываются сюда списком:
    # переписанный список пережил бы правку навигации молча и перестал бы
    # проверять то, что появилось. «Журналы» и «Обмен» разделами быть
    # перестали (зона И4), но их окна проверяются тем же -- они открываются
    # действием над носителем.
    from portable_os_admin.gui.shell import SECTIONS

    # Журналы и обмен разделами быть перестали и экранами тоже: они стали
    # вкладками окна свойств носителя (17.09.2026). Здесь проверяются разделы,
    # а вкладки окна -- своими тестами.
    for section in [key for key, _title, _hint in SECTIONS]:
        app.show_section(section)
        _pump(app, lambda: app._current_frame.winfo_width() > 1, timeout=5.0)
        app.update_idletasks()
        app.update()
        for problem in squashed(app._current_frame, []):
            complaints.append(f"{section}: {problem}")

    assert not complaints, "обрезано в окне 900x600:\n" + "\n".join(complaints)


def test_the_deploy_wizard_remembers_the_distribution_for_the_next_stick(gui_app, tmp_path, monkeypatch):
    """Тираж не начинается с пустого поля на каждой флешке.

    Один и тот же дистрибутив уходит на два десятка носителей, а мастер
    развёртывания начинал с чистого поля каждый раз: путь набирался заново на
    каждую флешку. Найдено проходом по сценариям администратора (пункт 21).

    Запоминается ровно каталог. Пароль и устройство -- нет, и по разным
    причинам: первый секрет и в файле настроек ему не место, второе меняется
    от флешки к флешке, а подставленное устройство здесь означает стёртый не
    тот диск.
    """
    from portable_os_admin.gui import preferences
    from portable_os_admin.gui.screens.release_wizard import (
        SOURCE_PREFERENCE_KEY,
        ReleaseWizardFrame,
    )

    store = {}
    monkeypatch.setattr(preferences, "get", lambda key, default=None: store.get(key, default))
    monkeypatch.setattr(preferences, "set_value", lambda key, value: store.__setitem__(key, value))

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    app.update()

    assert wizard.source_var.get() == "", "мастер начал не с пустого поля"
    wizard.source_var.set(str(tmp_path / "dist"))
    wizard._save_step_source()
    assert store.get(SOURCE_PREFERENCE_KEY) == str(tmp_path / "dist")

    # Следующая флешка: тот же дистрибутив уже подставлен.
    app.show_frame(ReleaseWizardFrame)
    again = app._current_frame
    app.update()
    assert again.source_var.get() == str(tmp_path / "dist"), "путь не запомнился"

    # Пароль -- никогда.
    assert again.passphrase == "", "пароль запомнился между запусками"
    assert not any("passphrase" in key or "пароль" in key for key in store), (
        "секрет попал в файл настроек"
    )


def test_nothing_about_configurations_is_written_next_to_the_container(gui_app, tmp_path):
    """Рядом с контейнером не появляется ни одного файла конфигурации.

    До 09.09.2026 они лежали там открытым текстом — `configs/*.json` рядом с
    зашифрованным контейнером. Паролей в конфигурации нет, но есть полное
    описание защищаемой системы: учётная запись, состав пакетов, что в киоске
    разрешено и что запрещено, разметка, число машин на носитель, зеркала.

    Проверяется через приложение, а не вызовом менеджера: путь от кнопки
    «Создать» до файла на диске и есть то, что разошлось с замыслом.
    """
    app = gui_app
    _open_container(app, tmp_path)

    config = app.config_manager.create(app.container, "secret-stick", "debian")
    config.account.username = "buhgalter"
    app.config_manager.save(app.container, config)
    app.save_container()

    on_disk = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert [p.name for p in on_disk] == ["admin.container"], on_disk
    # И самого имени в открытом виде на диске тоже нет: контейнер зашифрован.
    assert b"buhgalter" not in (tmp_path / "admin.container").read_bytes()
    assert app.config_manager.list_configs(app.container) == ["secret-stick"]


def test_a_configuration_survives_closing_and_reopening_the_container(gui_app, tmp_path):
    """Конфигурация переживает закрытие сеанса — она часть контейнера.

    Пока хранилищем был каталог, это выполнялось само собой. Теперь запись
    должна пройти через шифрование и вернуться обратно, и проверять это надо
    отдельно: забыть `save_container()` после правки — самая дешёвая из
    возможных ошибок.
    """
    app = gui_app
    _open_container(app, tmp_path)
    config = app.config_manager.create(app.container, "stick1", "redos")
    config.packages = ["mate-calc"]
    app.config_manager.save(app.container, config)
    app.save_container()
    app.end_session()

    startup = app._current_frame
    startup.path_var.set(str(tmp_path / "admin.container"))
    startup.password_var.set("CorrectHorse123")
    startup._open()
    app.update()

    assert app.config_manager.list_configs(app.container) == ["stick1"]
    reopened = app.config_manager.open(app.container, "stick1")
    assert reopened.packages == ["mate-calc"]
    assert reopened.target_os == "redos"


def test_configurations_travel_between_administrators_through_the_screen(gui_app, tmp_path, monkeypatch):
    """Путь «отправить — принять» проходится через сам экран, а не через модуль.

    Конфигурации живут в контейнере, поэтому отдать одну коллеге можно только
    посылкой: зашифрованной его кодом и подписанной своим. Проверяется весь
    путь целиком — от кнопки до записи в чужом контейнере, — потому что
    именно на стыке экрана и крипты теряются такие вещи, как «забыл сохранить
    контейнер после приёма».
    """
    from portable_os_admin.config import exchange
    from portable_os_admin.container import ContainerManager

    app = gui_app
    _open_container(app, tmp_path)
    config = app.config_manager.create(app.container, "stick1", "debian")
    config.packages = ["mate-calc"]
    app.config_manager.save(app.container, config)
    app.save_container()

    # Второй администратор — отдельный контейнер со своим паролем.
    colleague_manager = ContainerManager()
    colleague = colleague_manager.create(tmp_path / "colleague.container", "CorrectHorse456")
    # Код заводит семя в контейнере, и его надо сохранить: иначе при
    # следующем открытии файла семя окажется другим, а посылка -- не для
    # этого контейнера. В приложении это делает кнопка «Мой код обмена…».
    colleague_code = exchange.identity_of(colleague).code
    colleague_manager.save(colleague)
    parcel = tmp_path / ("parcel" + exchange.EXCHANGE_SUFFIX)

    # Обмен переехал в окно поверх списка (17.09.2026): на списке конфигураций
    # он занимал четыре кнопки из девяти и стоял рядом с «Удалить», то есть
    # предлагался как равный ей по последствиям. Выбранная строка уезжает в
    # окно вместе с открытием, поэтому выбирать заново не приходится.
    from portable_os_admin.gui.screens.configs_share import open_configs_share

    app.show_section("configs")
    app.update()
    configs = app._current_frame
    configs.table.selection_set(configs.table.get_children()[0])
    screen = open_configs_share(configs, app, "stick1")
    app.update()
    assert screen.config_name == "stick1", "выбор не доехал до окна обмена"
    monkeypatch.setattr(
        simpledialog, "askstring",
        lambda *a, **k: colleague_code,
    )
    monkeypatch.setattr(filedialog, "asksaveasfilename", lambda *a, **k: str(parcel))
    screen._send_selected()
    app.update()

    assert parcel.exists(), screen.message_bar.var.get()
    # В самой посылке ничего не читается открытым текстом.
    assert b"mate-calc" not in parcel.read_bytes()

    # Приём — со стороны коллеги: тот же экран, другой контейнер.
    app.end_session()
    startup = app._current_frame
    startup.path_var.set(str(tmp_path / "colleague.container"))
    startup.password_var.set("CorrectHorse456")
    startup._open()
    app.update()
    from portable_os_admin.gui.screens.configs_share import open_configs_share

    app.show_section("configs")
    app.update()
    receiver = open_configs_share(app._current_frame, app)
    monkeypatch.setattr(filedialog, "askopenfilename", lambda *a, **k: str(parcel))
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    receiver._receive()
    app.update()

    assert app.config_manager.list_configs(app.container) == ["stick1"]
    received = app.config_manager.open(app.container, "stick1")
    assert received.packages == ["mate-calc"]

    # И то же самое — на диске, до закрытия сеанса. Закрытие сеанса
    # сохраняет контейнер само, поэтому проверять после него бесполезно:
    # «забыл сохранить после приёма» так и осталось бы незамеченным до
    # первого падения программы с непринятой посылкой.
    on_disk = ContainerManager().open(tmp_path / "colleague.container", "CorrectHorse456")
    assert [record.name for record in on_disk.list_configs()] == ["stick1"]
    on_disk.close()
    colleague.close()


def test_the_build_wizard_offers_a_place_to_put_the_distribution(gui_app, tmp_path):
    """Шаг «Куда сохранить» предлагает каталог, который существует.

    Умолчанием был `configs/` рядом с контейнером — каталог, которого после
    переезда конфигураций внутрь контейнера может не быть вовсе. Мастер тогда
    падал на построении шага: `configs_dir` исчез вместе с ним.
    """
    from portable_os_admin.gui.screens.release_wizard import ReleaseWizardFrame

    app = gui_app
    _open_container(app, tmp_path)
    _make_validated_debian_config(app)

    app.show_frame(ReleaseWizardFrame)
    wizard = app._current_frame
    app.update()
    wizard.source_var.set(_throwaway_image(app))
    # Каталог для дистрибутива спрашивается только в ветви тиража.
    from portable_os_admin.gui.screens.release_wizard import TARGET_DISTRIBUTION

    wizard.target_var.set(TARGET_DISTRIBUTION)
    wizard._go_next()
    app.update()
    wizard.config_table.selection_set(wizard.config_table.get_children()[0])
    wizard._go_next()
    app.update()
    while wizard.step_titles[wizard.step_index] != "Куда сохранить":
        wizard._go_next()
        app.update()

    import pathlib

    offered = wizard.destination_var.get()
    assert offered, "мастер не предлагает никакого каталога"
    assert pathlib.Path(offered).is_dir(), f"предложенного каталога не существует: {offered}"


def test_the_maintenance_screen_says_when_no_revocation_resource_is_known(gui_app, tmp_path):
    """Пустая карточка молчала бы о главном: носители, выпущенные без ресурса,
    нельзя отозвать удалённо, и узнать об этом надо до выпуска, а не после."""
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(MaintenanceFrame)
    app.update()
    frame = app._current_frame

    assert "Не подключён" in frame.revocation_state.cget("text")


def test_a_plain_http_resource_is_refused_before_the_container_says_anything(gui_app, tmp_path):
    """По открытому каналу видно, какие носители у организации есть и когда
    они включаются, — поэтому адрес проверяется до всякого обращения."""
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(MaintenanceFrame)
    app.update()
    frame = app._current_frame

    frame.revocation_url.set("http://revoke.example.org")
    frame._connect_revocation()
    app.update()

    assert "https" in frame.message_bar.var.get()
    assert not app.session.container.data.revocation_server_url


def test_a_table_inside_a_wizard_keeps_more_than_one_row_visible(gui_app):
    """Одна видимая строка прячет всё остальное под прокруткой.

    Так «пропала» сохранённая конфигурация 14.09.2026: в разделе
    «Конфигурации» она была, в мастере сборки -- нет. Растяжение само по себе
    высоты не добавляет: внутри прокручиваемого шага мастера у таблицы нет
    соперников за место, и запрошенная строка так строкой и остаётся.
    """
    from portable_os_admin.gui.widgets import Column, data_table

    holder = ttk.Frame(gui_app)
    columns = (Column("a", "Колонка", 120, 80),)

    default_table, _ = data_table(holder, columns)
    asked_table, _ = data_table(holder, columns, min_rows=5)

    # Умолчание не трогаем: оно защищает кнопки под таблицей на окне 900x600.
    assert int(default_table.cget("height")) == 1
    assert int(asked_table.cget("height")) == 5


def test_access_tab_issues_a_code_and_saves_workplaces(gui_app, tmp_path, monkeypatch):
    """Вкладка «Доступ» (Э7): код активации показывается, рабочие места уходят
    на сервер тем списком, что видит администратор."""
    from portable_os_admin.admin import access_actions
    from portable_os_admin.gui.screens.media_properties import open_media_properties

    app = gui_app
    _open_container(app, tmp_path)
    record = _issue_a_medium(app)
    app.session.container.data.revocation_server_url = "https://access.example.org"
    saved = {}
    monkeypatch.setattr(access_actions, "get_targets", lambda c, m: [
        {"id": "rdp-1", "name": "РМ", "type": "rdp", "host": "10.0.0.5", "port": 3389,
         "user": "u", "domain": "", "pin": "sha256:" + "a" * 64, "smartcard": False}])
    monkeypatch.setattr(access_actions, "issue_activation", lambda c, m: "ABCDE-FGHJK")
    monkeypatch.setattr(access_actions, "set_targets",
                        lambda c, m, t: saved.setdefault("targets", t) and len(t))

    app.show_dashboard()
    app.update()
    window = open_media_properties(app._current_frame, app, record.media_id)
    tab = window._access
    for _ in range(100):
        app.update()
        time.sleep(0.02)
        if tab.targets:
            break
    assert [t["host"] for t in tab.targets] == ["10.0.0.5"]

    tab._issue()
    for _ in range(100):
        app.update()
        time.sleep(0.02)
        if tab.code_var.get():
            break
    assert tab.code_var.get() == "ABCDE-FGHJK"

    tab._accept({"id": "rdp-2", "name": "Сервер", "type": "rdp", "host": "10.0.0.9", "port": 3389,
                 "user": "root", "domain": "", "pin": "sha256:" + "b" * 64, "smartcard": False})
    tab._save()
    for _ in range(100):
        app.update()
        time.sleep(0.02)
        if saved:
            break
    assert [t["id"] for t in saved["targets"]] == ["rdp-1", "rdp-2"]
    window.destroy()


def test_the_org_root_tab_creates_a_reserve_root_and_certifies_the_container(gui_app, tmp_path, monkeypatch):
    """Через окно, а не API: резервный корень, фраза один раз, сертификат
    этому контейнеру -- как это сделает администратор (Э18)."""
    from portable_os_admin.admin import org_root
    from portable_os_admin.gui.screens import org_root_tab
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    monkeypatch.setattr(org_root, "RESERVE_KDF_N", 2 ** 14)
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(MaintenanceFrame)
    app.update()
    tab = app._current_frame.org_root_tab
    root_path = tmp_path / "reserve.poaroot"
    monkeypatch.setattr(org_root_tab.filedialog, "asksaveasfilename", lambda **_: str(root_path))
    shown = {}
    monkeypatch.setattr(org_root_tab, "PhraseDialog",
                        lambda parent, phrase, entry: shown.update(phrase=phrase, entry=entry))
    tab._create()
    for _ in range(200):
        app.update()
        if "phrase" in shown:
            break
        import time
        time.sleep(0.05)
    assert root_path.exists() and len(shown["phrase"]) == org_root.RESERVE_PHRASE_LENGTH
    assert org_root.anchors(app.session.container) == [shown["entry"]]
    assert "disabled" in tab.create_button.state()

    monkeypatch.setattr(org_root_tab.filedialog, "askopenfilename", lambda **_: str(root_path))
    monkeypatch.setattr(org_root_tab, "PhraseEntryDialog",
                        lambda parent, on_accept: on_accept(shown["phrase"]))
    tab._certify_self()
    for _ in range(200):
        app.update()
        if app.session.container.data.working_key_cert_json:
            break
        import time
        time.sleep(0.05)
    assert app.session.container.data.working_key_cert_json
    texts = " ".join(w.cget("text") for w in tab.facts.winfo_children()[0].winfo_children())
    assert "действует до" in texts and org_root.fingerprint(shown["entry"]) in texts


def test_the_releases_tab_shows_the_tree_and_assigns_a_distribution(gui_app, tmp_path, monkeypatch):
    """«Выпуски и группы» (Э7): подгруппа -- под родителем, унаследованное
    назначение видно, выбор в диалоге уходит на сервер."""
    import time

    from portable_os_admin.admin import access_actions
    from portable_os_admin.gui.screens import releases_tab
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    data = {
        "distributions": [{"id": "kiosk", "family": "debian", "arch": "amd64", "layout": 3,
                           "min_serial": 0, "releases": [7, 5]}],
        "groups": [
            {"id": "team", "parent": "dept", "distribution": None, "allow_vm": None,
             "effective": "kiosk", "effective_vm": True},
            {"id": "root", "parent": None, "distribution": "kiosk", "allow_vm": None,
             "effective": "kiosk", "effective_vm": False},
            {"id": "dept", "parent": "root", "distribution": None, "allow_vm": True,
             "effective": "kiosk", "effective_vm": True}],
        "media": [{"media_id": "m1", "group": "team"}],
    }
    calls = []
    monkeypatch.setattr(access_actions, "overview", lambda c: data)
    monkeypatch.setattr(access_actions, "assign", lambda c, g, d: calls.append((g, d)))
    app = gui_app
    _open_container(app, tmp_path)
    app.session.container.data.revocation_server_url = "https://access.example.org"
    app.show_frame(MaintenanceFrame)
    tab = app._current_frame.releases_tab
    for _ in range(100):
        app.update()
        if tab.group_tree.get_children():
            break
        time.sleep(0.02)
    assert list(tab.group_tree.get_children()) == ["root", "dept", "team"]
    team = tab.group_tree.item("team")["values"]
    assert team[3] == "kiosk" and team[4] == "да, насл." and team[5] == 1
    assert tab.dist_tree.item("kiosk")["values"][2] == 7

    tab.group_tree.selection_set("team")
    monkeypatch.setattr(releases_tab, "ChoiceDialog",
                        lambda parent, title, label, values, current, accept: accept(values[0]))
    tab._assign()
    for _ in range(100):
        app.update()
        if calls:
            break
        time.sleep(0.02)
    assert calls == [("team", None)]


def test_the_descriptor_is_published_with_the_listed_nodes(gui_app, tmp_path, monkeypatch):
    """«Ресурс отзыва → Описатель сервера» (Э18): адреса через запятую или
    пробел уходят списком, пустой список не публикуется."""
    import time

    from portable_os_admin.admin import access_actions
    from portable_os_admin.gui.screens.maintenance import MaintenanceFrame

    sent = []
    monkeypatch.setattr(access_actions, "publish_descriptor",
                        lambda c, nodes, fallback: sent.append((nodes, fallback)) or 42)
    app = gui_app
    _open_container(app, tmp_path)
    app.show_frame(MaintenanceFrame)
    frame = app._current_frame
    frame.descriptor_nodes.set("")
    frame._publish_descriptor()
    assert sent == []
    frame.descriptor_nodes.set("https://a.example:8443, https://b.example:8443")
    frame.descriptor_fallback.set("https://mirror.example/poa")
    frame._publish_descriptor()
    for _ in range(100):
        app.update()
        if sent:
            break
        time.sleep(0.02)
    assert sent == [(["https://a.example:8443", "https://b.example:8443"],
                     ["https://mirror.example/poa"])]
