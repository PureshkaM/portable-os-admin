"""Recognising installation images.

Works on directory trees rather than real ISOs: `inspect_mounted` reads a
mounted image, and a directory laid out the same way exercises exactly the same
code without needing root or a multi-gigabyte fixture. The shapes below are
copied from the real images this was checked against -- a Debian 13.6 DVD and a
RED OS 8 DVD.
"""
from __future__ import annotations

import gzip

import pytest

from portable_os_admin.errors import InstallError
from portable_os_admin.install.image_source import inspect_mounted


def _debian_tree(root, packages, *, architectures="amd64", codename="trixie"):
    suite = root / "dists" / codename
    (suite / "main" / f"binary-{architectures}").mkdir(parents=True)
    (suite / "Release").write_text(
        f"Origin: Debian\nSuite: stable\nVersion: 13.6\nCodename: {codename}\n"
        f"Architectures: {architectures}\nComponents: main contrib\n",
        encoding="utf-8",
    )
    index = suite / "main" / f"binary-{architectures}" / "Packages.gz"
    body = "".join(f"Package: {name}\nVersion: 1\n\n" for name in packages)
    index.write_bytes(gzip.compress(body.encode("utf-8")))
    (root / ".disk").mkdir()
    (root / ".disk" / "info").write_text('Debian GNU/Linux 13.6.0 "Trixie" - DVD Binary-1\n', encoding="utf-8")
    return root


def test_reads_family_release_and_architecture_out_of_the_image(tmp_path):
    """Nothing is inferred from the filename, and no list of known releases is
    consulted -- so an image of a distribution version this program has never
    heard of still works."""
    _debian_tree(tmp_path, ["linux-image-amd64", "grub-efi-amd64", "bash"], codename="forky")

    info = inspect_mounted("/somewhere/whatever-the-file-is-called.iso", str(tmp_path))

    assert info.family == "debian"
    assert info.release == "forky"
    assert info.architectures == ("amd64",)
    assert info.complete
    assert "Trixie" in info.label  # taken from the image, not from the path


def test_network_install_image_is_rejected_with_the_reason(tmp_path):
    """A netinst image carries a base system but no kernel and no bootloader,
    so a medium built from it could never boot. Judged by what the image holds
    rather than by its name, because the name is not evidence."""
    _debian_tree(tmp_path, ["bash", "coreutils"])

    info = inspect_mounted("/images/debian-13.6.0-amd64-netinst.iso", str(tmp_path))

    assert not info.complete
    assert "ядро" in info.incomplete_reason
    assert "загрузчик" in info.incomplete_reason
    assert "netinst" in info.incomplete_reason


def test_rhel_family_image_is_recognised(tmp_path):
    """Shape taken from the real RED OS 8 DVD: the product name lives in
    .treeinfo, while the first line of .discinfo is a build timestamp."""
    (tmp_path / "repodata").mkdir()
    (tmp_path / "repodata" / "repomd.xml").write_text("<repomd/>", encoding="utf-8")
    (tmp_path / "repodata" / "primary.xml.zst").write_bytes(b"x" * 200_000)
    (tmp_path / "Packages").mkdir()
    (tmp_path / ".treeinfo").write_text(
        "[general]\narch = x86_64\nfamily = RED OS\nname = RED OS 8\nversion = 8\n",
        encoding="utf-8",
    )
    (tmp_path / ".discinfo").write_text("1752250767.418455\nRED OS 8\nx86_64\n", encoding="utf-8")

    info = inspect_mounted("/images/redos-8.iso", str(tmp_path))

    assert info.family == "redos"
    assert info.release == "8"
    assert info.architectures == ("x86_64",)
    assert info.label == "RED OS 8"
    assert info.complete


def test_something_that_is_not_an_installation_image_is_refused(tmp_path):
    (tmp_path / "some-file.txt").write_text("hello", encoding="utf-8")

    with pytest.raises(InstallError, match="не удалось распознать образ"):
        inspect_mounted("/images/holiday-photos.iso", str(tmp_path))


def test_components_over_declared_by_the_release_file_are_dropped(tmp_path):
    """apt drops the whole repository when one declared component has no index
    for the architecture in use, which would cost the entire install. Better to
    quietly not offer that component."""
    from portable_os_admin.install.image_source import _usable_components

    suite = tmp_path / "dists" / "trixie"
    (suite / "main" / "binary-amd64").mkdir(parents=True)
    (suite / "contrib" / "binary-amd64").mkdir(parents=True)
    # declared but carries nothing for this architecture
    (suite / "non-free" / "binary-i386").mkdir(parents=True)

    fields = {"Components": "main contrib non-free"}
    assert _usable_components(str(suite), fields, ("amd64",)) == ("main", "contrib")


def test_components_fall_back_to_main_when_nothing_matches(tmp_path):
    from portable_os_admin.install.image_source import _usable_components

    suite = tmp_path / "dists" / "trixie"
    suite.mkdir(parents=True)
    assert _usable_components(str(suite), {"Components": "weird"}, ("amd64",)) == ("main",)


# ----------------------------------------------------------------------
# Версии графических оболочек
# ----------------------------------------------------------------------
def test_apt_index_yields_the_desktop_version():
    """Версия читается из индекса образа, а не выводится из выпуска.

    Правило «такой-то выпуск значит такая-то версия» пришлось бы обновлять к
    каждому релизу. Пакеты упразднённого KDE (01.10.2026) больше не читаются.
    """
    import gzip
    import tempfile

    from portable_os_admin.install.image_source import _desktop_versions_from_apt

    index = (
        b"Package: mate-desktop\nVersion: 1.26.2-1.2\n\n"
        b"Package: plasma-desktop\nVersion: 4:6.3.6-1\nArchitecture: amd64\n\n"
        b"Package: something-else\nVersion: 9.9\n\n"
    )
    with tempfile.NamedTemporaryFile(suffix=".gz", delete=False) as handle:
        handle.write(gzip.compress(index))
        path = handle.name

    versions = _desktop_versions_from_apt(path)
    assert versions == {"mate": "1.26.2-1.2"}


def test_an_unreadable_index_is_not_a_failure():
    """Образ без индекса — повод не знать версию, а не повод упасть."""
    from portable_os_admin.install.image_source import _desktop_versions_from_apt

    assert _desktop_versions_from_apt("/нет/такого/файла.gz") == {}
