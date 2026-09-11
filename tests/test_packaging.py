"""What the .deb and .rpm must carry to work on any machine of their family.

Both packages were built and installed for real; these guard the things that
break quietly instead of loudly. A data file dropped from the package does not
fail the build -- it fails at the moment a user needs recovery, or needs a
configuration checked, which is far too late to find out.
"""
import importlib.resources
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def test_non_python_files_ship_with_the_package():
    """Neither is a module, so a packaging change can silently leave them out
    and nothing notices until the feature is used.

    Честно об ограничении этой проверки: тесты гоняются на editable-установке
    (`pip install -e`, см. README), а под ней `importlib.resources` читает
    файл прямо из `src/` репозитория в обход какой-либо сборки -- тест пройдёт,
    даже если `pyproject.toml` вообще не упомянет эти файлы в package-data.
    Он ловит только «файл пропал из репозитория», не ошибку упаковки как
    таковую. Настоящая защита от забытого файла для .deb и .rpm -- ниже, в
    test_both_packages_copy_the_whole_source_tree_not_a_file_list: оба вообще
    не читают package-data, а копируют src/portable_os_admin целиком.
    """
    for package, name in (
        ("portable_os_admin.recovery", "recovery_module.sh"),
        # Слой вывода, которым носитель разговаривает с человеком. Появился
        # 29.08.2026 и в перечень package-data не попал: собранный пакет отдал
        # бы носителю модуль восстановления без экрана, и выяснилось бы это на
        # живой загрузке у того, кто остался без системы. Список заменён на
        # `recovery/*.sh` -- перечислять поимённо оказалось нечем помочь.
        ("portable_os_admin.recovery", "ui.sh"),
        ("portable_os_admin.config", "kickstart_runner.py"),
    ):
        assert importlib.resources.files(package).joinpath(name).is_file(), name


def test_both_packages_copy_the_whole_source_tree_not_a_file_list():
    """Настоящая защита от забытого файла в .deb и .rpm.

    `pyproject.toml`'s `package-data` не имеет отношения к тому, что реально
    едет в собранный пакет: и `%install` в спеке, и `override_dh_auto_install`
    в `packaging/debian/rules` копируют `src/portable_os_admin` целиком, одной
    командой, без списка имён или расширений. Забыть файл этим способом
    нельзя -- можно только по ошибке сузить саму команду копирования, и вот
    это здесь и проверяется.
    """
    spec = _read("packaging/rpm/portable-os-admin.spec")
    rules = _read("packaging/debian/rules")
    assert "cp -r src/portable_os_admin %{buildroot}%{python3_sitelib}/" in spec
    assert (
        "cp -r src/portable_os_admin debian/portable-os-admin/usr/lib/python3/dist-packages/"
        in rules
    )


def test_the_launcher_pins_no_python_version():
    """/usr/bin/python3 is 3.9 on an older RHEL and 3.13 on Debian trixie. The
    application supports both, and the launcher must not decide otherwise."""
    launcher = _read("packaging/polkit/launch-root")
    assert "/usr/bin/python3" in launcher
    for pinned in ("python3.9", "python3.11", "python3.12", "python3.13"):
        assert pinned not in launcher


def test_the_launcher_no_longer_looks_for_a_virtualenv():
    """Both packages install the application as a system Python package. The
    venv branch was a leftover from when the .rpm downloaded its dependencies,
    which is exactly what "installs on a machine with no internet" rules out."""
    assert "venv" not in _read("packaging/polkit/launch-root")


def test_the_launcher_takes_the_whole_session_not_just_the_display():
    """polkit clears the environment. A Wayland session needs its runtime
    directory as well, or the privileged copy has nowhere to put session
    files."""
    launcher = _read("packaging/polkit/launch-root")
    for variable in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"):
        assert variable in launcher


def test_the_deb_offers_an_agent_for_every_mainstream_desktop():
    """polkit is desktop-agnostic; the password prompt is not. A single named
    agent would leave every other desktop without a dialog at all."""
    control = _read("packaging/debian/control")
    recommends = control[control.index("Recommends:"):control.index("Description:")]
    for agent in ("polkit-kde-agent-1", "policykit-1-gnome", "mate-polkit", "xfce-polkit", "lxqt-policykit"):
        assert agent in recommends, agent


def test_the_deb_can_unpack_an_rpm_off_an_installation_image():
    """Checking a RedOS configuration means reading the distribution's own
    parser out of its image, and Debian has neither rpm2cpio nor bsdtar by
    default."""
    control = _read("packaging/debian/control")
    assert "libarchive-tools | rpm" in control


def test_the_deb_declares_cpio_for_reading_foreign_images():
    """rpm2cpio (from `rpm`) only unwraps the outer cpio archive -- cpio
    itself extracts it, и `install/image_catalog.py` тоже вызывает его прямо
    при чтении часовых поясов и раскладок с образа РЕД ОС, без обходного пути
    через bsdtar, который есть у `install/rpm_extract.py`."""
    control = _read("packaging/debian/control")
    assert "cpio" in control[control.index("Recommends:"):control.index("Description:")]


def test_cross_architecture_support_is_suggested_not_required():
    """It is only needed to build a medium for another architecture, and such
    an install is refused before the medium is touched when it is absent."""
    control = _read("packaging/debian/control")
    assert "qemu-user-static" in control[control.index("Suggests:"):control.index("Description:")]


def test_the_deb_can_ask_for_administrator_rights_without_a_separate_pkexec_package():
    """`pkexec` стал отдельным пакетом только в Debian 12 (bookworm); до этого
    его нёс `policykit-1`. Без альтернативы `apt-get install` на более старой
    системе отказал бы уже на разрешении зависимостей, до всякого запуска."""
    control = _read("packaging/debian/control")
    depends = control[control.index("Depends:"):control.index("Recommends:")]
    assert "pkexec | policykit-1" in depends


