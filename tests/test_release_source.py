"""Источник читается сам, а не по доброй воле нажавшего кнопку.

Найдено живым прогоном 17.09.2026. Путь к образу, набранный в поле или
подставленный из запомненного, оставлял мастер слепым: `image_info` пустой, и
на шаге конфигураций все строки получали «образ не прочитан — проверка при
установке». Отбор по пригодности молча выключался, Debian-конфигурация
предлагалась наравне с РЕД ОС для образа РЕД ОС, а отказ приходил внутри
установки -- когда носитель уже стёрт.

Проверяется граница `_accept_image`: она смотрит два поля и зовёт чтение, и Tk
здесь ничего не решает, поэтому вызывается на заглушке.
"""
from __future__ import annotations

from types import SimpleNamespace

from portable_os_admin.gui.screens import release_wizard
from portable_os_admin.gui.screens.release_wizard import SOURCE_IMAGE, ReleaseWizardFrame


def _info(path: str, family: str = "redos", complete: bool = True):
    return SimpleNamespace(
        path=path,
        family=family,
        release="8.0",
        architectures=("x86_64",),
        label="РЕД ОС 8",
        complete=complete,
        incomplete_reason="" if complete else "в образе нет базовой группы пакетов",
    )


class _Stub:
    """Мастер без окна: `_accept_image` трогает только эти поля."""

    def __init__(self):
        self.image_info = None
        self.image_path = ""
        self.directory = "/root/dist-прошлый"
        self.metadata = object()
        self.source_kind = ""
        self.plans = 0
        self.said = []
        self.message_bar = SimpleNamespace(
            info=lambda text: self.said.append(("info", text)),
            error=lambda text: self.said.append(("error", text)),
        )

    def _apply_step_plan(self):
        self.plans += 1


def test_a_typed_image_path_is_read_not_taken_on_trust(monkeypatch):
    """Путь, набранный руками, читается так же, как выбранный кнопкой."""
    asked = []
    monkeypatch.setattr(
        release_wizard.image_source,
        "probe",
        lambda path: asked.append(path) or _info(path),
    )
    stub = _Stub()

    assert ReleaseWizardFrame._accept_image(stub, "/root/images/redos8.iso") is True

    assert asked == ["/root/images/redos8.iso"], "образ не прочитан"
    assert stub.image_info.family == "redos"
    assert stub.source_kind == SOURCE_IMAGE
    assert stub.plans == 1, "план шагов не пересобран"
    # Ветвь дистрибутива забыта: источник теперь другой.
    assert stub.directory == ""
    assert stub.metadata is None


def test_an_image_already_read_is_not_read_twice(monkeypatch):
    """Кнопка «Выбрать образ…» читает образ сама; повторять незачем."""
    asked = []
    monkeypatch.setattr(
        release_wizard.image_source, "probe", lambda path: asked.append(path) or _info(path)
    )
    stub = _Stub()
    stub.image_info = _info("/root/images/redos8.iso")

    assert ReleaseWizardFrame._accept_image(stub, "/root/images/redos8.iso") is True
    assert asked == [], "образ прочитан второй раз"


def test_a_different_path_is_read_again(monkeypatch):
    """Прочитанное относится к прежнему пути и о новом не говорит ничего."""
    asked = []
    monkeypatch.setattr(
        release_wizard.image_source, "probe", lambda path: asked.append(path) or _info(path)
    )
    stub = _Stub()
    stub.image_info = _info("/root/images/debian.iso", family="debian")

    assert ReleaseWizardFrame._accept_image(stub, "/root/images/redos8.iso") is True
    assert asked == ["/root/images/redos8.iso"]
    assert stub.image_info.family == "redos", "остались сведения о прошлом образе"


def test_an_unreadable_image_does_not_block_the_release(monkeypatch):
    """Нечитаемый образ -- не тупик: установщик проверит его сам.

    Отказать здесь значило бы запереть выпуск там, где он возможен: образ может
    лежать на носителе, который программа не умеет читать заранее. Но сказать
    вслух надо -- иначе отбор конфигураций выключится молча, а это и был
    исходный дефект.
    """
    def _boom(path):
        raise OSError("устройство не готово")

    monkeypatch.setattr(release_wizard.image_source, "probe", _boom)
    stub = _Stub()

    assert ReleaseWizardFrame._accept_image(stub, "/root/images/redos8.iso") is True
    assert stub.image_info is None
    assert stub.plans == 1
    kinds = [kind for kind, _text in stub.said]
    assert "info" in kinds, "о невозможности прочитать образ не сказано"
    assert "без отбора" in " ".join(text for _kind, text in stub.said)


def test_an_incomplete_image_is_refused_with_the_reason(monkeypatch):
    """Неполный образ (netinst) отвергается до того, как тронут носитель."""
    monkeypatch.setattr(
        release_wizard.image_source,
        "probe",
        lambda path: _info(path, complete=False),
    )
    stub = _Stub()

    assert ReleaseWizardFrame._accept_image(stub, "/root/images/netinst.iso") is False
    assert any(kind == "error" for kind, _text in stub.said)
    assert stub.plans == 0, "план пересобран для отвергнутого образа"
