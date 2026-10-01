"""Что именно должна уметь рабочая среда, чтобы её можно было запереть.

Две вещи, и граница между ними проведена по тому, где живёт ограничение:

* `files()` -- настройки, которые пишутся на носитель как файлы;
* `apply_fragment()` -- то, что можно сделать только на целевой системе, где
  видно, какие приложения и модули на самом деле установлены.

Интерфейс появился, когда сред было две (MATE и KDE Plasma): правило,
написанное для одной, молча применялось к другой. KDE упразднён 01.10.2026
(LIVE-2.0), среда осталась одна, но граница «общее -- в common.py, своё --
здесь» сохранена: она по-прежнему отделяет то, что запирает систему, от того,
что запирает оболочку.
"""
from __future__ import annotations

from ....config.schema import InstallConfig
from ..common import DESKTOP_MATE


class DesktopEnvironment:
    """Одна рабочая среда одной версии."""

    #: Как среда называется в конфигурации.
    name: str = ""

    def files(self, config: InstallConfig) -> dict[str, tuple[str, int | None]]:
        """Путь относительно корня -> (содержимое, права или None)."""
        raise NotImplementedError

    def apply_fragment(self, config: InstallConfig) -> str:
        """Кусок shell, выполняемый на целевой системе после установки пакетов.

        Пустая строка -- законный ответ: среде может быть нечего доделывать.
        """
        return ""


def for_config(config: InstallConfig, image=None) -> DesktopEnvironment:
    """Среда, которой строится этот киоск.

    Среда одна -- MATE. Параметры сохранены, чтобы вызывающим не пришлось
    меняться: образ здесь больше ни на что не влияет.
    """
    from .mate import MateEnvironment

    return MateEnvironment()


__all__ = ["DesktopEnvironment", "for_config", "DESKTOP_MATE"]
