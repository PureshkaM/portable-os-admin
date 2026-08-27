"""Что именно должна уметь рабочая среда, чтобы её можно было запереть.

Три вещи, и границы между ними проведены по тому, где живёт ограничение:

* `files()` -- настройки, которые пишутся на носитель как файлы;
* `apply_fragment()` -- то, что можно сделать только на целевой системе, где
  видно, какие приложения и модули на самом деле установлены;
* `desktop_shortcuts_work()` -- ответ на вопрос, имеет ли смысл класть ярлыки
  на рабочий стол; под KDE он зависит от того, действует ли запрет
  run_desktop_files.

Интерфейс появился не ради симметрии. До него всё это лежало в одном файле, и
каждая функция знала про обе среды сразу: `if DESKTOP = mate ... else ...` --
и в Python, и внутри генерируемого shell-скрипта. Из шести дефектов, найденных
живыми прогонами 27.08.2026, четыре были одного вида: правило написано для
одной среды и молча применено к другой. `splash` вместо `rhgb`,
`kscreensaverrc` вместо `kscreenlockerrc`, запрет модулей вместо удаления,
ярлыки вместо панели -- каждый раз ошибка была не в логике, а в том, что для
неё не было отдельного места.

Debian 13 несёт Plasma 6, где другие пути (`kservices6`), другие инструменты
(`kcmshell6`) и другое меню. Он добавляется здесь новым классом, а не новой
веткой внутри существующих.
"""
from __future__ import annotations

from ....config.schema import InstallConfig
from ..common import DESKTOP_KDE, DESKTOP_MATE, desktop_of


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

    def desktop_shortcuts_work(self, config: InstallConfig) -> bool:
        """Запустится ли ярлык, положенный на рабочий стол.

        Ярлык, который виден и не работает, хуже отсутствующего, поэтому
        ответ «нет» означает, что ярлыки не кладутся вовсе.
        """
        return True


def for_config(config: InstallConfig) -> DesktopEnvironment:
    """Среда, которой строится этот киоск.

    Пока выбор идёт по одному полю конфигурации. Когда программа научится
    читать версию оболочки из образа, здесь появится второй параметр, и
    Plasma 5 с Plasma 6 разойдутся по своим классам -- ради этого функция и
    отдельная, а не `if` по месту использования.
    """
    from .kde import Kde5Environment
    from .mate import MateEnvironment

    if desktop_of(config) == DESKTOP_KDE:
        return Kde5Environment()
    return MateEnvironment()


__all__ = ["DesktopEnvironment", "for_config", "DESKTOP_KDE", "DESKTOP_MATE"]
