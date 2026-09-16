"""Служба выпуска: экран говорит, что нужно, а не как это делается.

Проверяется граница, а не установщик: служба обязана передать установщику
ровно то, что ей дали, ничего не подменив и не сгладив. Подменённый аргумент
здесь -- это носитель, записанный не тем, чем просили, и увидеть такое можно
только живой загрузкой.
"""
from __future__ import annotations

import pytest

from portable_os_admin.admin import media_release
from portable_os_admin.install import medium


class _FakeInstaller:
    """Запоминает, что у него попросили, и умеет прерываться."""

    created: "list[_FakeInstaller]" = []

    def __init__(self, session):
        self.session = session
        self.calls: "list[tuple[str, tuple, dict]]" = []
        self.aborted = 0
        _FakeInstaller.created.append(self)

    def run(self, *args, **kwargs):
        self.calls.append(("run", args, kwargs))
        return "установлено"

    def build_distribution(self, *args, **kwargs):
        self.calls.append(("build", args, kwargs))
        return "собрано"

    def deploy_distribution(self, *args, **kwargs):
        self.calls.append(("deploy", args, kwargs))
        return "развёрнуто"

    def abort(self):
        self.aborted += 1


@pytest.fixture
def service(monkeypatch):
    _FakeInstaller.created = []
    monkeypatch.setattr(media_release, "OSInstaller", _FakeInstaller)
    return media_release.MediaReleaseService(session="сеанс")


#: Парольная фраза байтами: кириллица в байтовом литерале запрещена языком, а
#: подменять её на строку нельзя -- установщик принимает именно байты.
PASSPHRASE = "фраза".encode("utf-8")


def test_install_passes_everything_through_untouched(service):
    result = service.install_from_image(
        "устройство", "конфигурация", PASSPHRASE,
        image_path="/root/images/redos8.iso", dry_run=False,
        confirm_device="/dev/sdb", media_name="имя", media_owner="владелец",
    )
    assert result == "установлено"
    name, args, kwargs = _FakeInstaller.created[-1].calls[0]
    assert name == "run"
    assert args == ("устройство", "конфигурация", PASSPHRASE)
    assert kwargs["image_path"] == "/root/images/redos8.iso"
    assert kwargs["dry_run"] is False
    assert kwargs["confirm_device"] == "/dev/sdb"
    assert kwargs["media_name"] == "имя"
    assert kwargs["media_owner"] == "владелец"


def test_build_and_deploy_go_to_their_own_entries(service):
    service.build_distribution("конфигурация", "/root/dist", image_path="/образ.iso")
    service.deploy_distribution("устройство", "/root/dist", PASSPHRASE, media_name="имя")

    build = _FakeInstaller.created[0].calls[0]
    deploy = _FakeInstaller.created[1].calls[0]
    assert build[0] == "build"
    assert build[1] == ("конфигурация", "/root/dist")
    assert deploy[0] == "deploy"
    assert deploy[1] == ("устройство", "/root/dist", PASSPHRASE)
    assert deploy[2]["media_name"] == "имя"


def test_every_operation_gets_its_own_installer(service):
    """Установщик держит состояние уборки -- что смонтировано и какие тома
    открыты. Переиспользованный между записями, он убирал бы за прошлой."""
    service.build_distribution("конфигурация", "/root/dist")
    service.build_distribution("конфигурация", "/root/dist2")
    assert len(_FakeInstaller.created) == 2
    assert _FakeInstaller.created[0] is not _FakeInstaller.created[1]


def test_abort_reaches_the_running_installer(service):
    service.build_distribution("конфигурация", "/root/dist")
    service.abort()
    assert _FakeInstaller.created[-1].aborted == 1


def test_an_abort_asked_for_before_the_start_is_not_lost(service):
    """Между нажатием «Начать» и созданием установщика в фоновом потоке есть
    мгновение. «Прервать», нажатое именно в него, раньше пропадало совсем:
    останавливать было ещё нечего, а второй раз человек не нажмёт -- кнопка
    выглядит сработавшей. Для записи носителя это мгновение стоит флешки."""
    service.abort()
    service.install_from_image("устройство", "конфигурация", PASSPHRASE)
    assert _FakeInstaller.created[-1].aborted == 1


def test_abort_without_work_does_not_raise(service):
    """Кнопка «Прервать» не должна падать оттого, что нажата на полсекунды
    позже, чем работа кончилась."""
    service.abort()
    assert service.running is False


def test_the_service_knows_which_medium_it_releases(service):
    """Вид носителя выбирается здесь -- в том месте, которое знает, что именно
    выпускается. Сегодня вид один, и это флешка."""
    assert service.medium is medium.FLASH
    assert service.medium.service_key_path
