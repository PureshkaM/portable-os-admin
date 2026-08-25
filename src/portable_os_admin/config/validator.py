"""Configuration validation.

Implements UC-3's validation step and "Конфигурация.Проверка.*". The spec
requires the check to go through "внешнее средство проверки конфигурации"
(Программные интерфейсы-2); `ExternalCommandValidator` models that as a
pluggable subprocess call, while `BuiltinConfigValidator` is the default
implementation used when no external tool is configured, so the application
is usable standalone.
"""
from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Protocol

from .schema import (
    LUKS_CIPHERS,
    LUKS_KDFS,
    SUPPORTED_ARCH,
    SUPPORTED_DESKTOPS,
    SUPPORTED_SESSION_TYPES,
    SUPPORTED_TARGET_OS,
    InstallConfig,
)

_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: A desktop-entry name as RED OS's `kiosk-mode-on -a` takes it: the basename
#: of a file in /usr/share/applications without the ".desktop" suffix. No path
#: separators -- the name is interpolated into shell inside the target, and a
#: name that cannot contain a slash or a quote cannot escape it.
_DESKTOP_ENTRY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")

#: Resolved at import, on the main thread. importlib.resources performs a lazy
#: import and races the import lock when first called from a worker thread --
#: which aborted the interpreter outright when the installer did it that way.
KICKSTART_RUNNER = str(
    importlib.resources.files("portable_os_admin.config").joinpath("kickstart_runner.py")
)

#: Families whose automated-install format is kickstart, and whose syntax
#: versions pykickstart names RHEL<major>. Used to *ask* for a version; when
#: the parser does not implement it, the runner falls back to the newest one
#: it does, so a wrong guess degrades instead of failing.
KICKSTART_SYNTAX_PREFIX = {"redos": "RHEL", "rhel": "RHEL", "centos": "RHEL", "almalinux": "RHEL"}


