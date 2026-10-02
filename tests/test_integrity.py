"""Целостность загрузчика и initramfs (задача 13).

Проверяется то, ради чего механизм существует: подмена файла, исполняемого
до расшифровки корня, должна быть замечена. Всё остальное здесь -- свойства,
без которых эта проверка ничего не стоит: подпись описи, вложенные точки
монтирования и честное поведение на носителе, записанном раньше.
"""
from __future__ import annotations

import pytest

from portable_os_admin.install import integrity


@pytest.fixture
def medium(tmp_path):
    """Незашифрованная часть носителя: ESP смонтирован внутрь /boot."""
    boot = tmp_path / "boot"
    esp = boot / "efi"
    (esp / "EFI" / "BOOT").mkdir(parents=True)
    (esp / "EFI" / "BOOT" / "BOOTX64.EFI").write_bytes(b"shim")
    (esp / "EFI" / "BOOT" / "grubx64.efi").write_bytes(b"grub")
    (boot / "vmlinuz-6.12").write_bytes(b"kernel")
    (boot / "initramfs-6.12.img").write_bytes(b"initramfs" * 1000)
    return {"boot": str(boot), "esp": str(esp)}


def test_a_replaced_initramfs_is_noticed(medium):
    """То, ради чего всё это написано.

    Подменённый initramfs спрашивает пароль ровно так же, как настоящий, и
    запоминает его. До 01.09.2026 такая подмена не обнаруживалась ничем.
    """
    before = integrity.collect(medium)

    initramfs = f"{medium['boot']}/initramfs-6.12.img"
    with open(initramfs, "wb") as handle:
        handle.write(b"initramfs" * 999 + b"stealer!!")

    difference = integrity.compare(before, integrity.collect(medium))

    assert not integrity.is_intact(difference)
    assert difference["changed"] == ["boot:initramfs-6.12.img"]
    assert "изменено файлов: 1" in integrity.describe(difference)


def test_an_untouched_medium_is_reported_as_such(medium):
    """Ложная тревога здесь дороже, чем кажется: администратор перестанет
    верить проверке раньше, чем она пригодится."""
    before = integrity.collect(medium)
    difference = integrity.compare(before, integrity.collect(medium))

    assert integrity.is_intact(difference)
    assert "не изменялись" in integrity.describe(difference)


def test_a_file_brought_from_outside_is_noticed(medium):
    """Лишний файл на ESP -- это принесённое со стороны, а не «мелочь»."""
    before = integrity.collect(medium)
    with open(f"{medium['esp']}/EFI/BOOT/evil.efi", "wb") as handle:
        handle.write(b"payload")

    difference = integrity.compare(before, integrity.collect(medium))
    assert difference["added"] == ["esp:EFI/BOOT/evil.efi"]


def test_the_esp_is_not_counted_twice(medium):
    """ESP монтируется внутрь /boot, и опись не должна их складывать.

    Иначе одна правка ESP выглядела бы двумя изменениями -- как
    `esp:EFI/...` и как `boot:efi/EFI/...`, -- и число расхождений перестало
    бы значить что-либо.
    """
    manifest = integrity.collect(medium)

    esp_entries = [path for path in manifest if path.startswith("esp:")]
    doubled = [path for path in manifest if path.startswith("boot:efi/")]

    assert esp_entries, "ESP не попал в опись вовсе"
    assert not doubled, "файлы ESP посчитаны и как часть /boot"


def test_the_manifest_cannot_be_forged_without_the_container(medium):
    """Подпись выводится из секрета администратора, а он на носитель не едет.

    Без этого опись можно было бы переписать вместе с загрузчиком: тот, кто
    поменял initramfs, поменял бы и список сумм.
    """
    manifest = integrity.collect(medium)
    secret = b"admin-secret-32-bytes-long------"
    signature = integrity.sign(manifest, secret)

    assert integrity.verify(manifest, signature, secret)
    assert not integrity.verify(manifest, signature, b"someone-elses-secret-32-bytes--")

    tampered = dict(manifest)
    tampered["boot:initramfs-6.12.img"] = "0" * 64
    assert not integrity.verify(tampered, signature, secret)


def test_the_signature_does_not_depend_on_dictionary_order(medium):
    """Подписывается каноническая форма, а не то, как словарь лёг в память."""
    manifest = integrity.collect(medium)
    reversed_order = dict(reversed(list(manifest.items())))
    secret = b"admin-secret-32-bytes-long------"

    assert integrity.sign(manifest, secret) == integrity.sign(reversed_order, secret)


def test_a_missing_area_is_not_an_error(tmp_path):
    """Каталога может не быть -- например, в сухом прогоне.

    Падать здесь нельзя: опись снимается в конце записи, и исключение на
    последнем шаге испортило бы носитель, который уже готов.
    """
    assert integrity.collect({"boot": str(tmp_path / "нет"), "esp": ""}) == {}


def test_slot_files_and_the_slot_choice_are_not_in_the_manifest(tmp_path):
    """Э4/Э9: обновление законно меняет каталоги слотов и переменные GRUB --
    в опись они не входят (слот сверяется с подписанным выпуском, Э5)."""
    from portable_os_admin.install import integrity

    boot, esp = tmp_path / "boot", tmp_path / "boot" / "efi"
    for path in (boot / "slot-a" / "vmlinuz", boot / "slot-b" / "initrd.img", boot / "poa-medium.img",
                 boot / "grub" / "grub.cfg", esp / "EFI" / "poa" / "grubenv", esp / "EFI" / "BOOT" / "BOOTX64.EFI"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    manifest = integrity.collect({"boot": str(boot), "esp": str(esp)})
    assert set(manifest) == {"boot:poa-medium.img", "boot:grub/grub.cfg", "esp:EFI/BOOT/BOOTX64.EFI"}
