"""Create/open/save/validate install configurations (UC-3).

"Конфигурация.Использование.Запрет": `ConfigManager.validate` is the only
place that flips `validation_status` to "valid", and the installer refuses to
run against anything else (enforced in install/installer.py).

**Конфигурации живут внутри контейнера администратора,** а не файлами рядом с
ним. Так было задумано с самого начала, но реализовано иначе, и расхождение
прожило до 09.09.2026: паролей в конфигурации нет, зато есть полное описание
защищаемой системы -- учётная запись, состав пакетов, что в киоске разрешено,
разметка, число машин на носитель, зеркала, -- и всё это лежало открытым
текстом рядом с зашифрованным контейнером.

Отсюда форма методов: они принимают открытый контейнер, а не путь. Файл
остаётся только у обмена между администраторами (`config/exchange.py`), и там
он зашифрован ключом получателя.
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

    def create(self, container, name: str, target_os: str = "debian") -> InstallConfig:
        """Новая конфигурация с умолчаниями, сразу сохранённая в контейнер."""
        config = InstallConfig.create_default(name=name, target_os=target_os)
        self.save(container, config)
        return config

    def open(self, container, name: str) -> InstallConfig:
        record = container.find_config(name)
        if record is None:
            raise ConfigError(f"в контейнере нет конфигурации «{name}»")
        try:
            return InstallConfig.from_dict(record.data)
        except (TypeError, ValueError, KeyError) as exc:
            raise ConfigError(f"конфигурация «{name}» повреждена: {exc}") from exc

    def save(self, container, config: InstallConfig) -> None:
        """Записать конфигурацию в контейнер под её собственным именем.

        Сам файл контейнера при этом не переписывается: за это отвечает
        `ContainerManager.save`, и вызывает его тот, кто знает, когда работа
        закончена. Так одна правка конфигурации не приводит к перешифровке
        контейнера на каждое нажатие.
        """
        if not config.name:
            raise ConfigError("у конфигурации нет имени")
        container.put_config(config.name, config.to_dict())

    def delete(self, container, name: str) -> None:
        if not container.remove_config(name):
            raise ConfigError(f"в контейнере нет конфигурации «{name}»")

    def rename(self, container, old_name: str, new_name: str) -> None:
        """Переименование -- это смена ключа, поэтому отдельным методом.

        Через `save` оно превратилось бы в копию: старая запись осталась бы
        под прежним именем.
        """
        config = self.open(container, old_name)
        config.name = new_name
        container.remove_config(old_name)
        container.put_config(new_name, config.to_dict())

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

    def list_configs(self, container) -> "list[str]":
        """Имена конфигураций в контейнере, по алфавиту."""
        return [record.name for record in container.list_configs()]

    def load_all(self, container) -> "list[InstallConfig]":
        """Все конфигурации разобранными -- для списков, где нужен не только

        заголовок, но и состояние проверки или целевая система.
        """
        loaded = []
        for record in container.list_configs():
            try:
                loaded.append(InstallConfig.from_dict(record.data))
            except (TypeError, ValueError, KeyError):
                # Повреждённая запись не должна прятать остальные: она
                # пропускается здесь и назовётся при попытке открыть её.
                continue
        return loaded
