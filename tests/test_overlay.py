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


def test_both_families_get_the_same_body_with_their_own_wrapper():
    """Действие одно, обвязка разная — и разойтись они не должны.

    initramfs-tools зовёт точку монтирования будущего корня `$rootmnt`,
    dracut — `$NEWROOT`. Разница ровно в имени: две копии текста разошлись бы
    при первой же правке, и на одном из семейств носитель перестал бы
    грузиться незаметно.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    debian = get_backend("debian").overlay_boot_files(layout)
    redos = get_backend("redos").overlay_boot_files(layout)

    deb_script = debian["etc/initramfs-tools/scripts/local-bottom/poa-overlay"][0]
    red_script = redos["usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-hook.sh"][0]

    assert "$rootmnt" in deb_script and "$NEWROOT" not in deb_script
    assert "$NEWROOT" in red_script and "$rootmnt" not in red_script
    # Тело одно: сравниваем по опорным строкам, а не по всему тексту.
    for landmark in ("не подключить образ системы", "lowerdir=", "poa-overlay"):
        assert landmark in deb_script and landmark in red_script


def test_the_kernel_modules_are_put_into_the_image_explicitly():
    """Ни dracut, ни initramfs-tools не положат их сами.

    Оба кладут то, что нужно найденной конфигурации, а overlay, squashfs и
    loop в неё не входят: на момент сборки корень — обычная ext4.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    deb_hook = get_backend("debian").overlay_boot_files(layout)[
        "etc/initramfs-tools/hooks/poa-overlay"
    ][0]
    red_setup = get_backend("redos").overlay_boot_files(layout)[
        "usr/lib/dracut/modules.d/50poa-overlay/module-setup.sh"
    ][0]

    for module in ("overlay", "squashfs", "loop"):
        assert module in deb_hook, f"initramfs-tools не получит {module}"
        assert module in red_setup, f"dracut не получит {module}"

    # dracut не включает модуль просто потому, что он есть.
    conf = get_backend("redos").overlay_boot_files(layout)["etc/dracut.conf.d/50-poa-overlay.conf"][0]
    assert "add_dracutmodules" in conf


# ----------------------------------------------------------------------
# Развёртывание: система кладётся образом, а не деревом
# ----------------------------------------------------------------------
import pytest


@pytest.fixture
def session(tmp_path):
    from portable_os_admin.container import ContainerManager
    from portable_os_admin.session import AdminSession

    return AdminSession(ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123"))


@pytest.fixture
def kiosk_config():
    from portable_os_admin.config import ConfigManager, InstallConfig
    from portable_os_admin.config.schema import KioskConfig

    config = InstallConfig.create_baseline("kiosk-stick", "debian")
    config.release = "trixie"
    config.packages = ["mate-calc"]
    config.kiosk = KioskConfig(enabled=True)
    ConfigManager().validate(config)
    return config


@pytest.fixture
def big_device():
    from portable_os_admin.install import BlockDevice

    return BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer",
        removable=True, mountpoints=(None,),
    )


@pytest.fixture
def layout():
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick", "debian")
    return build_media_layout(
        plan_partitions("/dev/sdz", config.disk), mapper_name="poa-abc123", mount_root="/mnt/poa"
    )


