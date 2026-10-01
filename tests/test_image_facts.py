"""Что образ знает о себе сам -- и что говорится, когда он не знает.

Проверяется не «функция вызвалась», а правило, ради которого модуль написан
(решение 16.09.2026): дал образ список -- поле ограничено этим списком; не дал
-- поле открыто, и рядом стоит **причина**. Пустой список без причины
неотличим от «в образе ничего нет», и именно это раньше и происходило.
"""
from __future__ import annotations

from portable_os_admin.admin import image_facts
from portable_os_admin.install import image_catalog
from portable_os_admin.install.image_source import ImageInfo


def _info(**kwargs) -> ImageInfo:
    base = dict(
        path="/tmp/image.iso",
        family="debian",
        release="trixie",
        architectures=("amd64",),
        label="Debian 13",
    )
    base.update(kwargs)
    return ImageInfo(**base)


def test_a_list_from_the_image_closes_the_field():
    """Список есть -- значит выбирать можно только из него."""
    choices = image_facts.Choices(values=("ru_RU.UTF-8", "en_US.UTF-8"))
    assert choices.known
    assert choices.values == ("ru_RU.UTF-8", "en_US.UTF-8")
    assert choices.reason == ""


def test_an_empty_list_always_carries_a_reason():
    """Пустота без объяснения -- это ложь «в образе ничего нет»."""
    choices = image_facts.Choices(reason="языки из образа не прочитать: нет rpm2cpio")
    assert not choices.known
    assert "rpm2cpio" in choices.reason


def test_a_missing_extraction_tool_is_named_not_swallowed(monkeypatch):
    """У РЕД ОС списки лежат внутри пакетов: без `rpm2cpio` их не достать.

    Это не поломка образа и не ошибка администратора -- это отсутствие
    инструмента на машине, и сказать надо именно так.
    """
    def raise_missing():
        raise image_catalog.ExtractionToolMissingError("нет rpm2cpio")

    result = image_facts._choices(raise_missing, "языки")
    assert not result.known
    assert "нет rpm2cpio" in result.reason
    assert "языки" in result.reason


def test_architectures_come_from_the_image_not_from_a_tuple():
    """В мастере стоял кортеж `("amd64", "arm64")`, а образ Debian несёт только
    `amd64`, и РЕД ОС зовёт свою архитектуру `x86_64`. Ни того, ни другого
    зашитый список не знает."""
    facts = image_facts.ImageFacts(info=_info(architectures=("amd64",)))
    assert facts.architectures.values == ("amd64",)

    redos = image_facts.ImageFacts(info=_info(family="redos", architectures=("x86_64",)))
    assert redos.architectures.values == ("x86_64",)


def test_architectures_without_an_image_explain_themselves():
    facts = image_facts.ImageFacts(failure="образ не опознан: файл не найден")
    assert not facts.architectures.known
    assert "не опознан" in facts.architectures.reason


def test_desktops_are_read_with_their_version():
    """Версия оболочки читается из образа, а не выводится из выпуска."""
    facts = image_facts.ImageFacts(info=_info(desktop_versions={"mate": "1.26.2-1.2"}))
    assert facts.desktops.values == ("mate",)
    assert facts.desktops.label_for("mate") == "mate 1.26.2-1.2"


def test_an_image_with_no_desktops_says_so():
    facts = image_facts.ImageFacts(info=_info(desktop_versions={}))
    assert not facts.desktops.known
    assert "оболочки" in facts.desktops.reason


def test_an_unreadable_image_never_raises(tmp_path):
    """Интерфейсу нужен не разбор исключения, а ответ: можно ли пользоваться и
    что сказать человеку."""
    facts = image_facts.read_image_facts(str(tmp_path / "нет-такого.iso"))
    assert not facts.usable
    assert facts.failure
    assert not facts.locales.known
    assert facts.locales.reason == "" or facts.locales.reason


def test_a_partial_failure_keeps_the_other_lists(monkeypatch):
    """Не достались раскладки -- языки и пояса всё равно должны приехать."""
    monkeypatch.setattr(image_facts, "probe", lambda path: _info())

    class _Image:
        mountpoint = "/mnt/fake"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(image_facts, "MountedImage", lambda path: _Image())
    monkeypatch.setattr(image_catalog, "locales", lambda m, f: ["ru_RU.UTF-8"])
    monkeypatch.setattr(image_catalog, "timezones", lambda m, f: ["Europe/Moscow"])
    monkeypatch.setattr(
        image_catalog, "keyboard_layouts",
        lambda m, f: (_ for _ in ()).throw(image_catalog.ExtractionToolMissingError("нет xkb")),
    )
    monkeypatch.setattr(image_catalog, "desktop_applications", lambda m, f: [])

    facts = image_facts.read_image_facts("/tmp/image.iso")

    assert facts.usable
    assert facts.locales.values == ("ru_RU.UTF-8",)
    assert facts.timezones.values == ("Europe/Moscow",)
    assert not facts.keyboard_layouts.known
    assert "нет xkb" in facts.keyboard_layouts.reason
    # Приложений в образе нет -- это тоже ответ, и он со своей причиной.
    assert not facts.applications.known
    assert facts.applications.reason


def test_launchers_are_asked_only_about_chosen_packages(monkeypatch):
    """Полный перебор образа занял бы минуты, а нужен ответ про десяток имён."""
    monkeypatch.setattr(image_facts, "probe", lambda path: _info())
    asked: list[list[str]] = []

    class _Image:
        mountpoint = "/mnt/fake"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(image_facts, "MountedImage", lambda path: _Image())
    for name in ("locales", "timezones", "desktop_applications"):
        monkeypatch.setattr(image_catalog, name, lambda m, f: [])
    monkeypatch.setattr(image_catalog, "keyboard_layouts", lambda m, f: [])

    def _launchers(mountpoint, family, packages):
        asked.append(list(packages))
        return {"mate-calc": ["mate-calc"]}

    monkeypatch.setattr(image_catalog, "launchers_by_package", _launchers)

    facts = image_facts.read_image_facts("/tmp/image.iso", packages=["mate-calc"])
    assert asked == [["mate-calc"]]
    assert facts.launchers == {"mate-calc": ["mate-calc"]}

    # Без выбранных пакетов образ об этом не спрашивают вовсе.
    asked.clear()
    image_facts.read_image_facts("/tmp/image.iso")
    assert asked == []
