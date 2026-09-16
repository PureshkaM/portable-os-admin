"""Граница «носитель»: место, которое токен займёт, когда появится.

Проверяется не поведение установщика, а честность описания: программа должна
называть свойства нынешнего носителя такими, какие они есть, иначе место под
второй вид окажется занято выдумкой.
"""
from __future__ import annotations

from portable_os_admin.install import medium
from portable_os_admin.install.backends.constants import SERVICE_KEY_PATH


def test_the_flash_profile_says_the_key_can_leave_with_a_copy():
    """У флешки ключ тома выводится из парольной фразы -- значит снятая копия
    образа открывается там же, где известна фраза. Это названная в README
    граница модели угроз, и она должна быть названа и в коде."""
    assert medium.FLASH.key_leaves_the_medium is True


def test_the_flash_profile_does_not_claim_to_prevent_boot_tampering():
    """Опись обнаруживает подмену загрузчика после факта: initramfs успевает
    получить фразу. Обещать запрет там, где есть только обнаружение, -- это
    ровно тот вид лжи, ради которого модуль и написан."""
    assert medium.FLASH.boot_tampering_is_prevented is False
    assert medium.FLASH.boot_protection is medium.BootProtection.SIGNED_MANIFEST


def test_the_flash_profile_knows_where_its_service_key_lies():
    """Файл ключа служебных томов -- свойство носителя, а не установщика: у
    токена его не будет вовсе."""
    assert medium.FLASH.service_key_path == SERVICE_KEY_PATH


def test_an_unknown_profile_falls_back_instead_of_raising():
    """Конфигурация, записанная будущей версией с токеном, не должна ронять
    сегодняшнюю программу: она соберёт флешку."""
    assert medium.profile("токен-которого-нет") is medium.DEFAULT
    assert medium.profile("") is medium.DEFAULT
    assert medium.profile("flash") is medium.FLASH


def test_only_the_verified_medium_is_declared():
    """Токен не объявлен, пока не проверен ни одной строчкой живого прогона.

    Заранее описанный вид носителя выглядел бы как поддержка, которой нет, и
    первый же администратор выбрал бы его.
    """
    assert set(medium.PROFILES) == {"flash"}


def test_description_reads_as_a_sentence_about_the_real_thing():
    line = medium.FLASH.describe()
    assert "парольной фразы" in line
    assert "не запрещается" in line
