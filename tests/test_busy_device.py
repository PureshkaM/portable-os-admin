"""Носитель, который держит кто-то ещё, писать нельзя.

Стоило это испорченного носителя и часа работы: 04.09.2026 виртуальная машина
стенда осталась запущенной и держала файл образа открытым на запись,
установка отработала все восемнадцать шагов, отчиталась «Носитель готов» -- а
внутри системного тома не оказалось файловой системы вовсе. Проверка
«смонтирован ли носитель» такое не ловит: смонтировано ничего не было.
"""
from __future__ import annotations

import os

import pytest

from portable_os_admin.errors import InstallError
from portable_os_admin.install.devices import BlockDevice, busy_holders
from portable_os_admin.install.installer import OSInstaller


def test_holders_of_a_device_nobody_touches_are_empty(tmp_path):
    """Обычный случай: носитель свободен, и функция это говорит прямо."""
    assert busy_holders(str(tmp_path / "нет-такого-устройства")) == []


def test_a_process_holding_the_image_file_is_found(tmp_path):
    """Файл-образ, открытый другим процессом, -- тот самый случай.

    Проверяется на самом деле, а не на подставном /proc: открываем файл в
    этом же процессе и ищем себя.
    """
    image = tmp_path / "medium.img"
    image.write_bytes(b"0" * 1024)

    with open(image, "r+b"):
        holders = busy_holders(str(image))

    assert any(str(os.getpid()) in holder for holder in holders), (
        f"процесс, держащий образ, не найден: {holders}"
    )


def test_a_closed_file_is_not_reported_as_busy(tmp_path):
    image = tmp_path / "medium.img"
    image.write_bytes(b"0" * 1024)
    handle = open(image, "r+b")
    handle.close()

    assert busy_holders(str(image)) == []


def test_the_installer_refuses_to_write_onto_a_busy_medium(monkeypatch):
    """Отказ, а не запись в четыре руки.

    Сообщение обязано называть держателя и говорить, что делать: администратор
    видит его в середине работы, и «устройство занято» без имени -- это
    полчаса поисков.
    """
    installer = OSInstaller.__new__(OSInstaller)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=16 * 1024**3, model="Kingston",
        removable=True, mountpoints=(), partitions=(),
    )

    monkeypatch.setattr(
        "portable_os_admin.install.installer.busy_holders",
        lambda _path: ["процесс 4242 (qemu-system-x86_64)"],
    )

    with pytest.raises(InstallError) as failure:
        installer._refuse_if_busy(device, dry_run=False)

    message = str(failure.value)
    assert "/dev/sdb" in message
    assert "qemu-system-x86_64" in message, "не сказано, кто держит носитель"
    assert "Обслуживание" in message, "не сказано, где освободить"


def test_a_dry_run_checks_nothing(monkeypatch):
    """Пробный прогон ничего не пишет, и запрещать ему нечего."""
    installer = OSInstaller.__new__(OSInstaller)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=1, model="", removable=True,
        mountpoints=(), partitions=(),
    )
    monkeypatch.setattr(
        "portable_os_admin.install.installer.busy_holders",
        lambda _path: ["процесс 1 (кто-то)"],
    )

    installer._refuse_if_busy(device, dry_run=True)  # не должно бросить


def test_the_check_runs_before_anything_is_erased():
    """Проверка стоит до первой разрушающей команды, а не после неё.

    Иначе она сообщала бы о занятости носителя, который уже стёрт.
    """
    import inspect

    source = inspect.getsource(OSInstaller._prepare_medium)
    assert source.index("_refuse_if_busy") < source.index("self.medium_modified"), (
        "проверка занятости должна стоять до первой разрушающей команды"
    )
