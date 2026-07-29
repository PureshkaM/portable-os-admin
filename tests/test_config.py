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


def test_valid_config_passes_builtin_validation():
    config = InstallConfig.create_default("stick1", "debian")
    result = BuiltinConfigValidator().validate(config)
    assert result.ok
    assert result.errors == []


@pytest.mark.parametrize(
    "mutate,expected_substring",
    [
        (lambda c: setattr(c, "target_os", "windows"), "целевая ОС"),
        (lambda c: setattr(c, "release", ""), "релиз"),
        (lambda c: setattr(c.disk, "boot_partition_mb", 10), "загрузочного раздела"),
        (lambda c: setattr(c.luks, "failed_attempts_limit", 0), "лимит попыток"),
        (lambda c: setattr(c.account, "username", "1bad name"), "имя пользователя"),
        (lambda c: setattr(c.remote_access, "enabled", True) or setattr(c.remote_access, "port", 0), "порт"),
    ],
)
def test_invalid_configs_are_rejected(mutate, expected_substring):
    config = InstallConfig.create_default("stick1", "debian")
    mutate(config)
    result = BuiltinConfigValidator().validate(config)
    assert not result.ok
    assert any(expected_substring in err for err in result.errors)


def test_config_manager_validate_updates_status_in_place():
    cm = ConfigManager()
    config = InstallConfig.create_default("stick1", "redos")
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
