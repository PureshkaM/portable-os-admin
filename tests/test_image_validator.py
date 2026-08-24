"""Tests for taking the configuration validator off the installation image.

That this actually works was established by running it: the parser unpacked
from a real x86_64 RedOS 8 image accepted a generated configuration and
rejected an unknown command, a misspelt option and a missing required
argument -- on an arm64 Debian host, because the package is noarch. These
tests cover the parts that can go wrong silently on a different machine:
finding the package, unpacking it with whatever tool exists, and refusing to
claim a configuration was checked when it was not.
"""
import json
import subprocess
import sys

import pytest

from portable_os_admin.config import image_validator
from portable_os_admin.install import rpm_extract
from portable_os_admin.config.validator import (
    ConfigValidationUnavailableError,
    NativeToolValidator,
    kickstart_syntax_version,
)


def _fake_image(tmp_path, relative="Packages/p/python3-kickstart-3.47-1.red80.noarch.rpm"):
    package = tmp_path / relative
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_bytes(b"not really an rpm")
    return package


def test_package_is_found_in_the_usual_place(tmp_path):
    package = _fake_image(tmp_path)
    assert rpm_extract.find_package(str(tmp_path), image_validator.PACKAGE_PATTERNS) == str(package)


def test_package_is_found_in_an_unusual_layout(tmp_path):
    """Layouts differ between RHEL rebuilds; the search must not depend on one."""
    package = _fake_image(tmp_path, "somewhere/else/pykickstart-3.52.noarch.rpm")
    assert rpm_extract.find_package(str(tmp_path), image_validator.PACKAGE_PATTERNS) == str(package)


def test_an_image_without_a_parser_says_so(tmp_path):
    (tmp_path / "Packages").mkdir()
    with pytest.raises(rpm_extract.PackageNotOnImageError):
        rpm_extract.find_package(str(tmp_path), image_validator.PACKAGE_PATTERNS)


def test_module_search_path_points_at_the_parent_of_the_package(tmp_path):
    """Found by locating the module itself rather than assuming a Python
    version or site-packages vs dist-packages."""
    module = tmp_path / "usr/lib/python3.11/site-packages/pykickstart"
    module.mkdir(parents=True)
    (module / "version.py").write_text("", encoding="utf-8")

    assert image_validator.module_search_path(str(tmp_path)) == str(module.parent)


def test_unpacking_without_any_extractor_names_what_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(rpm_extract, "resolve_tool", lambda name: None)
    with pytest.raises(rpm_extract.PackageNotOnImageError) as excinfo:
        rpm_extract.unpack(str(_fake_image(tmp_path)), str(tmp_path / "out"))
    assert "libarchive-tools" in str(excinfo.value)


def test_syntax_version_comes_from_the_image_release():
    assert kickstart_syntax_version("redos", "8.0") == "RHEL8"
    assert kickstart_syntax_version("redos", "7.3") == "RHEL7"
    # a family that does not use kickstart, or a release that is not a number,
    # yields no request at all -- the runner then picks the newest version the
    # parser implements instead of being handed a made-up one
    assert kickstart_syntax_version("debian", "trixie") == ""
    assert kickstart_syntax_version("redos", "") == ""


def test_a_broken_image_does_not_pass_the_configuration_silently(tmp_path, make_config):
    """The failure mode that matters: when the parser cannot be obtained, the
    answer must be "not checked", never "valid"."""
    (tmp_path / "Packages").mkdir()
    validator = NativeToolValidator(str(tmp_path), "redos", "8.0")
    config = make_config()
    config.target_os = "redos"
    with pytest.raises(ConfigValidationUnavailableError):
        validator.validate(config)


def test_runner_reports_a_missing_parser_as_unavailable_not_as_invalid(tmp_path):
    """Run with an empty PYTHONPATH the runner cannot import pykickstart. It
    must still print a verdict this program can read, and that verdict must
    distinguish "could not check" from "configuration is wrong"."""
    from portable_os_admin.config.validator import KICKSTART_RUNNER

    config = tmp_path / "config.ks"
    config.write_text("lang ru_RU.UTF-8\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, KICKSTART_RUNNER, str(config)],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(tmp_path / "empty"), "PATH": "/usr/bin:/bin"},
        check=False,
    )
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["available"] is False
    assert report["ok"] is False