def test_the_rpm_spec_installs_a_system_package_not_a_virtualenv():
    spec = _read("packaging/rpm/portable-os-admin.spec")
    assert "python3_sitelib" in spec
    assert "/opt/portable-os-admin/venv" not in spec


def test_the_rpm_spec_has_no_host_dependent_fallback_for_sitelib():
    """`%{python3_sitelib}` без реальных RHEL-макросов (python3-devel) -- не
    то же самое, что «доступный на этой машине путь»: он взят из
    `sysconfig` build-хоста и на Debian указывал на
    /usr/lib/python3/dist-packages, куда интерпретатор РЕД ОС не заглядывает.
    Собранный так .rpm ставился, но падал ModuleNotFoundError на первом
    запуске. Резервного пути больше нет -- build-rpm.sh обязан отказаться
    раньше, чем rpmbuild вообще запустится (см. следующий тест)."""
    spec = _read("packaging/rpm/portable-os-admin.spec")
    assert "sysconfig.get_paths" not in spec
    assert "%{!?python3_sitelib" not in spec


def test_build_rpm_refuses_without_the_real_rhel_macros():
    script = _read("packaging/build-rpm.sh")
    assert "python3_sitelib" in script
    assert "exit 1" in script
    assert "RPM_NODEPS" not in script


def test_the_rpm_depends_on_the_interpreter_it_was_built_for_not_a_floor():
    """RHEL-семейство держит `python3` версии 3.6 вечно; 3.9/3.11/... -- это
    отдельные, по-разному названные пакеты. `python3 >= 3.9` из старой спеки
    либо не разрешается вовсе, либо разрешается не тем интерпретатором.
    `python3(abi)` -- это то виртуальное имя, которое rpm сам генерирует для
    интерпретатора, под который actually считался %{python3_sitelib} выше."""
    spec = _read("packaging/rpm/portable-os-admin.spec")
    assert "python3(abi) = %{python3_version}" in spec
    assert "Requires:       python3 >= 3.9" not in spec


def test_the_rpm_names_systemd_for_udevadm_not_a_guessed_subpackage():
    """`install/tooling.py` -- проверенный живьём источник истины о том, какой
    пакет что даёт; спека обязана согласовываться с ним, а не расходиться."""
    spec = _read("packaging/rpm/portable-os-admin.spec")
    assert "Requires:       systemd\n" in spec
    assert "Requires:       systemd-udev" not in spec


def test_the_rpm_declares_cpio_for_reading_the_images_it_extracts():
    """`install/image_catalog.py` распаковывает .rpm-пакеты образа (tzdata,
    xkeyboard-config) через `rpm2cpio | cpio` без обходного пути через bsdtar
    -- в отличие от `install/rpm_extract.py`, у которого такой путь есть.
    Поэтому cpio здесь -- Requires, а не Recommends."""
    spec = _read("packaging/rpm/portable-os-admin.spec")
    assert "Requires:       cpio\n" in spec


def test_the_rpm_source_archive_carries_the_files_the_spec_installs():
    """The spec installs the polkit action, its rule and the launcher out of
    packaging/. Leaving that directory out of the tarball fails the build at
    %install, which is a confusing place to learn about it."""
    script = _read("packaging/build-rpm.sh")
    stage_line = next(line for line in script.splitlines() if line.startswith("cp -r src"))
    assert "packaging" in stage_line


def test_the_desktop_file_agrees_with_the_copy_embedded_in_the_spec():
    """Имя записи киоска-меню живёт в двух местах -- отдельным файлом для
    .deb и heredoc'ом внутри спеки для .rpm, -- и ничего их не сверяло: они
    могли разойтись молча, и .rpm показывал бы устаревшее описание."""
    desktop_file = _read("packaging/debian/portable-os-admin.desktop").strip()
    spec = _read("packaging/rpm/portable-os-admin.spec")
    start = spec.index("cat > %{buildroot}/usr/share/applications/portable-os-admin.desktop <<'EOF'\n")
    start = spec.index("\n", start) + 1
    end = spec.index("\nEOF", start)
    embedded = spec[start:end].strip()
    assert embedded == desktop_file


def test_the_cryptography_floor_covers_what_the_code_actually_imports():
    """`config/exchange.py` подписывает обмен конфигурациями Ed25519 -- он
    появился в `cryptography` 2.6, позже занижённой границы 2.0, которая
    рассчитывалась только на AESGCM (2.0) и Scrypt (1.6)."""
    pyproject = _read("pyproject.toml")
    assert "cryptography>=2.6" in pyproject
    assert "cryptography>=2.0" not in pyproject


def test_the_tests_run_against_the_repository_and_not_a_stray_copy():
    """Проверяется код репозитория, а не копия, лежащая рядом.

    09.09.2026 в корне рабочего каталога на стенде оказалась копия пакета --
    остаток давней сборки `.deb`. Каталог с этим именем стоит в пути раньше
    `src/`, поэтому pytest час гонял старый код: правка была на диске, тест
    её не видел, а падение выглядело как ошибка в новой логике.

    Проверка дешёвая и однозначная: модуль обязан лежать внутри `src/` этого
    репозитория.
    """
    import pathlib

    import portable_os_admin

    module = pathlib.Path(portable_os_admin.__file__).resolve()
    root = pathlib.Path(__file__).resolve().parent.parent
    assert module.parent.parent == root / "src", (
        f"тесты импортируют {module}, а не код репозитория из {root / 'src'}"
    )
