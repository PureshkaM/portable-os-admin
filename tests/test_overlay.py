"""Система на носителе как сжатый образ плюс слой изменений.

Свойство, которое эти проверки защищают, — не «файлы на месте», а «носитель
загрузится и его базу нельзя изменить незаметно». Ошибка здесь стоит
незагружающегося носителя, а это тот отказ, который нельзя починить на месте.
"""
from __future__ import annotations

from portable_os_admin.install import overlay


def test_the_image_is_copied_as_one_file():
    """Вся выгода в том, что запись последовательная.

    Прежняя раскладка писала 4,32 ГБ миллионом мелких файлов и шла втрое
    медленнее потолка носителя. Здесь один файл на 1,56 ГиБ.
    """
    argv = overlay.copy_image_command("/dist/rootfs.squashfs", "/mnt/container")

    assert argv[0] == "cp"
    assert argv[-1] == f"/mnt/container/{overlay.SYSTEM_IMAGE}"
    assert "/dist/rootfs.squashfs" in argv


def test_overlay_needs_its_working_directory_prepared():
    """workdir обязан лежать на той же ФС, что и upperdir, и быть пустым.

    Иначе монтирование откажет, и сообщение об этом не назовёт причину.
    """
    commands = [" ".join(argv) for argv in overlay.prepare_commands("/mnt/container")]
    image, upper, work = overlay.container_paths("/mnt/container")

    assert any(upper in c for c in commands)
    assert any(work in c for c in commands)
    # Оба — внутри контейнера, то есть на одной файловой системе.
    assert upper.startswith("/mnt/container/") and work.startswith("/mnt/container/")
    assert image == "/mnt/container/" + overlay.SYSTEM_IMAGE


def test_the_lower_layer_is_mounted_read_only():
    """База неизменяема — на этом держится весь смысл схемы."""
    argv = overlay.lower_mount_command("/mnt/container")

    assert "squashfs" in argv
    options = argv[argv.index("-o") + 1]
    assert "ro" in options.split(",")


def test_the_working_overlay_is_assembled_from_all_three_layers():
    argv = overlay.mount_command("/mnt/container", "/mnt/root")
    options = argv[argv.index("-o") + 1]

    assert "lowerdir=" in options and "upperdir=" in options and "workdir=" in options
    assert argv[-1] == "/mnt/root"
    assert "overlay" in argv


def test_the_boot_script_refuses_instead_of_booting_half_a_system():
    """Отказ должен быть громким.

    Если overlay не собрался, оставить контейнер корнем нельзя: система из
    одного файла system.squashfs не загрузится, а понять это по симптомам
    почти невозможно.
    """
    script = overlay.boot_script("poa-abc123")

    assert "exit 1" in script
    for step in ("не смонтировать контейнер", "не подключить образ системы", "не собрать overlay"):
        assert step in script, f"нет внятного отказа: {step}"
    # Проверяется, что том вообще открыт, до всего остального.
    assert script.index("/dev/mapper/poa-abc123") < script.index("mount -t squashfs")


def test_the_boot_script_keeps_the_container_reachable():
    """На контейнере лежат и образ, и слой изменений.

    Размонтировать его нельзя, пока система работает, поэтому он переносится
    внутрь собранного корня.
    """
    script = overlay.boot_script("poa-abc123")

    assert f"mount --move \"$container\" \"$rootmnt{overlay.CONTAINER_MOUNT}\"" in script


def test_the_root_is_not_described_in_fstab():
    """Корень собран до того, как fstab прочитан.

    Запись о нём заставила бы systemd перемонтировать то, чего в его
    понимании не существует.
    """
    assert overlay.fstab_rows() == []
