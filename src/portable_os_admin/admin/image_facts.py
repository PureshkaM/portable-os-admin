"""Что установочный образ знает о себе сам.

Одна точка, из которой интерфейс берёт **всё**, что иначе пришлось бы вводить
по памяти: семейство и выпуск, архитектуры, поколения графических оболочек,
языки, часовые пояса, раскладки, приложения и запускатели.

Зачем отдельным модулем, а не прямо в мастере. Мастер конфигурации держал эти
значения кортежами в коде -- `("ru_RU.UTF-8", "en_US.UTF-8")`, `("us", "ru")`,
четыре часовых пояса наугад, -- а списки из образа подставлялись только если
администратор нажмёт «Прочитать из образа…», причём кнопка эта жила на шаге
пакетов, а не там, где выбирают образ. То есть правда о дистрибутиве была делом
добровольным и лежала не на своём месте.

Главное правило, ради которого модуль и написан (решение 16.09.2026):

* **дал образ список -- поле ограничено этим списком**: значение, которого в
  дистрибутиве нет, выбрать нельзя, потому что носитель с ним не соберётся;
* **не дал (нет инструмента распаковки, нет пакета, образ не смонтировался) --
  поле открыто, и рядом стоит причина**. Запрет применяется к знанию, а не к
  незнанию: пустой список выглядит как «в образе ничего нет», и это ложь.

Поэтому каждый список едет вместе со своей причиной -- см. `Choices`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..install import image_catalog
from ..install.image_source import ImageInfo, MountedImage, probe


@dataclass(frozen=True)
class Choices:
    """Список значений и -- если списка нет -- причина, по которой его нет.

    `values` пуст ровно тогда, когда образ не смог ответить. Интерфейс по этому
    признаку и решает, запирать поле списком или открывать ввод: пустой список
    без причины неотличим от «в образе ничего нет», а с причиной -- это
    «спросить было нечем, решай сам».
    """

    values: tuple[str, ...] = ()
    reason: str = ""

    #: Пары «значение -- человеческое имя» там, где они есть (раскладки).
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        return bool(self.values)

    def label_for(self, value: str) -> str:
        return self.labels.get(value, value)


@dataclass(frozen=True)
class ImageFacts:
    """Всё, что удалось узнать об образе за одно монтирование.

    Монтирование стоит дорого (права, точка монтирования, размонтирование в
    любом исходе), поэтому читается всё сразу и один раз, а не по полю за раз.
    """

    info: "ImageInfo | None" = None
    locales: Choices = field(default_factory=Choices)
    timezones: Choices = field(default_factory=Choices)
    keyboard_layouts: Choices = field(default_factory=Choices)
    applications: Choices = field(default_factory=Choices)
    #: Запускатели выбранных пакетов: имя пакета -> список `.desktop`.
    launchers: dict[str, list[str]] = field(default_factory=dict)
    #: Почему не удалось прочитать образ целиком. Пусто, если удалось.
    failure: str = ""

    @property
    def usable(self) -> bool:
        return self.info is not None

    @property
    def architectures(self) -> Choices:
        """Архитектуры, под которые в образе есть пакеты.

        Раньше в мастере стоял кортеж `("amd64", "arm64")` -- список, который
        разойдётся с дистрибутивом на следующем выпуске и уже сейчас врёт про
        образ, несущий только одну архитектуру.
        """
        if self.info is None:
            return Choices(reason=self.failure or "образ ещё не выбран")
        if not self.info.architectures:
            return Choices(reason="образ не назвал своих архитектур")
        return Choices(values=tuple(self.info.architectures))

    @property
    def desktops(self) -> Choices:
        """Графические оболочки, найденные в индексе образа.

        Поколение читается из образа (`desktop_versions`), а не выводится из
        выпуска: Debian 13 несёт Plasma 6, Debian 12 -- Plasma 5, и правило
        «такой-то выпуск значит такое-то поколение» пришлось бы обновлять к
        каждому релизу.
        """
        if self.info is None:
            return Choices(reason=self.failure or "образ ещё не выбран")
        found = tuple(sorted(self.info.desktop_versions))
        if not found:
            return Choices(reason="в индексе образа не найдено ни одной оболочки")
        labels = {
            name: f"{name} {version}"
            for name, version in self.info.desktop_versions.items()
        }
        return Choices(values=found, labels=labels)


def read_image_facts(path: str, packages: "list[str] | None" = None) -> ImageFacts:
    """Читает образ целиком: факты о нём и все списки за одно монтирование.

    Ошибки не поднимаются наружу. Интерфейсу нужен не разбор исключения, а
    ответ на два вопроса: можно ли этим образом пользоваться и что сказать
    человеку, если нельзя. Поэтому причина едет строкой в `failure`, а
    частичная неудача (образ смонтирован, но, скажем, `rpm2cpio` не нашёлся)
    оставляет остальные списки на месте.
    """
    try:
        info = probe(path)
    except Exception as exc:  # noqa: BLE001 -- причина важнее типа
        return ImageFacts(failure=f"образ не опознан: {exc}")

    family = info.family
    wanted = list(packages or [])
    try:
        with MountedImage(path) as image:
            locales = _choices(
                lambda: image_catalog.locales(image.mountpoint, family),
                "языки",
            )
            timezones = _choices(
                lambda: image_catalog.timezones(image.mountpoint, family),
                "часовые пояса",
            )
            layouts = _layout_choices(image.mountpoint, family)
            applications = _choices(
                lambda: image_catalog.desktop_applications(image.mountpoint, family),
                "приложения",
            )
            launchers: dict[str, list[str]] = {}
            if wanted:
                try:
                    launchers = image_catalog.launchers_by_package(
                        image.mountpoint, family, wanted
                    )
                except Exception:  # noqa: BLE001 -- запускатели необязательны
                    launchers = {}
    except Exception as exc:  # noqa: BLE001
        return ImageFacts(info=info, failure=f"образ не смонтировался: {exc}")

    return ImageFacts(
        info=info,
        locales=locales,
        timezones=timezones,
        keyboard_layouts=layouts,
        applications=applications,
        launchers=launchers,
    )


def _choices(read_values, what: str) -> Choices:
    """Список из образа либо причина, по которой его нет.

    Исключение здесь -- обычное дело: у РЕД ОС списки лежат внутри пакетов, и
    без `rpm2cpio` их не достать. Это не поломка образа и не ошибка
    администратора, это отсутствие инструмента на машине -- так и надо сказать.
    """
    try:
        values = tuple(read_values())
    except image_catalog.ExtractionToolMissingError as exc:
        return Choices(reason=f"{what} из образа не прочитать: {exc}")
    except Exception as exc:  # noqa: BLE001
        return Choices(reason=f"{what} из образа не прочитать: {exc}")
    if not values:
        return Choices(reason=f"образ не назвал ни одного значения ({what})")
    return Choices(values=values)


def _layout_choices(mountpoint: str, family: str) -> Choices:
    """Раскладки: у них, в отличие от прочего, есть человеческие имена."""
    try:
        pairs = image_catalog.keyboard_layouts(mountpoint, family)
    except Exception as exc:  # noqa: BLE001
        return Choices(reason=f"раскладки из образа не прочитать: {exc}")
    if not pairs:
        return Choices(reason="образ не назвал ни одной раскладки")
    return Choices(
        values=tuple(code for code, _name in pairs),
        labels={code: name for code, name in pairs},
    )
