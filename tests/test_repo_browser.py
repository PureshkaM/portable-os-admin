import gzip

import pytest

from portable_os_admin.config import repo_browser
from portable_os_admin.errors import RepoBrowseError

APT_PACKAGES_TEXT = """Package: vim
Version: 2:9.0.1378-2
Description: Vi IMproved - enhanced vi editor
 long description continues here

Package: curl
Version: 7.88.1-10
Description: command line tool for transferring data with URL syntax
"""

PRIMARY_XML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<metadata xmlns="http://linux.duke.edu/metadata/common" xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="2">
<package type="rpm">
  <name>openssh-server</name>
  <arch>x86_64</arch>
  <version epoch="0" ver="9.3p1" rel="5.red80"/>
  <summary>An open source SSH server daemon</summary>
</package>
<package type="rpm">
  <name>some-i686-only-lib</name>
  <arch>i686</arch>
  <version epoch="0" ver="1.0" rel="1"/>
  <summary>Should be filtered out (wrong arch)</summary>
</package>
</metadata>
"""

REPOMD_XML = """<?xml version="1.0" encoding="UTF-8"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <location href="repodata/deadbeef-primary.xml.gz"/>
  </data>
</repomd>
"""


def test_apt_package_source_parses_packages_index(monkeypatch):
    def fake_fetch(url):
        assert url == "http://example.test/debian/dists/bookworm/main/binary-amd64/Packages.gz"
        return gzip.compress(APT_PACKAGES_TEXT.encode("utf-8"))

    monkeypatch.setattr(repo_browser, "_fetch", fake_fetch)

    source = repo_browser.AptPackageSource()
    packages = source.list_packages("http://example.test/debian", "bookworm")

    names = {p.name for p in packages}
    assert names == {"vim", "curl"}
    vim = next(p for p in packages if p.name == "vim")
    assert vim.version == "2:9.0.1378-2"
    assert "Vi IMproved" in vim.summary


def test_apt_package_source_requires_mirror_and_release():
    source = repo_browser.AptPackageSource()
    with pytest.raises(RepoBrowseError):
        source.list_packages("", "bookworm")
    with pytest.raises(RepoBrowseError):
        source.list_packages("http://example.test/debian", "")


def test_dnf_package_source_parses_primary_and_filters_arch(monkeypatch):
    def fake_fetch(url):
        if url.endswith("repodata/repomd.xml"):
            return REPOMD_XML.encode("utf-8")
        assert url.endswith("repodata/deadbeef-primary.xml.gz")
        return gzip.compress(PRIMARY_XML_TEMPLATE.encode("utf-8"))

    monkeypatch.setattr(repo_browser, "_fetch", fake_fetch)

    source = repo_browser.DnfPackageSource()
    packages = source.list_packages("http://example.test/redos/8.0/x86_64/os", "8.0")

    names = {p.name for p in packages}
    assert names == {"openssh-server"}  # i686 package filtered out
    pkg = packages[0]
    assert pkg.version == "9.3p1-5.red80"
    assert "SSH server" in pkg.summary


def test_dnf_package_source_missing_primary_data_raises(monkeypatch):
    def fake_fetch(url):
        return b'<?xml version="1.0"?><repomd xmlns="http://linux.duke.edu/metadata/repo"></repomd>'

    monkeypatch.setattr(repo_browser, "_fetch", fake_fetch)

    source = repo_browser.DnfPackageSource()
    with pytest.raises(RepoBrowseError):
        source.list_packages("http://example.test/redos/8.0/x86_64/os", "8.0")


def _no_network(monkeypatch):
    """Любое обращение к сети в этих тестах -- провал, а не запасной путь."""
    def forbidden(url):
        raise AssertionError(f"обращение к сети: {url}")

    monkeypatch.setattr(repo_browser, "_fetch", forbidden)


def _redos_image(root):
    """Образ РЕД ОС: repodata/ в корне, как в test_image_catalog."""
    repodata = root / "repodata"
    repodata.mkdir()
    (repodata / "repomd.xml").write_text(REPOMD_XML, encoding="utf-8")
    with gzip.open(repodata / "deadbeef-primary.xml.gz", "wt", encoding="utf-8") as handle:
        handle.write(PRIMARY_XML_TEMPLATE)
    return str(root)


def _debian_image(root, release="trixie"):
    """Образ Debian: dists/<выпуск>/<компонент>/binary-amd64/Packages.gz."""
    suite = root / "dists" / release
    (suite / "main" / "binary-amd64").mkdir(parents=True)
    (suite / "contrib" / "binary-amd64").mkdir(parents=True)
    (suite / "Release").write_text(f"Codename: {release}\n", encoding="utf-8")
    with gzip.open(suite / "main" / "binary-amd64" / "Packages.gz", "wt", encoding="utf-8") as handle:
        handle.write(APT_PACKAGES_TEXT)
    contrib = "Package: ttf-mscorefonts-installer\nVersion: 3.8.1\nDescription: Installer\n"
    with gzip.open(suite / "contrib" / "binary-amd64" / "Packages.gz", "wt", encoding="utf-8") as handle:
        handle.write(contrib)
    return str(root)


def test_redos_packages_come_from_the_image_without_network(tmp_path, monkeypatch):
    """В закрытом контуре сети нет, а образ есть всегда."""
    _no_network(monkeypatch)
    packages = repo_browser.list_image_packages(_redos_image(tmp_path), "redos")

    assert [p.name for p in packages] == ["openssh-server"]
    assert packages[0].version == "9.3p1-5.red80"


def test_redos_image_without_repomd_is_found_by_file_name(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    image = _redos_image(tmp_path)
    (tmp_path / "repodata" / "repomd.xml").unlink()

    assert [p.name for p in repo_browser.list_image_packages(image, "redos")] == ["openssh-server"]


def test_debian_packages_come_from_all_components_of_the_image(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    image = _debian_image(tmp_path)

    names = [p.name for p in repo_browser.list_image_packages(image, "debian", "trixie", "x86_64")]

    # contrib читается наравне с main, а x86_64 из конфигурации -- это amd64 apt.
    assert names == ["curl", "ttf-mscorefonts-installer", "vim"]


def test_debian_release_not_on_the_image_falls_back_to_what_is_there(tmp_path, monkeypatch):
    """Выпуск в конфигурации может называться иначе (`stable`), а образ один."""
    _no_network(monkeypatch)
    image = _debian_image(tmp_path)

    names = {p.name for p in repo_browser.list_image_packages(image, "debian", "stable", "")}
    assert {"vim", "curl"} <= names


def test_image_of_the_wrong_family_is_an_error_not_an_empty_list(tmp_path):
    with pytest.raises(RepoBrowseError):
        repo_browser.list_image_packages(_debian_image(tmp_path), "redos")
    with pytest.raises(RepoBrowseError):
        repo_browser.list_image_packages(str(tmp_path / "нет"), "debian")


def test_load_packages_prefers_the_image(tmp_path, monkeypatch):
    _no_network(monkeypatch)
    packages, origin = repo_browser.load_packages(
        "debian", "trixie", image_mountpoint=_debian_image(tmp_path), mirror="http://example.test/debian"
    )
    assert origin == "image"
    assert "vim" in {p.name for p in packages}


def test_network_is_the_fallback_when_the_image_has_no_index(tmp_path, monkeypatch):
    monkeypatch.setattr(
        repo_browser, "_fetch", lambda url: gzip.compress(APT_PACKAGES_TEXT.encode("utf-8"))
    )
    packages, origin = repo_browser.load_packages(
        "debian", "bookworm", image_mountpoint=str(tmp_path), mirror="http://example.test/debian"
    )
    assert origin == "network"
    assert {p.name for p in packages} == {"vim", "curl"}


def test_when_both_sources_fail_both_reasons_are_named(tmp_path, monkeypatch):
    """Иначе в закрытом контуре видно только «нет сети», а беда была в образе."""
    def offline(url):
        raise RepoBrowseError("нет сети")

    monkeypatch.setattr(repo_browser, "_fetch", offline)
    with pytest.raises(RepoBrowseError) as caught:
        repo_browser.load_packages(
            "debian", "trixie", image_mountpoint=str(tmp_path), mirror="http://example.test/debian"
        )
    assert "dists/" in str(caught.value)
    assert "нет сети" in str(caught.value)


def test_get_package_source_factory():
    assert isinstance(repo_browser.get_package_source("debian"), repo_browser.AptPackageSource)
    assert isinstance(repo_browser.get_package_source("redos"), repo_browser.DnfPackageSource)
    with pytest.raises(RepoBrowseError):
        repo_browser.get_package_source("basealt")
