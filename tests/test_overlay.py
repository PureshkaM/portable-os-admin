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
    # Верхний слой получает метку мандатного контроля сразу при создании.
    # Корень собранного overlay берёт инод отсюда, и без метки он выходит
    # `unlabeled_t`: прогон 12.09.2026 показал носитель, доходящий до
    # graphical.target, где ни одна служба не запускается.
    assert any("setfattr" in c and upper in c for c in commands), (
        "верхний слой остаётся без метки SELinux"
    )
    assert image == "/mnt/container/" + overlay.SYSTEM_IMAGE


def test_labels_are_written_through_the_upper_layer_not_the_overlay():
    """По overlay метки не ложатся, и молча.

    `setfiles`, запущенный по собранному корню, пропускает такую файловую
    систему и выходит с нулём. Прогон 12.09.2026: ни fstab, ни crypttab, ни
    настройка автовхода SDDM метки не получили, носитель встал на экране входа,
    и в журнале установки не было ни строчки. Поэтому метится слой изменений
    напрямую -- он на обычном ext4, -- а `-r` отбрасывает префикс при сверке с
    политикой.
    """
    from portable_os_admin.install.backends import get_backend

    argv = get_backend("redos").restore_labels_command(
        "/mnt/root", ("/etc",), upper="/mnt/poa-upper"
    )
    joined = " ".join(argv)
    assert "-r /mnt/poa-upper" in joined, "метки снова ставятся сквозь overlay"
    assert "/mnt/poa-upper/etc" in joined


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
    # Нижний слой -- тоже, и это стоило целого прогона 11.09.2026. Носитель
    # доходил до graphical.target, после чего каждая служба падала с кодом 127,
    # включая journald: overlay держится на смонтированном squashfs, а тот
    # оставался в initramfs. Записи в верхний слой шли, уже открытые файлы
    # работали, а любой новый exec из /usr получал "файла нет".
    assert f"mount --move \"$lower\" \"$rootmnt{overlay.LOWER_MOUNT}\"" in script, (
        "образ системы не переносится в новый корень"
    )
    # Сперва нижний слой, потом контейнер: оба лежат в /run/poa, и перенос
    # родителя раньше ребёнка оставил бы ребёнка за бортом.
    assert script.index('"$lower"') < script.index(f'"$rootmnt{overlay.CONTAINER_MOUNT}"')


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
    # overlay_root=True -- это и есть «корень собран из образа». Носитель с
    # обычным корнем таких хуков не получает: см. соседний тест.
    deployed = backend.scripted_files(kiosk_config, layout, overlay_root=True)
    built = backend.scripted_files(kiosk_config, None, overlay_root=True)

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
        assert backend.overlay_root_arguments(layout) in joined, (
            f"{family}: в меню нет описания корня"
        )


def test_the_root_is_named_the_way_the_family_understands_it(layout):
    """Одно и то же устройство описано по-разному, и разница не косметическая.

    У initramfs-tools корень монтирует сам образ, а наш скрипт подменяет его
    после -- поэтому `root=` там обычное устройство. У dracut корня в
    командной строке нет вовсе: любое значение `root=` монтирует systemd
    внутри initramfs, ещё до всякого нашего хука. Три загрузки РЕД ОС
    10.09.2026 перебрали все варианты -- устройство, `poa:<устройство>`,
    `poa`, -- и каждый кончался аварийной оболочкой со своим сообщением.
    """
    from portable_os_admin.install.backends import get_backend

    debian = get_backend("debian").overlay_root_arguments(layout)
    assert debian == f"root={layout.mapper_path}"

    redos = get_backend("redos").overlay_root_arguments(layout)
    assert redos == "rd.poa.overlay=1", "признак корня из образа назван иначе"
    assert "root=" not in redos, "в командной строке dracut не должно быть root="


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


