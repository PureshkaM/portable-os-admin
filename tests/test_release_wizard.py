"""Единый мастер выпуска: путь выводится из источника, а не спрашивается.

Проверяется ядро слияния -- правило ветвления. Оно смотрит ровно на два поля,
поэтому проверяется без окна: Tk здесь ничего не решает, а решает таблица
«источник -> шаги».

Главное обещание слияния закреплено тестом, а не намерением: **в каждой ветви
ровно пять шагов**, столько же, сколько было в отдельном мастере. Слияние,
удлиняющее работу, ухудшает то, ради чего затевалось, и заметить это на глаз
невозможно -- шаги добавляются по одному и каждый выглядит оправданным.
"""
from __future__ import annotations

from types import SimpleNamespace

from portable_os_admin.gui.screens.release_wizard import (
    SOURCE_DISTRIBUTION,
    SOURCE_IMAGE,
    TARGET_DISTRIBUTION,
    TARGET_MEDIUM,
    ReleaseWizardFrame,
)


def _keys(source_kind: str, target: str = TARGET_MEDIUM) -> "list[str]":
    """План шагов для такого источника -- без окна: правило смотрит два поля."""
    state = SimpleNamespace(source_kind=source_kind, target=target)
    return [key for key, _title, _hint in ReleaseWizardFrame._plan(state)]


def _titles(source_kind: str, target: str = TARGET_MEDIUM) -> "list[str]":
    state = SimpleNamespace(source_kind=source_kind, target=target)
    return [title for _key, title, _hint in ReleaseWizardFrame._plan(state)]


def test_an_image_written_straight_to_a_medium():
    assert _keys(SOURCE_IMAGE) == ["source", "config", "device", "passphrase", "confirm"]


def test_an_image_turned_into_a_distribution_for_a_batch():
    assert _keys(SOURCE_IMAGE, TARGET_DISTRIBUTION) == [
        "source",
        "config",
        "packages",
        "destination",
        "run",
    ]


def test_a_ready_distribution_goes_straight_to_the_medium():
    """Конфигурация не спрашивается: она уже в описании дистрибутива."""
    keys = _keys(SOURCE_DISTRIBUTION)
    assert keys == ["source", "device", "passphrase", "handover", "confirm"]
    assert "config" not in keys


def test_every_branch_is_five_steps_long():
    """Столько же, сколько было в отдельном мастере.

    Условие, а не совпадение. Шаг «куда» поэтому живёт на шаге источника, а имя
    и владелец спрашиваются на шаге пароля там, где файлов раздела обмена не
    бывает: каждое из этих решений экономит ровно один шаг.
    """
    for source, target in (
        (SOURCE_IMAGE, TARGET_MEDIUM),
        (SOURCE_IMAGE, TARGET_DISTRIBUTION),
        (SOURCE_DISTRIBUTION, TARGET_MEDIUM),
    ):
        assert len(_keys(source, target)) == 5, f"ветвь {source}/{target} длиннее пяти шагов"


def test_the_source_is_always_asked_first():
    """Путь выпуска не спрашивается вовсе -- он выводится из источника."""
    for source, target in (
        (SOURCE_IMAGE, TARGET_MEDIUM),
        (SOURCE_IMAGE, TARGET_DISTRIBUTION),
        (SOURCE_DISTRIBUTION, TARGET_MEDIUM),
        ("", TARGET_MEDIUM),
    ):
        assert _keys(source, target)[0] == "source"


def test_the_handover_step_exists_where_exchange_files_do():
    """Файлы в раздел обмена принимает только развёртывание дистрибутива --
    установщик берёт их в `deploy_distribution` и не берёт в `run`. Шаг «Кому и
    что» поэтому есть ровно там, и в других ветвях имя с владельцем
    спрашиваются на шаге пароля."""
    assert "handover" in _keys(SOURCE_DISTRIBUTION)
    assert "handover" not in _keys(SOURCE_IMAGE)
    assert "handover" not in _keys(SOURCE_IMAGE, TARGET_DISTRIBUTION)


def test_the_handover_step_comes_before_the_writing_starts():
    keys = _keys(SOURCE_DISTRIBUTION)
    assert keys.index("handover") < keys.index("confirm")


def test_the_step_titles_tests_rely_on_are_kept():
    """Заголовки шагов -- договор с набором: помощник `_advance_to` ходит по
    ним намеренно, потому что счёт нажатий ломается от вставленного шага."""
    assert "Конфигурация" in _titles(SOURCE_IMAGE)
    assert "Носитель" in _titles(SOURCE_IMAGE)
    assert "Подтверждение и запись" in _titles(SOURCE_IMAGE)
    assert "Кому и что" in _titles(SOURCE_DISTRIBUTION)
    assert "Свои пакеты" in _titles(SOURCE_IMAGE, TARGET_DISTRIBUTION)
    assert "Куда сохранить" in _titles(SOURCE_IMAGE, TARGET_DISTRIBUTION)
    assert "Сборка" in _titles(SOURCE_IMAGE, TARGET_DISTRIBUTION)


def test_every_key_finds_its_builder():
    """Опечатка в ключе даёт мастер, падающий на этом шаге у администратора.

    Пока шаги были списком ссылок на функции, такого расхождения не бывало:
    список ссылался прямо на них. Раздача по имени ключа это допускает, значит
    проверяется весь набор ключей всех ветвей.
    """
    seen = set()
    for source, target in (
        (SOURCE_IMAGE, TARGET_MEDIUM),
        (SOURCE_IMAGE, TARGET_DISTRIBUTION),
        (SOURCE_DISTRIBUTION, TARGET_MEDIUM),
    ):
        seen.update(_keys(source, target))
    missing = [key for key in sorted(seen) if not hasattr(ReleaseWizardFrame, f"_build_step_{key}")]
    assert not missing, f"ключи без строителя: {missing}"
