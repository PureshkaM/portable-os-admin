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


def test_the_polkit_launcher_uses_the_packaged_entry_point():
    """Both the menu and the privileged process use the packaged entry point."""
    launcher = _read("packaging/polkit/launch-root")
    assert "exec /usr/bin/portable-os-admin" in launcher


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


def test_cross_architecture_support_is_suggested_not_required():
    """It is only needed to build a medium for another architecture, and such
    an install is refused before the medium is touched when it is absent."""
    control = _read("packaging/debian/control")
    assert "qemu-user-static" in control[control.index("Suggests:"):control.index("Description:")]


def test_the_rpm_resolves_python_dependencies_from_the_operating_system():
    spec = _read("packaging/rpm/portable-os-admin.spec")
    requires = {
        line.split()[1] for line in spec.splitlines() if line.startswith("Requires:")
    }
    assert {"python3", "python3-tkinter", "python3-cryptography"} <= requires
    assert "exec /usr/bin/python3 -I /usr/lib/portable-os-admin/app" in spec
    assert "/usr/lib/portable-os-admin/runtime" not in spec


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
