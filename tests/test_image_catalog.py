"""Что образ рассказывает о себе (задача 3).

Разбор устроен на поддельных, но точных по форме метаданных: настоящие образы
есть только в стенде, а форма `filelists.xml.gz`, `SUPPORTED` и `base.lst`
одинакова у любого выпуска. Что чтение работает на настоящих образах,
проверено отдельно и записано в план: РЕД ОС 8 — 482 локали, 312 записей
приложений, 491 пояс, 99 раскладок; Debian 13 — 152 локали, 448 поясов, 99
раскладок.
"""
from __future__ import annotations

import gzip

import pytest

from portable_os_admin.install import image_catalog


@pytest.fixture
def redos_image(tmp_path):
    """Образ РЕД ОС: метаданные repodata с описью файлов пакетов."""
    repodata = tmp_path / "repodata"
    repodata.mkdir()
    listing = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="2">\n'
        '<package pkgid="a" name="mate-calc" arch="x86_64">\n'
        "  <file>/usr/bin/mate-calc</file>\n"
        "  <file>/usr/share/applications/mate-calc.desktop</file>\n"
        "</package>\n"
        '<package pkgid="b" name="glibc-langpack-ru" arch="x86_64">\n'
        '  <file type="dir">/usr/lib/locale/ru_RU</file>\n'
        '  <file type="dir">/usr/lib/locale/ru_RU.utf8</file>\n'
        '  <file type="dir">/usr/lib/locale/ru_RU.koi8r</file>\n'
        "  <file>/usr/lib/locale/ru_RU.utf8/LC_CTYPE</file>\n"
        "</package>\n"
        "</filelists>\n"
    )
    with gzip.open(repodata / "aaaa-filelists.xml.gz", "wt", encoding="utf-8") as handle:
        handle.write(listing)
    return str(tmp_path)


def test_application_entries_come_from_the_image(redos_image):
    """Ради этого списка всё и затевалось.

    Администратор задаёт киоску разрешённые приложения по имени записи, и до
    01.09.2026 это имя надо было помнить наизусть: калькулятор в MATE зовётся
    `mate-calc`, и узнать это из интерфейса было неоткуда.
    """
    assert image_catalog.desktop_applications(redos_image, "redos") == ["mate-calc"]


def test_locales_are_read_from_directory_entries(redos_image):
    """Локаль в RPM -- это каталог, а не файл.

    На первом прогоне 01.09.2026 нашлась ровно одна запись, `locale-archive`:
    каталоги отфильтровывались заодно с прочим шумом, а локали именно ими и
    записаны. Однобайтовые кодировки не предлагаются: ставить систему в koi8-r
    в 2026 году -- решение, которое принимают сознательно.
    """
    found = image_catalog.locales(redos_image, "redos")

    assert "ru_RU.utf8" in found
    assert "ru_RU" in found
    assert "ru_RU.koi8r" not in found


def test_debian_has_no_application_entries_and_says_so(tmp_path):
    """Пусто -- не недоделка, а свойство установочного DVD.

    На нём нет файла `Contents`, то есть узнать, какой пакет какие файлы
    принесёт, из образа нельзя вовсе.
    """
    assert image_catalog.desktop_applications(str(tmp_path), "debian") == []


def test_debian_locales_come_from_the_supported_list(tmp_path, monkeypatch):
    """`SUPPORTED` лежит внутри пакета `locales`, а тот -- в каталоге glibc.

    Промах на этом пути выглядел как «в образе нет локалей»: pool раскладывает
    пакеты по имени исходного, а не бинарного.
    """
    supported = (
        "# комментарий\n"
        "ru_RU.UTF-8 UTF-8\n"
        "ru_RU.KOI8-R KOI8-R\n"
        "en_US.UTF-8 UTF-8\n"
    )
    monkeypatch.setattr(image_catalog, "_package_file", lambda *a, **k: supported)

    found = image_catalog.locales(str(tmp_path), "debian")

    assert found == ["en_US.UTF-8", "ru_RU.UTF-8"]
    assert all(name.endswith(".UTF-8") for name in found)


def test_layouts_carry_their_names(tmp_path, monkeypatch):
    """Код без названия бесполезен: по `ru` выбрать можно, по `epo` -- нет."""
    base_lst = (
        "! model\n"
        "  pc105  Generic 105-key PC\n"
        "! layout\n"
        "  us  English (US)\n"
        "  ru  Russian\n"
        "! variant\n"
        "  dvorak  us: English (Dvorak)\n"
    )

    def fake_unpack(package, into):
        rules = f"{into}/usr/share/X11/xkb/rules"
        import os

        os.makedirs(rules, exist_ok=True)
        with open(f"{rules}/base.lst", "w", encoding="utf-8") as handle:
            handle.write(base_lst)
        return True

    monkeypatch.setattr(image_catalog, "_find_package", lambda *a, **k: "/tmp/fake.deb")
    monkeypatch.setattr(image_catalog, "_unpack", fake_unpack)

    found = image_catalog.keyboard_layouts(str(tmp_path), "debian")

    assert ("ru", "Russian") in found
    assert ("us", "English (US)") in found
    # Секция variant -- не раскладки, и попадать сюда не должна.
    assert not any(code == "dvorak" for code, _ in found)


def test_a_missing_image_is_not_an_error(tmp_path):
    """Образ может оказаться не тем: список пуст, исключения нет.

    Падение здесь означало бы, что мастер конфигурации роняется от выбора
    чужого файла.
    """
    assert image_catalog.locales(str(tmp_path / "нет"), "redos") == []
    assert image_catalog.timezones(str(tmp_path / "нет"), "debian") == []
    assert image_catalog.keyboard_layouts(str(tmp_path / "нет"), "redos") == []
