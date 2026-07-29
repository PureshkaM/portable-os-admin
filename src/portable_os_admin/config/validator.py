"""Configuration validation.

Implements UC-3's validation step and "Конфигурация.Проверка.*". The spec
requires the check to go through "внешнее средство проверки конфигурации"
(Программные интерфейсы-2); `ExternalCommandValidator` models that as a
pluggable subprocess call, while `BuiltinConfigValidator` is the default
implementation used when no external tool is configured, so the application
is usable standalone.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .schema import SUPPORTED_TARGET_OS, InstallConfig

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str]


class ConfigValidator(Protocol):
    def validate(self, config: InstallConfig) -> ValidationResult:
        ...


class BuiltinConfigValidator:
    """Synchronous, in-process validation covering the requirements that are
    checkable without booting any real installer (structural + range checks)."""

    def validate(self, config: InstallConfig) -> ValidationResult:
        errors: list[str] = []

        if config.target_os not in SUPPORTED_TARGET_OS:
            errors.append(
                f"неподдерживаемая целевая ОС: {config.target_os!r} "
                f"(ожидается одно из {SUPPORTED_TARGET_OS})"
            )

        if not config.release:
            errors.append("не задан релиз/версия устанавливаемой ОС")

        disk = config.disk
        if disk.boot_partition_mb < 256:
            errors.append("размер загрузочного раздела должен быть не менее 256 МБ")
        if disk.log_partition_mb < 64:
            errors.append("размер раздела логов должен быть не менее 64 МБ")
        if disk.exchange_partition_mb < 64:
            errors.append("размер раздела обмена должен быть не менее 64 МБ")

        luks = config.luks
        if luks.key_size_bits not in (256, 512):
            errors.append("размер ключа LUKS должен быть 256 или 512 бит")
        if not (1 <= luks.failed_attempts_limit <= 10):
            errors.append("лимит попыток ввода пароля должен быть в диапазоне от 1 до 10")

        account = config.account
        if not _USERNAME_RE.match(account.username):
            errors.append(
                f"недопустимое имя пользователя: {account.username!r} "
                "(строчные буквы/цифры/подчёркивание/дефис, не начинается с цифры)"
            )
        if account.min_password_length < 8:
            errors.append("минимальная длина пользовательского пароля должна быть не менее 8")

        remote = config.remote_access
        if remote.enabled and remote.method not in ("openssh", "vpn"):
            errors.append(f"неподдерживаемый метод удалённого доступа: {remote.method!r}")
        if remote.enabled and not (0 < remote.port <= 65535):
            errors.append("некорректный порт удалённого доступа")

        return ValidationResult(ok=not errors, errors=errors)


class ConfigValidationUnavailableError(RuntimeError):
    """Raised by ExternalCommandValidator when the external tool cannot be run
    ("Конфигурация.Проверка.Недоступна")."""


class ExternalCommandValidator:
    """Delegates validation to an external executable.

    The executable is invoked as `<command> <config-json-path>` and is
    expected to print a JSON object `{"ok": bool, "errors": [str, ...]}` on
    stdout and exit 0. This is the extension point mentioned in "Программные
    интерфейсы-2"; no specific external validator ships with this project."""

    def __init__(self, command: str):
        self.command = command

    def validate(self, config: InstallConfig) -> ValidationResult:
        if shutil.which(self.command) is None:
            raise ConfigValidationUnavailableError(f"внешнее средство проверки недоступно: {self.command}")

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config.to_dict()), encoding="utf-8")
            try:
                proc = subprocess.run(
                    [self.command, str(config_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConfigValidationUnavailableError(str(exc)) from exc

        try:
            payload = json.loads(proc.stdout)
            return ValidationResult(ok=bool(payload["ok"]), errors=list(payload.get("errors", [])))
        except (json.JSONDecodeError, KeyError) as exc:
            raise ConfigValidationUnavailableError(
                f"внешнее средство проверки вернуло некорректный ответ: {exc}"
            ) from exc
