"""Tests for building a medium of another architecture.

These check the decision, not the emulation: whether qemu actually runs an
x86_64 binary on an arm64 host was established by running it (see
README, раздел «Архитектура» "Поддерживаемые сочетания"), and no unit test can restate
that. What is worth pinning down here is that the program refuses *before*
erasing the medium when the machine is not set up, and that it recognises a
registration written by whatever the distribution ships rather than one exact
name.
"""
from portable_os_admin.install import emulation


def _write_registration(directory, name, *, status="enabled", interpreter="/usr/bin/qemu-x86_64-static", flags="OCF"):
    path = directory / name
    path.write_text(
        f"{status}\ninterpreter {interpreter}\nflags: {flags}\noffset 0\nmagic 7f454c46\n",
        encoding="utf-8",
    )
    return path


def test_architecture_spellings_are_normalised_onto_the_kernel_s():
    assert emulation.kernel_arch("amd64") == "x86_64"
    assert emulation.kernel_arch("x86_64") == "x86_64"
    assert emulation.kernel_arch("arm64") == "aarch64"
    assert emulation.kernel_arch("ppc64el") == "ppc64le"


def test_an_unknown_architecture_is_passed_through_not_rejected():
    """A name this table has never heard of must degrade into "no emulator
    registered", which is true and actionable, rather than into "unsupported
    by this program", which would be this program inventing a limit."""
    assert emulation.kernel_arch("some-future-arch") == "some-future-arch"


def test_native_target_needs_no_emulator(monkeypatch):
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    result = emulation.probe("arm64")
    assert result.native and result.available
    assert result.interpreter == ""


def test_registered_emulator_makes_a_foreign_target_possible(tmp_path, monkeypatch):
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    _write_registration(tmp_path, "qemu-x86_64")

    result = emulation.probe("amd64")
    assert result.available and not result.native
    assert result.registration == "qemu-x86_64"
    assert result.fix_binary


def test_a_registration_is_matched_by_its_interpreter_too(tmp_path, monkeypatch):
    """Debian names the entry qemu-x86_64 but points it at a wrapper called
    x86_64-binfmt-P; another distribution may name either differently. Matching
    on the architecture token in either place avoids hardcoding one layout."""
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    _write_registration(tmp_path, "some-local-name", interpreter="/usr/libexec/qemu-binfmt/x86_64-binfmt-P")

    assert emulation.probe("amd64").available


def test_missing_emulator_names_the_package_to_install(tmp_path, monkeypatch):
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))

    result = emulation.probe("amd64")
    assert not result.available
    assert emulation.EMULATOR_PACKAGE in result.reason


def test_disabled_registration_is_not_treated_as_available(tmp_path, monkeypatch):
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    _write_registration(tmp_path, "qemu-x86_64", status="disabled")

    result = emulation.probe("amd64")
    assert not result.available
    assert "выключен" in result.reason


def test_registration_without_the_fix_binary_flag_is_refused(tmp_path, monkeypatch):
    """Without F the kernel looks for the interpreter inside the chroot, where
    it cannot exist before the root has been bootstrapped. Reporting that is
    honest; pretending it will work would fail deep inside debootstrap."""
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    _write_registration(tmp_path, "qemu-x86_64", flags="OC")

    result = emulation.probe("amd64")
    assert not result.available
    assert "без флага F" in result.reason


def test_no_binfmt_support_at_all_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path / "absent"))

    result = emulation.probe("amd64")
    assert not result.available
    assert "binfmt_misc" in result.reason


def test_installer_refuses_before_the_medium_is_touched(monkeypatch, tmp_path):
    """The whole point of probing early: failing here costs nothing, failing
    after wipefs costs the medium's previous contents."""
    import pytest

    from portable_os_admin.config import InstallConfig
    from portable_os_admin.errors import InstallError
    from portable_os_admin.install.installer import OSInstaller

    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))

    config = InstallConfig.create_default("stick1", "debian")
    config.arch = "amd64"
    installer = OSInstaller.__new__(OSInstaller)
    with pytest.raises(InstallError) as excinfo:
        installer._check_arch(None, config)
    assert emulation.EMULATOR_PACKAGE in str(excinfo.value)


def test_installer_warns_when_the_medium_was_built_under_emulation(monkeypatch, tmp_path):
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.installer import OSInstaller

    monkeypatch.setattr(emulation.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    _write_registration(tmp_path, "qemu-x86_64")

    config = InstallConfig.create_default("stick1", "debian")
    config.arch = "amd64"
    warnings = OSInstaller.__new__(OSInstaller)._check_arch(None, config)
    assert len(warnings) == 1
    assert "x86_64" in warnings[0]
    # the operator must be told to boot it on real target hardware
    assert "проверьте загрузку" in warnings[0]
