"""Create/open/save/validate install configurations (UC-3).

"Конфигурация.Использование.Запрет": `ConfigManager.validate` is the only
place that flips `validation_status` to "valid", and the installer refuses to
run against anything else (enforced in install/installer.py).
"""
from __future__ import annotations

import json
from pathlib import Path

from ..errors import ConfigError
from .schema import InstallConfig
from .validator import (
    BuiltinConfigValidator,
    ConfigValidationUnavailableError,
    ConfigValidator,
    NativeToolValidator,
)


class ConfigManager:
    def __init__(
        self,
        validator: ConfigValidator | None = None,
        native_validator: ConfigValidator | None = None,
    ):
        self.validator = validator or BuiltinConfigValidator()
        self.native_validator = native_validator or NativeToolValidator()
        #: Set by validate() when the distribution's own tool was unavailable,
        #: so a caller can tell the user what was and was not checked.
        self.native_validation_note = ""

    def create(self, directory: Path, name: str, target_os: str = "debian") -> InstallConfig:
        config = InstallConfig.create_default(name=name, target_os=target_os)
        self.save(config, self._path_for(directory, name))
        return config

    def open(self, path: Path) -> InstallConfig:
        path = Path(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"не удалось открыть файл конфигурации: {exc}") from exc
        return InstallConfig.from_dict(payload)

    def save(self, config: InstallConfig, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"не удалось сохранить файл конфигурации: {exc}") from exc

    def validate(self, config: InstallConfig) -> InstallConfig:
        """Runs validation and updates config.validation_status/errors in place.

        Two layers, because they know different things. This project's own
        structural rules cover its own requirements -- that /boot must sit
        outside the LUKS volume, which key sizes a given cipher accepts -- which
        no distribution tool has any reason to know. The distribution's own
        validator (ksvalidator for the RHEL family, debconf-set-selections for
        Debian) checks the configuration against a real external standard
        instead of against rules invented here, which is what the
        specification's "внешнее средство проверки конфигурации" means.

        A missing distribution tool is reported, not silently skipped: the
        result then says the configuration passed the internal rules only.
        Raises ConfigValidationUnavailableError only when validation could not
        be performed at all -- "Конфигурация.Проверка.Недоступна".
        """
        result = self.validator.validate(config)
        errors = list(result.errors)
        self.native_validation_note = ""

        try:
            native = self.native_validator.validate(config)
        except ConfigValidationUnavailableError as exc:
            self.native_validation_note = str(exc)
        else:
            errors += [e for e in native.errors if e not in errors]

        config.validation_errors = errors
        config.validation_status = "valid" if not errors else "invalid"
        return config

    def list_configs(self, directory: Path) -> list[Path]:
        directory = Path(directory)
        if not directory.exists():
            return []
        return sorted(directory.glob("*.json"))

    @staticmethod
    def _path_for(directory: Path, name: str) -> Path:
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return Path(directory) / f"{safe_name}.json"
