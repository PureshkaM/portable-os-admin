"""Справочник конфигурации должен описывать всё, что есть в программе.

Документация, которую никто не проверяет, расходится с кодом за две недели, и
дальше вредит: администратор читает про поле, которого нет, или не находит
того, которое есть. Поэтому полнота `docs/CONFIG.md` — не вопрос
добросовестности, а тест.
"""
from __future__ import annotations

import dataclasses
import pathlib
import re

from portable_os_admin.config.schema import InstallConfig

REFERENCE = pathlib.Path(__file__).resolve().parents[1] / "docs" / "CONFIG.md"


def _fields(cls, prefix: str = "") -> list[str]:
    """Все поля конфигурации точечными именами: `kiosk.allowed_apps`."""
    names: list[str] = []
    for field in dataclasses.fields(cls):
        default = None
        if field.default is not dataclasses.MISSING:
            default = field.default
        elif field.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            default = field.default_factory()  # type: ignore[misc]
        if dataclasses.is_dataclass(type(default)):
            names += _fields(type(default), f"{prefix}{field.name}.")
        else:
            names.append(f"{prefix}{field.name}")
    return names


def test_every_configuration_field_is_documented():
    text = REFERENCE.read_text(encoding="utf-8")
    missing = [name for name in _fields(InstallConfig) if f"`{name}`" not in text]

    assert not missing, (
        "в docs/CONFIG.md нет описания полей: " + ", ".join(missing)
        + ".\nЛибо опишите их, либо уберите из схемы — незадокументированное "
        "поле администратор не найдёт."
    )


def test_the_reference_does_not_describe_fields_that_no_longer_exist():
    """Обратная сторона: удалённое поле должно исчезнуть и из справочника."""
    text = REFERENCE.read_text(encoding="utf-8")
    known = set(_fields(InstallConfig))
    # Имена в таблицах: строки вида `| `поле` | тип |`.
    documented = set(re.findall(r"^\| `([a-z_][a-z0-9_.]*)` \|", text, re.M))

    stale = sorted(documented - known)
    assert not stale, "справочник описывает несуществующие поля: " + ", ".join(stale)


def test_the_reference_says_what_the_file_is_not():
    """Важная оговорка: установка выполняется не этим файлом.

    Без неё администратор решит, что программа передаёт конфигурацию штатному
    установщику дистрибутива, и будет искать в ней возможности kickstart.
    """
    text = REFERENCE.read_text(encoding="utf-8")

    assert "kickstart" in text and "preseed" in text
    assert "установка выполняется не этим файлом" in text.lower()


def test_the_reference_admits_what_cannot_be_configured_yet():
    """Список зашитого — часть справочника, а не умолчание.

    Администратор должен узнать про предел из документации, а не наткнувшись
    на него посреди работы.
    """
    text = REFERENCE.read_text(encoding="utf-8")

    assert "Чего пока нельзя задать" in text
    assert "system_tasks" in text.split("Чего пока нельзя задать")[1]