def kickstart_syntax_version(family: str, release: str) -> str:
    """"RHEL8" from ("redos", "8.0") -- derived from the image, not fixed here."""
    prefix = KICKSTART_SYNTAX_PREFIX.get((family or "").strip().lower())
    major = (release or "").strip().split(".")[0]
    if not prefix or not major.isdigit():
        return ""
    return f"{prefix}{major}"


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

        errors += self._kiosk_errors(config)
        errors += self._auto_update_errors(config)

        return ValidationResult(ok=not errors, errors=errors)

    def _auto_update_errors(self, config: InstallConfig) -> list[str]:
        errors: list[str] = []
        settings = getattr(config, "auto_update", None)
        if settings is None or not settings.enabled:
            return errors

        # The one check that matters. The installation image lives on the
        # administrator's machine and is gone once the stick is unplugged, so a
        # medium with no mirror has no package source at all: the update
        # service would install cleanly and then fail on every boot for the
        # life of the medium, writing errors to a log nobody reads.
        if not config.mirror:
            errors.append(
                "автообновление включено, но не задан адрес репозитория: "
                "установочный образ на носителе недоступен, обновляться будет неоткуда"
            )

        if settings.delay_minutes < 0:
            errors.append("задержка перед автообновлением не может быть отрицательной")

        return errors

    def _kiosk_errors(self, config: InstallConfig) -> list[str]:
        """Checks specific to the restricted desktop.

        All of these are things that produce a medium which installs cleanly
        and is then useless or unsafe -- exactly the class of defect this
        project has been bitten by before, where a configuration validated,
        installed for an hour, and only revealed the problem on the medium.
        """
        errors: list[str] = []
        kiosk = getattr(config, "kiosk", None)
        if kiosk is None or not kiosk.enabled:
            return errors

        # Which families have the mode is a property of the backend, not a list
        # repeated here, so adding a backend cannot leave this check stale.
        try:
            from ..install.backends import get_backend

            supported = bool(get_backend(config.target_os).display_manager_service)
        except Exception:  # noqa: BLE001 - an unknown OS is already reported above
            supported = False
        if not supported:
            errors.append(
                f"режим киоска не поддерживается для {config.target_os!r}: "
                "реализован и проверен только для debian и redos"
            )

        desktop = getattr(kiosk, "desktop_environment", "") or ""
        if desktop not in SUPPORTED_DESKTOPS:
            errors.append(
                f"неподдерживаемая рабочая среда киоска: {desktop!r} "
                f"(ожидается одно из {', '.join(SUPPORTED_DESKTOPS)})"
            )

        session_type = getattr(kiosk, "session_type", "") or ""
        if session_type not in SUPPORTED_SESSION_TYPES:
            errors.append(
                f"неподдерживаемый тип сеанса: {session_type!r} "
                f"(ожидается одно из {', '.join(SUPPORTED_SESSION_TYPES)})"
            )
        elif session_type == "wayland":
            # MATE has no Wayland session at all, and RED OS 8's image carries
            # Plasma 5.27 without `startplasma-wayland` -- checked against the
            # image's own file list. Refusing here beats a medium that installs
            # cleanly and then has no session to start.
            if desktop == "mate":
                errors.append(
                    "MATE не умеет работать под Wayland: выберите X11 или рабочую среду KDE"
                )
            elif config.target_os == "redos":
                errors.append(
                    "в образе РЕД ОС нет Wayland-сеанса Plasma (есть только startplasma-x11): "
                    "для РЕД ОС выберите X11"
                )

        if not kiosk.allowed_apps:
            errors.append(
                "в режиме киоска должно быть разрешено хотя бы одно приложение: "
                "иначе пользователь получит рабочий стол, с которого нечего запустить"
            )

        # A desktop-entry name, not a package name and not a path. The same
        # spelling RED OS's kiosk-mode-on takes.
        bad = [a for a in kiosk.allowed_apps if not _DESKTOP_ENTRY_RE.match(a)]
        if bad:
            errors.append(
                "недопустимые имена разрешённых приложений: "
                f"{', '.join(repr(a) for a in bad)} — ожидается имя desktop-записи "
                "без «.desktop» и без пути, например 'mate-calc'"
            )

        if kiosk.autostart_app and kiosk.autostart_app not in kiosk.allowed_apps:
            errors.append(
                f"приложение автозапуска {kiosk.autostart_app!r} не входит в список "
                "разрешённых: оно было бы скрыто сразу после установки"
            )

        if kiosk.screen_blank_minutes < 0:
            errors.append("время до гашения экрана не может быть отрицательным")

        if kiosk.firejail_options and not kiosk.firejail:
            errors.append(
                "заданы параметры firejail, но сама песочница выключена"
            )

        return errors


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

    The parser is preferably taken from the installation image itself (see
    image_validator.py): it is the version that image's own installer uses, and
    for the RHEL family it is the only copy available at all on a Debian
    workstation. Only when no image is at hand does this fall back to a tool
    installed on the administrator's machine.

    When neither exists the result says so rather than quietly passing --
    pretending a configuration was checked would be worse than admitting it
    was not.
    """

    def __init__(self, image_mountpoint: str = "", image_family: str = "", image_release: str = ""):
        #: A mounted installation image to take the parser from. Empty means
        #: "no image chosen yet", which is the normal case while editing a
        #: configuration, before any image has been picked.
        self.image_mountpoint = image_mountpoint
        self.image_family = image_family
        self.image_release = image_release

    def validate(self, config: InstallConfig) -> ValidationResult:
        from . import native

        fmt, text = native.render(config)
        if fmt == "kickstart" and self.image_mountpoint:
            return self._validate_from_image(text)
        tool, argv, verdict_from = NATIVE_VALIDATORS[fmt]
        if shutil.which(tool) is None:
            raise ConfigValidationUnavailableError(
                f"средство проверки {tool} не установлено, поэтому конфигурация проверена "
                "только внутренними правилами. "
                + (
                    "pykickstart нет в репозиториях Debian. Проверка станет возможной, "
                    "когда будет выбран установочный образ: разбор берётся с него самого."
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


    def _validate_from_image(self, text: str) -> ValidationResult:
        """Runs the parser unpacked from the image, in its own interpreter.

        Out of process on purpose: it is third-party code of an unknown
        version, it needs compatibility shims to import on newer Pythons, and
        a bad enough input can end the process it runs in. None of that may
        take the application down with it.
        """
        import tempfile
        from pathlib import Path

        from . import image_validator

        with tempfile.TemporaryDirectory(prefix="poa-ksval-") as tmp:
            config_path = Path(tmp) / "config.ks"
            config_path.write_text(text, encoding="utf-8")
            unpacked = Path(tmp) / "parser"
            try:
                search_path = image_validator.prepare(self.image_mountpoint, str(unpacked))
            except image_validator.ValidatorNotOnImageError as exc:
                raise ConfigValidationUnavailableError(
                    f"{exc}. Конфигурация проверена только внутренними правилами."
                ) from exc

            env = dict(os.environ, PYTHONPATH=search_path, PYTHONDONTWRITEBYTECODE="1")
            wanted = kickstart_syntax_version(self.image_family, self.image_release)
            try:
                proc = subprocess.run(
                    [sys.executable, KICKSTART_RUNNER, str(config_path), wanted],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=env,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ConfigValidationUnavailableError(
                    f"не удалось запустить разбор конфигурации: {exc}"
                ) from exc

        report = {}
        for line in reversed(proc.stdout.splitlines()):
            if line.strip().startswith("{"):
                try:
                    report = json.loads(line)
                except ValueError:
                    report = {}
                break
        if not report:
            raise ConfigValidationUnavailableError(
                "разбор конфигурации средствами образа не дал результата "
                f"(код {proc.returncode}); конфигурация проверена только внутренними правилами"
            )
        if not report.get("available", False):
            raise ConfigValidationUnavailableError(
                str(report.get("reason", "средство проверки с образа недоступно"))
                + "; конфигурация проверена только внутренними правилами"
            )
        if report.get("ok"):
            return ValidationResult(ok=True, errors=[])
        version = report.get("version", "")
        label = f"pykickstart с образа ({version})" if version else "pykickstart с образа"
        return ValidationResult(
            ok=False,
            errors=[f"{label}: {message}" for message in report.get("errors", [])]
            or [f"{label} отклонил конфигурацию"],
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