def test_what_the_image_keeps_on_the_service_volumes_is_carried_onto_them(
    session, kiosk_config, big_device, tmp_path
):
    """Пустой раздел не должен накрывать то, что лежит в образе.

    Дважды пройденные грабли, теперь с третьей стороны. При раскладке деревом
    их обходили порядком: /boot и том журналов подключали до копирования, и
    файлы ложились уже на них. При системе из образа порядок обратный по
    необходимости -- корня до сборки overlay не существует, -- и обойти
    нечем: ядро с initramfs лежат внутри образа, а раздел /boot приходит
    пустым и прячет их.

    Запись на настоящую флешку 09.09.2026 остановилась именно на этом:
    генератор меню не нашёл в /boot ни одного ядра, потому что видел пустой
    раздел. То же самое ждало /var/log -- каталоги auditd и прочих служб
    остались бы в образе, а том журналов пришёл бы пустым.
    """
    commands = _deployed_commands(session, kiosk_config, big_device, tmp_path)

    mounted_boot = next(i for i, c in enumerate(commands) if c.rstrip().endswith("/boot"))
    copied = [i for i, c in enumerate(commands) if "cp -a" in c and "/run/poa/lower" in c]
    assert copied, "содержимое образа не переносится на служебные разделы"
    assert min(copied) > mounted_boot, "перенос идёт до подключения разделов"

    joined = "\n".join(commands[i] for i in copied)
    assert "/boot/." in joined, "ядро остаётся спрятанным под пустым /boot"
    assert "/var/log/." in joined, "каталоги служб остаются в образе"

    # И всё это -- до сборки initramfs и меню, которым эти файлы нужны.
    built = next(i for i, c in enumerate(commands) if "initramfs" in c or "dracut" in c)
    assert max(copied) < built, "образ переносится после того, как его содержимое уже понадобилось"


