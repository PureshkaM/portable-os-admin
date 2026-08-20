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

from .schema import LUKS_CIPHERS, LUKS_KDFS, SUPPORTED_ARCH, SUPPORTED_TARGET_OS, InstallConfig

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
        if disk.esp_partition_mb < 128:
            errors.append("размер EFI-раздела должен быть не менее 128 МБ")
        # A kernel plus its initramfs is ~100 MB on both families, and package
        # upgrades keep several generations around; anything under 512 MB fills
        # up and breaks the next kernel update on the medium.
        if disk.boot_partition_mb < 512:
            errors.append("размер раздела /boot должен быть не менее 512 МБ")
        if disk.log_partition_mb < 64:
            errors.append("размер раздела логов должен быть не менее 64 МБ")
        if disk.exchange_partition_mb < 64:
            errors.append("размер раздела обмена должен быть не менее 64 МБ")

        if config.arch and config.arch not in SUPPORTED_ARCH:
            errors.append(
                f"неподдерживаемая архитектура: {config.arch!r} "
                f"(ожидается одно из {SUPPORTED_ARCH} или пусто — как у текущей машины)"
            )

        luks = config.luks
        if luks.cipher not in LUKS_CIPHERS:
            errors.append(
                f"неподдерживаемый шифр LUKS: {luks.cipher!r} "
                f"(ожидается одно из {', '.join(LUKS_CIPHERS)})"
            )
        else:
            allowed = LUKS_CIPHERS[luks.cipher]
            if luks.key_size_bits not in allowed:
                errors.append(
                    f"шифр {luks.cipher} не принимает ключ {luks.key_size_bits} бит "
                    f"(допустимо: {', '.join(str(s) for s in allowed)})"
                )
        if luks.kdf not in LUKS_KDFS:
            errors.append(
                f"неподдерживаемая функция формирования ключа: {luks.kdf!r} "
                f"(ожидается одно из {', '.join(LUKS_KDFS)})"
            )
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
        # "vpn" used to be accepted here and offered in the UI, but nothing ever
        # implemented it: the install configured a remote-access method only for
        # "openssh", so choosing vpn produced a config that validated, installed
        # cleanly, and left the medium with no remote access whatsoever.
        if remote.enabled and remote.method != "openssh":
            errors.append(
                f"неподдерживаемый метод удалённого доступа: {remote.method!r} "
                "(реализован только openssh)"
            )
        if remote.enabled and not (0 < remote.port <= 65535):
            errors.append("некорректный порт удалённого доступа")

        return ValidationResult(ok=not errors, errors=errors)


#: The official validator each family ships for its own automated-install file.
#: Both are the distribution's own tool, not something written here.
NATIVE_VALIDATORS = {
    # pykickstart. Parses the whole file: removed and deprecated commands,
    # missing required options, mutually exclusive options, some partitioning
    # errors. Ships with the RHEL family; absent from Debian's archive.
    # Verdict is the exit status: ksvalidator exits non-zero on a bad file.
    "kickstart": ("ksvalidator", ["ksvalidator"], "status"),
    # debconf. Checks that every line names an owner, a question, a type and a
    # value -- the file's format, not the sense of its values.
    # Verdict is the *output*, not the exit status. Checked rather than
    # assumed: given a malformed line, debconf-set-selections prints
    # "warning: Unknown type ..., skipping line 2" and still exits 0, so a
    # validator that trusted the return code would silently pass everything.
    # Its documentation agrees -- no output means the file was accepted.
    "preseed": ("debconf-set-selections", ["debconf-set-selections", "--checkonly"], "output"),
}


class NativeToolValidator:
    """Validates by rendering the configuration into the distribution's own
    automated-install format and running the distribution's own validator.

    This is the "внешнее средство проверки конфигурации" the specification
    calls for. It is deliberately not the only check: it knows the
    distribution's rules and nothing about this project's own (that /boot must
    live outside the LUKS volume, which key sizes a cipher accepts), so
    validation runs it alongside BuiltinConfigValidator.

    When the tool is missing the result says so rather than quietly passing:
    pykickstart is not packaged for Debian, so a RedOS configuration authored on
    a Debian workstation genuinely cannot be checked this way, and pretending
    otherwise would be worse than admitting it.
    """

    def validate(self, config: InstallConfig) -> ValidationResult:
        from . import native

        fmt, text = native.render(config)
        tool, argv, verdict_from = NATIVE_VALIDATORS[fmt]
        if shutil.which(tool) is None:
            raise ConfigValidationUnavailableError(
                f"средство проверки {tool} не установлено, поэтому конфигурация проверена "
                "только внутренними правилами. "
                + (
                    "pykickstart нет в репозиториях Debian — конфигурации для RedOS этим "
                    "средством проверяются на машине с RedOS."
                    if fmt == "kickstart"
                    else "Установите пакет, который его предоставляет."
                )
            )

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("config.ks" if fmt == "kickstart" else "config.preseed")
            path.write_text(text, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [*argv, str(path)], capture_output=True, text=True, timeout=60, check=False
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConfigValidationUnavailableError(f"не удалось запустить {tool}: {exc}") from exc
            details = "\n".join(part for part in (proc.stderr, proc.stdout) if part).strip()
            details = details.replace(str(path), f"<{fmt}>")

        rejected = bool(details) if verdict_from == "output" else proc.returncode != 0
        if not rejected:
            return ValidationResult(ok=True, errors=[])
        return ValidationResult(
            ok=False,
            errors=[f"{tool}: {line.strip()}" for line in details.splitlines() if line.strip()]
            or [f"{tool} отклонил конфигурацию (код {proc.returncode})"],
        )


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
