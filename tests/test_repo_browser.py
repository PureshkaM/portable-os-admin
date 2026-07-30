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


def test_get_package_source_factory():
    assert isinstance(repo_browser.get_package_source("debian"), repo_browser.AptPackageSource)
    assert isinstance(repo_browser.get_package_source("astra"), repo_browser.AptPackageSource)
    assert isinstance(repo_browser.get_package_source("redos"), repo_browser.DnfPackageSource)
    with pytest.raises(RepoBrowseError):
        repo_browser.get_package_source("basealt")
