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

    #: Чем ярлык на рабочем столе должен быть: копией записи или ссылкой на неё.
    #:
    #: Умолчание -- копия: так ярлык переживает удаление пакета, а MATE
    #: запускает его, если файл исполняемый и помечен доверенным. KDE считает
    #: иначе, поэтому у него ответ свой -- см. `Kde5Environment`.
    desktop_shortcuts_are_links = False


def for_config(config: InstallConfig, image=None) -> DesktopEnvironment:
    """Среда, которой строится этот киоск.

    Выбирается парой «что просит конфигурация» и «что несёт образ». Поколение
    берётся из образа, а не выводится из выпуска дистрибутива: РЕД ОС 8 несёт
    Plasma 5.27, Debian 13 -- Plasma 6.3, и правило «такой-то выпуск значит
    такое-то поколение» пришлось бы обновлять к каждому релизу. Проверено
    чтением обоих образов.

    Без образа берётся поколение по умолчанию. Это не «наугад»: без образа
    программа ничего и не собирает, а такой вызов бывает у тестов и у
    предпросмотра конфигурации, где важен состав ограничений, а не точная
    версия.
    """
    from .kde import Kde5Environment
    from .mate import MateEnvironment

    if desktop_of(config) != DESKTOP_KDE:
        return MateEnvironment()

    major = _desktop_major(image, "kde")
    if major >= 6:
        from .kde6 import Kde6Environment

        return Kde6Environment()
    return Kde5Environment()


def _desktop_major(image, desktop: str) -> int:
    """Поколение оболочки, которое несёт образ, или 0, если оно неизвестно."""
    if image is None:
        return 0
    from ...image_source import desktop_major

    return desktop_major(getattr(image, "desktop_versions", {}).get(desktop, ""))


__all__ = ["DesktopEnvironment", "for_config", "DESKTOP_KDE", "DESKTOP_MATE"]