def test_the_encrypted_root_is_named_in_the_initramfs_explicitly(layout):
    """Образ должен знать про шифрование, даже когда корень -- overlay.

    cryptsetup-initramfs обычно обходится без подсказки: он смотрит, какое
    устройство держит корень, находит его в crypttab и включает описание в
    образ. У носителя с системой из образа корень -- overlay, устройства у
    него нет, и хук не включает ничего.

    Загрузка с настоящей флешки 10.09.2026 показала это в чистом виде:
    носитель дошёл до аварийной оболочки initramfs, не спросив пароля, а
    `/conf/conf.d/cryptroot` в образе отсутствовал. Опция `initramfs` в
    crypttab -- то самое явное указание, и она описана в crypttab(5).
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend

    config = InstallConfig.create_default("stick", "debian")
    layout.luks_uuid = "aaaa-bbbb"

    crypttab = get_backend("debian")._crypttab(layout, config)
    root_line = next(line for line in crypttab.splitlines() if layout.mapper_name in line)

    assert "initramfs" in root_line, "образ не узнает, что корень зашифрован"
    assert "luks" in root_line and "discard" in root_line, "прежние опции корня потеряны"


def test_the_medium_gets_the_scripts_that_could_not_be_in_the_image(
    session, kiosk_config, big_device, tmp_path
):
    """Хуки, собирающие корень, доезжают до носителя.

    В образ их не кладут: там ещё нет ни тома, ни имени его отображения. А
    развёртывание писало только `config_files` -- и хуки не попадали никуда.

    Загрузка с флешки 10.09.2026 показала это в чистом виде: пароль принят,
    том открыт, `/dev/mapper/poa-...` на месте -- и аварийная оболочка, потому
    что собрать из образа корень оказалось некому. В `/scripts/local-bottom`
    лежали только штатные скрипты дистрибутива.
    """
    from portable_os_admin.install.installer import OSInstaller

    directory = tmp_path / "dist"
    directory.mkdir()
    from portable_os_admin.install import distribution

    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real image")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    result = OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
    )
    written = [" ".join(r.argv) for r in result.plan.results]

    assert any("local-bottom" in c for c in written), (
        "хук, собирающий корень из образа, не доезжает до носителя"
    )
    # А то, что и так лежит в образе, второй раз не пишется.
    assert not any("poa-kiosk-apply" in c for c in written), (
        "образ переписывается поверх самого себя"
    )


def test_the_boot_script_speaks_busybox():
    """В initramfs команды выполняет BusyBox, а он длинных опций не знает.

    Загрузка с флешки 10.09.2026: `mount --move` молча не срабатывал -- вывод
    уходил в /dev/null, -- и следом попытка смонтировать том второй раз
    падала с «Device or resource busy», потому что он уже был корнем. Носитель
    оставался в аварийной оболочке при открытом томе и доехавших хуках.

    Тот же класс ошибки, что уже ловили в этом проекте: команда, написанная
    для полноценной системы и выполненная в initramfs.
    """
    script = overlay.boot_script("poa-abc123")

    assert "mount -o move" in script, "перенос точки монтирования не по-BusyBox'овски"
    move_at = script.index("mount -o move")
    long_at = script.index("mount --move")
    assert move_at < long_at, "короткая форма должна идти первой, длинная -- запасной"


def test_the_container_is_remounted_writable_before_the_overlay():
    """Слой изменений лежит на контейнере, а корень ядро монтирует только на чтение.

    `ro` в командной строке -- обычное дело: systemd перемонтирует корень сам
    после проверки. Но overlay собирается раньше, и upperdir на томе только
    для чтения не даёт собрать ничего.
    """
    script = overlay.boot_script("poa-abc123")

    assert "remount,rw" in script, "контейнер остаётся только для чтения"
    assert script.index("remount,rw") < script.index("lowerdir="), (
        "перемонтирование идёт после сборки overlay -- то есть поздно"
    )


def test_dracut_is_told_that_our_root_is_legal(layout):
    """Модуль объявляет корень `poa:` своим — иначе dracut его не примет.

    Без такого объявления dracut отвечает, что не знает, как обращаться с
    заданным корнем, и уходит в аварийную оболочку ещё до монтирования.
    Заодно объявляется ожидание устройства: зашифрованный том появляется
    позже, а без ожидания очередь initramfs закончится раньше него.
    """
    from portable_os_admin.install.backends import get_backend

    files = get_backend("redos").overlay_boot_files(layout)
    parse = files["usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-parse.sh"][0]

    assert "rootok=1" in parse, "корень не объявлен законным"
    assert "wait_for_dev" in parse, "устройство корня не ожидается"
    assert "rd.poa.overlay" in parse, "разбирается не наш признак"
    assert "/etc/cmdline.d" in parse, "корень не объявлен там, где его увидит dracut"
    assert layout.mapper_name in parse, "ожидается не тот том"


def test_the_dracut_hook_builds_the_root_instead_of_replacing_it(layout):
    """Хук монтирует контейнер сам и встаёт на место монтирования корня.

    Прежняя версия ждала уже смонтированный корень и подменяла его в
    `pre-pivot`. На dracut до `pre-pivot` дело не доходило: корень
    размонтировал и забраковал штатный модуль `rootfs-block` (загрузка
    РЕД ОС + KDE 10.09.2026, «Can't mount root filesystem»).
    """
    from portable_os_admin.install.backends import get_backend

    files = get_backend("redos").overlay_boot_files(layout)
    setup = files["usr/lib/dracut/modules.d/50poa-overlay/module-setup.sh"][0]
    hook = files["usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-hook.sh"][0]

    assert "inst_hook mount 90" in setup, "хук не встаёт на монтирование корня"
    assert "pre-pivot" not in setup, "остался хук, до которого загрузка не доходит"
    assert f"mount /dev/mapper/{layout.mapper_name}" in hook, "контейнер не монтируется"
    assert 'mount -o move "$NEWROOT"' not in hook, "хук всё ещё переносит чужое монтирование"


def test_the_dracut_hook_does_not_leave_strict_mode_behind(layout):
    """`set -u` в хуке dracut остаётся включённым и после него.

    Хуки исполняются через `.`, то есть в оболочке самого dracut. Строгий
    режим пережил бы наш скрипт и уронил бы чужой код на первой необъявленной
    переменной — отказ выглядел бы как поломка dracut, без всякой связи с
    нами.
    """
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install import overlay

    hook = get_backend("redos").overlay_boot_files(layout)[
        "usr/lib/dracut/modules.d/50poa-overlay/poa-overlay-hook.sh"
    ][0]

    assert "set -u" not in hook, "строгий режим утечёт в оболочку dracut"
    assert "return 0" in hook, "чужой корень не пропускается без выхода из dracut"
    assert "rd.poa.overlay" in hook, "хук не проверяет, что корень собирает он"
    # У initramfs-tools скрипт запускается сам по себе, и там строгий режим уместен.
    assert "set -u" in overlay.boot_script("poa-x")


def test_the_overlay_scripts_stay_off_a_plain_root():
    """Скрипты overlay -- только на носитель, чей корень собран из образа.

    Разойдись это решение с тем, что делает установщик, и носитель с обычным
    корнем получит скрипт, который при загрузке унесёт корень из `$rootmnt`
    ради образа, которого нет. Ровно так Debian + MATE 13.09.2026 переставал
    грузиться после регистрации машины.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )
    backend = get_backend("debian")
    path = "etc/initramfs-tools/scripts/local-bottom/poa-overlay"

    assert path not in backend.scripted_files(config, layout)
    assert path in backend.scripted_files(config, layout, overlay_root=True)


def test_the_script_looks_for_the_image_before_it_moves_the_root():
    """Порядок двух строк -- разница между «носитель просто загрузился» и
    «носитель не грузится и не объясняет почему»."""
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )
    script = get_backend("debian").overlay_boot_files(layout)[
        "etc/initramfs-tools/scripts/local-bottom/poa-overlay"
    ][0]

    assert script.index('[ -f "$rootmnt/') < script.index('mount -o move "$rootmnt"')