def _deployed_commands(session, config, device, tmp_path):
    """Команды развёртывания из собранного дистрибутива, без записи на диск."""
    from portable_os_admin.install import distribution
    from portable_os_admin.install.installer import OSInstaller

    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real image")
    metadata = distribution.DistributionMetadata.for_config(
        config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    result = OSInstaller(session).deploy_distribution(
        device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
    )
    return [" ".join(r.argv) for r in result.plan.results]


def test_the_system_reaches_the_medium_as_one_file(session, kiosk_config, big_device, tmp_path):
    """Один файл вместо миллиона -- ради этого overlay и делался.

    Замер, из-за которого: тестовая флешка держит 2,9 МБ/с последовательной
    записи, а раскладка распакованным деревом шла 0,49--0,88 МБ/с. Здесь
    проверяется, что на носитель уходит именно образ, а дерево не
    раскладывается вовсе.
    """
    commands = _deployed_commands(session, kiosk_config, big_device, tmp_path)

    assert any(c.startswith("cp ") and c.endswith("system.squashfs") for c in commands), (
        "образ системы не кладётся в контейнер"
    )
    assert not any(c.startswith("cp -a") for c in commands), (
        "система всё ещё раскладывается деревом"
    )


def test_the_root_is_an_overlay_over_the_image(session, kiosk_config, big_device, tmp_path):
    """Корень собирается поверх образа, и порядок сборки -- единственно верный.

    Нижний слой подключается раньше overlay, а том смонтирован как контейнер
    ещё раньше: overlay держит нижний слой, нижний слой -- петлевое
    устройство, петля -- файл в контейнере.
    """
    commands = _deployed_commands(session, kiosk_config, big_device, tmp_path)

    container = next(i for i, c in enumerate(commands) if c.endswith("-container"))
    lower = next(i for i, c in enumerate(commands) if "-t squashfs" in c)
    root = next(i for i, c in enumerate(commands) if c.startswith("mount -t overlay"))
    assert container < lower < root, "порядок сборки корня нарушен"
    assert "lowerdir=" in commands[root] and "upperdir=" in commands[root]


def test_the_medium_learns_to_assemble_that_root_at_boot(kiosk_config, layout):
    """Носитель должен уметь собрать этот корень сам, без машины администратора.

    Хуки initramfs пишутся только при развёртывании: в эталонном корне
    дистрибутива им делать нечего -- там ещё нет ни тома, ни имени его
    отображения. Проверяется, что при развёртывании они на носитель попадают,
    а при сборке дистрибутива -- нет.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("debian")
    deployed = backend.scripted_files(kiosk_config, layout)
    built = backend.scripted_files(kiosk_config, None)

    assert any("poa-overlay" in path for path in deployed), (
        "носитель не получает хуков, собирающих корень"
    )
    assert not any("poa-overlay" in path for path in built), (
        "хуки попали в эталонный корень, где нет ни тома, ни его имени"
    )


def test_the_administrator_can_turn_the_image_off(session, kiosk_config, big_device, tmp_path):
    """Прежний способ остаётся -- на случай сравнения или отказа overlay.

    Тогда система раскладывается деревом, как раньше, и никакого overlay на
    носителе нет: иначе выключатель обещал бы больше, чем делает.
    """
    kiosk_config.disk.system_image = False
    commands = _deployed_commands(session, kiosk_config, big_device, tmp_path)

    assert any(c.startswith("cp -a") for c in commands), "система не разложена деревом"
    assert not any(c.startswith("mount -t overlay") for c in commands), (
        "overlay собирается вопреки настройке"
    )


def test_the_boot_menu_is_written_by_us_when_the_root_is_an_image(layout):
    """Генератор дистрибутива на overlay-корне не работает, и это не мелочь.

    `update-grub` (и `grub2-mkconfig` в RHEL-семействе) первым делом
    выясняет, на каком устройстве лежит корень:
    ``GRUB_DEVICE=`grub-probe --target=device /` `` -- присваивание
    безусловное, подсказать нельзя. У overlay устройства нет, и запись на
    настоящую флешку 09.09.2026 остановилась на пятнадцатом шаге из
    восемнадцати с «failed to get canonical path of 'poa-overlay'».

    Поэтому при системе из образа меню пишется своё: у носителя ровно один
    пункт, и всё, что в нём стоит, известно программе.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend

    config = InstallConfig.create_default("stick", "debian")
    layout.boot_uuid = "1111-2222"

    for family in ("debian", "redos"):
        backend = get_backend(family)
        commands = [" ".join(c) for c in backend.bootloader_commands(layout, config, overlay_root=True)]
        joined = "\n".join(commands)

        assert "update-grub" not in joined, f"{family}: зовётся генератор, который тут не работает"
        assert "grub2-mkconfig" not in joined, f"{family}: то же для RHEL-семейства"
        assert "1111-2222" in joined, f"{family}: меню не находит /boot по UUID"
        assert f"root={layout.mapper_path}" in joined, f"{family}: в меню нет корня носителя"


def test_the_distributions_own_generator_stays_for_the_plain_root(layout):
    """Без образа всё как было: конфигурацию собирает сам дистрибутив.

    Свой `grub.cfg` -- вынужденная мера для overlay, а не улучшение вообще:
    там, где штатный генератор работает, он и остаётся, потому что знает про
    своё семейство больше, чем мы.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend

    config = InstallConfig.create_default("stick", "debian")

    debian = " ".join(" ".join(c) for c in get_backend("debian").bootloader_commands(layout, config))
    redos = " ".join(" ".join(c) for c in get_backend("redos").bootloader_commands(layout, config))

    assert "update-grub" in debian
    assert "grub2-mkconfig" in redos


def test_the_menu_finds_the_kernel_by_name_on_the_medium(layout):
    """Имя ядра программа не знает и не выдумывает -- ищет на месте.

    Версия ядра приходит с образом, и захардкоженное имя разошлось бы с
    первым же обновлением дистрибутива. Скрипт берёт последнее по версии и
    отказывается, если ядра или initramfs нет, -- вместо того чтобы записать
    меню, которое ничего не загрузит.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend

    config = InstallConfig.create_default("stick", "debian")
    script = get_backend("debian").grub_config_script(layout, config, "/boot/grub/grub.cfg")

    assert "ls -1 /boot/vmlinuz-*" in script
    assert "sort -V | tail -1" in script, "берётся не последняя версия ядра"
    assert "initrd.img-" in script and "initramfs-" in script, "учтено имя только одного семейства"
    assert "exit 1" in script, "при отсутствии ядра меню всё равно пишется"
