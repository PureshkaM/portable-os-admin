import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.config.validator import (
    BuiltinConfigValidator,
    ConfigValidationUnavailableError,
    ExternalCommandValidator,
)


def test_create_default_config_is_initially_unchecked(tmp_path):
    cm = ConfigManager()
    config = cm.create(tmp_path, "stick1", "debian")
    assert config.validation_status == "unchecked"
    assert config.target_os == "debian"


def test_valid_config_passes_builtin_validation(make_config):
    config = make_config()
    result = BuiltinConfigValidator().validate(config)
    assert result.ok
    assert result.errors == []


@pytest.mark.parametrize(
    "mutate,expected_substring",
    [
        (lambda c: setattr(c, "target_os", "windows"), "целевая ОС"),
        (lambda c: setattr(c, "release", ""), "релиз"),
        (lambda c: setattr(c.disk, "boot_partition_mb", 10), "/boot"),
        (lambda c: setattr(c.disk, "esp_partition_mb", 10), "EFI-раздела"),
        (lambda c: setattr(c, "arch", "sparc"), "архитектура"),
        (lambda c: setattr(c.luks, "failed_attempts_limit", 0), "лимит попыток"),
        (lambda c: setattr(c.account, "username", "1bad name"), "имя пользователя"),
        (lambda c: setattr(c.remote_access, "enabled", True) or setattr(c.remote_access, "port", 0), "порт"),
    ],
)
def test_invalid_configs_are_rejected(mutate, expected_substring, make_config):
    config = make_config()
    mutate(config)
    result = BuiltinConfigValidator().validate(config)
    assert not result.ok
    assert any(expected_substring in err for err in result.errors)


def test_config_manager_validate_updates_status_in_place(make_config):
    cm = ConfigManager()
    config = make_config("stick1", "redos")
    cm.validate(config)
    assert config.validation_status == "valid"


def test_save_and_open_round_trip(tmp_path):
    cm = ConfigManager()
    config = cm.create(tmp_path, "stick1", "debian")
    config.account.username = "ivanov"
    config.packages = ["vim", "htop"]
    path = tmp_path / "stick1.json"
    cm.save(config, path)

    reopened = cm.open(path)
    assert reopened.account.username == "ivanov"
    assert reopened.packages == ["vim", "htop"]


def test_external_validator_unavailable_when_command_missing():
    validator = ExternalCommandValidator("this-command-does-not-exist-poa")
    config = InstallConfig.create_default("stick1", "debian")
    with pytest.raises(ConfigValidationUnavailableError):
        validator.validate(config)


def test_list_configs_empty_directory(tmp_path):
    cm = ConfigManager()
    assert cm.list_configs(tmp_path / "nonexistent") == []


def test_configuration_is_checked_by_the_distributions_own_tool(make_config, monkeypatch):
    """The project's own rules only prove a configuration agrees with itself.

    Each target family ships an official validator for its own automated-install
    file, so the configuration is rendered into that format and handed to it --
    ksvalidator for the RHEL family, debconf-set-selections for Debian.
    """
    from portable_os_admin.config import native
    from portable_os_admin.config.validator import NATIVE_VALIDATORS

    assert native.render(make_config("s", "debian"))[0] == "preseed"
    assert native.render(make_config("s", "astra"))[0] == "preseed"
    assert native.render(make_config("s", "redos"))[0] == "kickstart"

    # The Debian tool reports a malformed file as a *warning* and still exits 0
    # -- verified against the real tool -- so its verdict has to come from its
    # output. Trusting the exit status would pass everything.
    assert NATIVE_VALIDATORS["preseed"][2] == "output"
    assert NATIVE_VALIDATORS["kickstart"][2] == "status"


def test_missing_distribution_tool_is_reported_not_silently_skipped(make_config, monkeypatch):
    """pykickstart is not packaged for Debian, so a RedOS configuration written
    on a Debian workstation genuinely cannot be checked this way. Saying the
    configuration is fine would overstate what was verified."""
    from portable_os_admin.config import validator as validator_module

    monkeypatch.setattr(validator_module.shutil, "which", lambda name: None)
    manager = ConfigManager()
    config = make_config("stick1", "redos")
    manager.validate(config)

    assert config.validation_status == "valid"  # internal rules passed
    assert "не установлено" in manager.native_validation_note


def test_native_rendering_carries_the_authored_values(make_config):
    config = make_config("stick1", "redos")
    config.account.username = "ivanov"
    config.account.sudo = True
    config.packages = ["htop"]
    config.remote_access.enabled = True

    from portable_os_admin.config import native

    text = native.to_kickstart(config)
    assert "--name=ivanov" in text
    assert "--groups=wheel" in text  # the RHEL family's admin group, not sudo
    assert "htop" in text
    assert "openssh-server" in text
    assert f"--cipher={config.luks.cipher}" in text
