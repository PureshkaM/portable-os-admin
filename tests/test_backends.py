from portable_os_admin.config import InstallConfig
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.backends.astra import AstraInstallBackend
from portable_os_admin.install.backends.debian import DebianInstallBackend
from portable_os_admin.install.backends.redos import RedOSInstallBackend


def test_get_backend_returns_expected_types():
    assert isinstance(get_backend("debian"), DebianInstallBackend)
    assert isinstance(get_backend("redos"), RedOSInstallBackend)
    assert isinstance(get_backend("astra"), AstraInstallBackend)


def _assert_debootstrap(argv, config):
    # debootstrap is wrapped in eatmydata when the host provides it, so this
    # must not assume it sits at argv[0] -- otherwise the suite would pass or
    # fail depending on which packages the test machine happens to have.
    assert argv[0] in ("debootstrap", "eatmydata")
    assert "debootstrap" in argv
    assert config.release in argv
    assert "/mnt/poa" in argv
    # the base system comes from the local image, never from a network mirror
    assert "file:///mnt/image" in argv


def test_debian_bootstrap_command():
    config = InstallConfig.create_default("stick1", "debian")
    backend = DebianInstallBackend()
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release), config)


def test_redos_bootstrap_command():
    config = InstallConfig.create_default("stick1", "redos")
    backend = RedOSInstallBackend()
    argv = backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
    assert argv[0] == "dnf"
    assert "--installroot" in argv
    assert "/mnt/poa" in argv


def test_chroot_setup_commands_include_account_sudo_and_system_tasks():
    config = InstallConfig.create_default("stick1", "debian")
    config.account.username = "ivanov"
    config.account.sudo = True
    config.system_tasks = ["echo hi > /etc/motd"]

    backend = DebianInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("useradd" in c and "ivanov" in c for c in commands)
    assert any("usermod" in c and "sudo" in c for c in commands)
    assert any("sh" in c and "-c" in c and "echo hi > /etc/motd" in c for c in commands)
    # the OS login password is always locked -- LUKS passphrase is the front door
    assert any("passwd" in c and "-l" in c for c in commands)


def test_redos_uses_wheel_group_and_sshd_service():
    config = InstallConfig.create_default("stick1", "redos")
    config.account.sudo = True
    config.remote_access.enabled = True
    config.remote_access.method = "openssh"

    backend = RedOSInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("wheel" in c for c in commands)
    assert any("sshd" in c for c in commands)


def test_astra_bootstrap_command_reuses_debootstrap():
    config = InstallConfig.create_default("stick1", "astra")
    backend = AstraInstallBackend()
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release), config)


def test_astra_uses_astra_admin_group_not_sudo():
    config = InstallConfig.create_default("stick1", "astra")
    config.account.sudo = True

    backend = AstraInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("astra-admin" in c for c in commands)
    assert not any(c[-1] == "sudo" and "usermod" in c for c in commands)


def test_debootstrap_skips_signature_check_for_a_local_image():
    """Installation images carry dists/<suite>/Release but no Release.gpg and
    no InRelease -- measured on debian-13.6.0-amd64-DVD-1.iso. Without
    --no-check-gpg debootstrap aborts with "Failed getting release signature
    file" before unpacking anything, so installing from a local image (the
    only supported source) fails every time."""
    config = InstallConfig.create_default("stick1", "debian")
    argv = DebianInstallBackend().bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
    assert "--no-check-gpg" in argv
    # the flag must precede the positional arguments, or debootstrap reads it
    # as the suite name
    assert argv.index("--no-check-gpg") < argv.index(config.release)


def test_debootstrap_keeps_signature_check_for_a_network_mirror():
    """A network mirror does publish signatures, so there the check stays on."""
    config = InstallConfig.create_default("stick1", "debian")
    argv = DebianInstallBackend().bootstrap_command(
        "/mnt/poa", config, "https://deb.debian.org/debian", config.release
    )
    assert "--no-check-gpg" not in argv


def test_astra_inherits_the_local_image_signature_behaviour():
    config = InstallConfig.create_default("stick1", "astra")
    argv = AstraInstallBackend().bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
    assert "--no-check-gpg" in argv


def test_redos_leaves_the_distributions_own_repository_files_alone():
    """`@core` installs redos-release, which writes correct repository files
    and ships the signing key. Verified by unpacking that package off a RED OS
    8 image: RedOS-Base.repo and RedOS-Updates.repo, gpgcheck=1 and
    gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT. Writing our own on top
    would replace something correct with something guessed."""
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = ""
    assert RedOSInstallBackend().sources_list(config, "8.0") is None


def test_redos_writes_a_mirror_next_to_them_not_over_them():
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = "https://repo.example.org/redos"
    backend = RedOSInstallBackend()

    text = backend.sources_list(config, "8.0")
    assert "repo.example.org" in text
    # a file of our own, so the distribution's stay untouched
    assert backend.sources_path == "etc/yum.repos.d/poa-mirror.repo"
    assert "RedOS-Base" not in backend.sources_path


def test_signing_settings_come_from_the_image_and_default_to_honest_silence():
    """With no image to read, the file says packages are not verified rather
    than pointing at a key path that may not exist -- a wrong gpgkey makes
    every dnf command fail with a confusing error."""
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = "https://repo.example.org/redos"
    text = RedOSInstallBackend().sources_list(config, "8.0", image_mountpoint="")
    assert "gpgcheck=0" in text
    assert "gpgkey=" not in text


def test_each_family_writes_its_sources_where_that_family_keeps_them():
    assert DebianInstallBackend().sources_path == "etc/apt/sources.list"
    assert AstraInstallBackend().sources_path == "etc/apt/sources.list"
    assert RedOSInstallBackend().sources_path.startswith("etc/yum.repos.d/")


def test_redos_package_installs_use_the_image_not_the_host_repositories():
    """dnf runs outside the chroot with --installroot, so it does not inherit
    the target's /etc/yum.repos.d. Without being told, it reaches for the
    administrator machine's own repositories -- which answer nothing on an
    isolated network and are not the chosen image in any case. The bootstrap
    command always said this; the later package installs did not."""
    backend = RedOSInstallBackend()
    backend.bind_install_source("file:///mnt/poa-image-x", "8.0")

    argv = backend.package_install_command("/mnt/poa", ["kernel"])
    assert "--repofrompath=poa-image,file:///mnt/poa-image-x" in argv
    # `--repo` names the only repository to use, which is what restricts the
    # transaction to the image. `--disablerepo=*` used to be here as well and
    # had to go: dnf refuses the two together ("argument --repo/--repoid: not
    # allowed with argument --disablerepo"), which is what the first real
    # RED OS run died on.
    assert "--repo=poa-image" in argv
    assert not any(a.startswith("--disablerepo") for a in argv)
    # an image carries no signature; the trust anchor is the file itself
    assert "--nogpgcheck" in argv
    assert "--releasever" in argv


def test_redos_package_installs_stay_plain_when_no_source_is_bound():
    """Nothing bound means nothing to say -- the installed system's own
    configuration is then the right answer, not an invented one."""
    argv = RedOSInstallBackend().package_install_command("/mnt/poa", ["kernel"])
    assert "--repofrompath" not in " ".join(argv)


def test_a_network_mirror_is_not_marked_as_unsigned():
    backend = RedOSInstallBackend()
    backend.bind_install_source("https://repo.example.org/redos/8.0/x86_64/os", "8.0")
    argv = backend.package_install_command("/mnt/poa", ["kernel"])
    assert "--nogpgcheck" not in argv


def test_redos_installs_the_bootloader_by_copying_signed_binaries():
    """grub2-install в RHEL-семействе для EFI отключён патчем Red Hat.

    Найдено живым прогоном: команда отвечает «This utility should not be used
    for EFI platforms because it does not support UEFI Secure Boot». И это по
    существу: собранный на месте grubx64.efi неподписан, а носитель переносной
    — на машине с включённым Secure Boot он не загрузится. Подписанные shim и
    grub приходят готовыми в пакетах, и ставятся копированием.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    commands = [" ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)]
    joined = "\n".join(commands)

    assert "grub2-install" not in joined, "grub2-install на EFI в РЕД ОС не работает"
    # Путь, который прошивка ищет на съёмном носителе.
    assert "EFI/BOOT/BOOTX64.EFI" in joined
    # Shim ищет grub рядом с собой.
    assert "EFI/BOOT/grubx64.efi" in joined
    # NVRAM не трогаем: запись привязала бы носитель к одной машине.
    assert "efibootmgr" not in joined


def test_the_distribution_keeps_the_bootloader_it_was_given():
    """Эталон не должен терять то, что нельзя восстановить.

    Уборка перед упаковкой вычищала boot/efi/EFI — то есть единственный
    загрузчик, который есть у RHEL-семейства, потому что там он приходит
    файлами в пакетах, а не собирается на месте.
    """
    from portable_os_admin.install import distribution

    joined = "\n".join(" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root"))

    assert "boot/efi/EFI" not in joined, "сборка выбрасывает загрузчик из эталона"
    # А вот метка носителя на ESP уникальна и остаться не должна.
    assert "boot/efi/poa" in joined


def test_redos_does_not_put_a_rescue_initramfs_on_the_medium():
    """Второй initramfs занимает 138 МБ из 974 на разделе /boot.

    Замер на собранном носителе. Для переносного носителя аварийный образ
    бесполезен по смыслу: систему, которая не загружается, чинят не с неё же,
    а с другого носителя или из режима восстановления, который здесь свой.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    backend.image_core_packages = ("bash", "dracut-config-rescue", "systemd")

    assert "dracut-config-rescue" not in backend.core_package_names()
    assert "bash" in backend.core_package_names()


def test_grub_config_is_written_where_grub_looks_for_it():
    """Раздел EFI смонтирован в /boot/efi, а не в /boot.

    Ошибка стоила часового прогона: grub2-mkconfig отвечает «нет такого файла»
    про свой временный grub.cfg.new, и по сообщению не догадаться, что не
    найден каталог назначения.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    commands = [" ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)]
    mkconfig = next(c for c in commands if "grub2-mkconfig" in c)

    assert "/boot/efi/EFI/redos/grub.cfg" in mkconfig
    assert "/boot/EFI/" not in mkconfig


def test_boot_entries_are_rewritten_for_a_separate_boot_partition():
    """Пути в записях BLS считаются от файловой системы, где лежит /boot.

    Найдено загрузкой: GRUB отвечал «file `/boot/vmlinuz-…' not found», потому
    что искал его внутри раздела /boot, где ядро лежит в корне. Записи
    написаны при сборке дистрибутива, где /boot — каталог корня (у РЕД ОС это
    раскладка по умолчанию: в anaconda.conf образа только /, /home и swap), а
    на носителе он отдельный раздел.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    commands = [" ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)]
    rewrite = next(c for c in commands if "loader/entries" in c)

    assert "s|^linux /boot/|linux /|" in rewrite
    assert "s|^initrd /boot/|initrd /|" in rewrite
    # И до генерации конфигурации, а не после: иначе grub2-mkconfig прочитает
    # ещё не исправленные записи.
    assert commands.index(rewrite) < next(
        i for i, c in enumerate(commands) if "grub2-mkconfig" in c
    )


def test_kernel_parameters_reach_redos_through_the_boot_entry():
    """В RHEL-семействе командную строку несёт вариант загрузки, а не /etc/default/grub.

    Значение оттуда попадает лишь в запасную переменную kernelopts, которую
    запись со своей строкой `options` перекрывает. Поле, которое пишется
    только в /etc/default/grub, для РЕД ОС не работает вовсе.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    config.boot.kernel_parameters = "nomodeset console=ttyS0,115200"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    commands = "\n".join(
        " ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)
    )
    assert "nomodeset console=ttyS0,115200" in commands
    assert "^options " in commands, "параметры дописываются не в строку options"


def test_redos_menu_does_not_wait_forever():
    """Носитель может оказаться в машине без клавиатуры.

    Меню, ждущее нажатия бесконечно, там означает «не загрузится никогда».
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    grub = get_backend("redos").config_files(config, layout)["etc/default/grub"]
    assert f"GRUB_TIMEOUT={config.boot.menu_timeout_seconds}" in grub
    assert config.boot.menu_timeout_seconds >= 0


def test_redos_tells_the_initramfs_to_unlock_the_volume():
    """Без rd.luks.* initramfs ждёт устройство, которого без расшифровки нет.

    Найдено по последовательному порту: загрузка две минуты ждала
    /dev/disk/by-uuid/… и уходила в аварийный режим с сообщением про
    несуществующий UUID — по нему причину не угадать. В RHEL-семействе
    указание расшифровать том приходит параметром ядра (так пишет anaconda), а
    в Debian ту же роль играет cryptsetup-initramfs, читающий crypttab при
    сборке образа. Поэтому Debian загружался, а РЕД ОС нет.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.arch = "amd64"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-abc", mount_root="/mnt/root"
    )
    layout.luks_uuid = "11111111-2222-3333-4444-555555555555"

    # Без привязки к машине разблокировкой занимается systemd-cryptsetup, и
    # узнаёт он о томе из параметров ядра.
    config.machine_binding.slots = 0
    commands = "\n".join(
        " ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)
    )

    assert f"rd.luks.uuid={layout.luks_uuid}" in commands
    # Имя тома задаётся явно: fstab и crypttab носителя ссылаются на него.
    assert f"rd.luks.name={layout.luks_uuid}=poa-abc" in commands

    # С привязкой этих параметров быть не должно: с ними systemd-cryptsetup
    # спросит пароль сразу, как появится устройство, — задолго до хука,
    # который подставляет ключ машины, и подставлять его будет уже некуда.
    config.machine_binding.slots = 5
    bound = "\n".join(
        " ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)
    )
    assert "rd.luks.uuid" not in bound
    assert "rd.luks.name" not in bound

    # И главное: параметры проставляются ПОСЛЕ grub2-mkconfig. Он
    # перезаписывает строку `options` в записях BLS значением
    # GRUB_CMDLINE_LINUX, и всё, что дописано раньше, теряется молча.
    config.machine_binding.slots = 0
    order = [" ".join(argv) for argv in get_backend("redos").bootloader_commands(layout, config)]
    sed_at = next(i for i, c in enumerate(order) if "rd.luks.uuid" in c)
    mkconfig_at = next(i for i, c in enumerate(order) if "grub2-mkconfig" in c)
    assert sed_at > mkconfig_at, "параметры допишутся до генерации и будут стёрты"

    # А в самом /etc/default/grub они тоже есть — оттуда их и берёт генератор.
    grub_defaults = get_backend("redos").config_files(config, layout)["etc/default/grub"]
    assert f"rd.luks.uuid={layout.luks_uuid}" in grub_defaults

    # И дописывание не повторяет то, что генератор уже поставил. Первая версия
    # дописывала всегда, параметры оказывались в строке дважды, а dracut
    # склеивал два имени тома через пробел и искал службу, которой нет.
    append = next(c for c in order if "loader/entries" in c and "rd.luks.uuid" in c)
    assert "grep -q" in append, "дописывание не проверяет, нет ли параметров уже"


def test_the_boot_screen_is_built_only_from_plugins_the_image_carries():
    """Тема опирается на плагины, которые есть в образе, а не на удобные.

    Первая версия была написана на плагине `script` -- он даёт полный контроль
    над отрисовкой. В образе РЕД ОС его нет вовсе: там `label` и `two-step`, а
    собирать носитель из сети этот проект принципиально не умеет. Тема на
    отсутствующем плагине не загружается молча, и носитель остаётся с немым
    экраном ровно там, где должен разговаривать с человеком.

    Найдено осмотром собранного носителя 29.08.2026: в системе стояли только
    `details`, `text` и `tribar`.
    """
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick", "redos")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-abc", mount_root="/mnt/root"
    )
    files = get_backend("redos").config_files(config, layout)

    theme = files["usr/share/plymouth/themes/poa/poa.plymouth"]
    assert "ModuleName=two-step" in theme
    assert "ModuleName=script" not in theme, "плагина script нет в образе РЕД ОС"
    assert "ScriptFile" not in theme
    # Чёрный фон и белый текст -- весь вид этого экрана.
    assert "BackgroundStartColor=0x000000" in theme
    assert "BackgroundEndColor=0x000000" in theme
    assert "MessageColor=0xffffff" in theme

    # Тема выбрана: без этого plymouth возьмёт тему дистрибутива.
    assert "Theme=poa" in files["etc/plymouth/plymouthd.conf"]

    # Шрифт назван явно. В initramfs шрифтов нет вовсе, и без имени plymouth
    # рисует прямоугольники вместо букв -- проверено осмотром 29.08.2026.
    assert "DejaVu Sans" in theme

    # А сам шрифт и плагин, рисующий текст, кладутся в initramfs руками:
    # модуль plymouth копирует `two-step.so`, но не `label.so`, и без него
    # графический режим не показывает ни строчки -- plymouth молча уходит в
    # текстовый, где кириллицы нет.
    setup = get_backend("redos").recovery_boot_files(layout, config)[
        "usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh"
    ][0]
    assert "label.so" in setup
    assert "DejaVuSans.ttf" in setup
    # fontconfig: Pango в initramfs есть, а разрешать имя шрифта нечем.
    assert "/etc/fonts" in setup

    # И плагины, на которых она держится, установлены поимённо: сам `plymouth`
    # приносит только details, text и tribar.
    packages = get_backend("redos").boot_packages(config)
    assert "plymouth-plugin-two-step" in packages
    assert "plymouth-plugin-label" in packages


def test_both_families_build_a_generic_initramfs():
    """Носитель поедет на машину, которой никто не видел.

    Цена ошибки измерена: собранный по умолчанию образ РЕД ОС занял 21 МБ и не
    содержал ни usb_storage, ни uas — драйверов того самого накопителя, с
    которого он должен грузиться. Ядро не видело флешку, раздел LUKS не
    появлялся, загрузка уходила в аварийный режим, а симптом — «нет устройства
    с таким UUID» — уводил в сторону.

    Требование общее для обоих семейств, потому что общая причина; способ
    разный, поэтому проверяется результат, а не команда.
    """
    from portable_os_admin.install.backends import get_backend

    redos = get_backend("redos")
    debian = get_backend("debian")

    assert "--no-hostonly" in redos.initramfs_command("/mnt/root")

    # И настройка, которая держит образ универсальным при пересборках на самом
    # носителе: без неё первое же автообновление на чужой машине сделало бы
    # носитель незагружаемым на всех остальных.
    redos_conf = redos.generic_initramfs_files()
    debian_conf = debian.generic_initramfs_files()
    assert redos_conf, "РЕД ОС не закрепляет универсальность образа"
    assert debian_conf, "Debian не закрепляет универсальность образа"
    assert "hostonly=no" in "".join(v[0] for v in redos_conf.values())
    assert "MODULES=most" in "".join(v[0] for v in debian_conf.values())


def test_the_generic_setting_reaches_the_medium():
    """Файл должен попасть на носитель, а не остаться намерением."""
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    for family, marker in (("redos", "dracut.conf.d"), ("debian", "initramfs-tools")):
        config = InstallConfig.create_baseline("stick", family)
        config.release = "8.0" if family == "redos" else "trixie"
        layout = build_media_layout(
            plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
        )
        scripted = get_backend(family).scripted_files(config, layout)
        assert any(marker in path for path in scripted), f"{family}: настройка не пишется"


def test_labels_are_set_once_per_distribution_not_per_medium():
    """Систему разворачивают с машины, где мандатного контроля может не быть.

    Тогда файлы попадают в систему без меток, и при первой загрузке она
    замирает: «Failed to mount API filesystems. Freezing execution.» — не
    дойдя даже до служебных файловых систем.

    /.autorelabel не годится: он срабатывает после старта systemd, а без
    меток systemd не стартует. Поэтому метки ставятся при сборке дистрибутива
    — они живут в расширенных атрибутах, которые сохраняют и squashfs, и
    копирование, так что каждый носитель получает их даром.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )
    redos = get_backend("redos")

    relabel = " ".join(" ".join(argv) for argv in redos.relabel_commands("/mnt/root"))
    assert "setfiles" in relabel and "file_contexts" in relabel

    # setfiles, а не restorecon: последний разговаривает с работающим SELinux,
    # которого внутри chroot нет, и молча ничего не делает. Проверено на
    # носителе: SDDM не смог прочитать собственную конфигурацию, потому что у
    # неё не было метки.
    restore = redos.restore_labels_command("/mnt/root", ("/etc", "/boot"))
    assert restore is not None
    assert "setfiles" in " ".join(restore)
    assert "restorecon" not in " ".join(restore)

    files = redos.config_files(config, layout)
    assert "SELINUX=enforcing" in files["etc/selinux/config"]
    assert ".autorelabel" not in files, "этот путь не работает без меток"

    assert get_backend("debian").relabel_commands("/mnt/root") == []


def test_the_kernel_command_line_is_built_in_one_place():
    """Параметр, забытый в одной из копий, проявится только на живой загрузке.

    И по симптому его не найти: сегодня на это ушло два цикла. Поэтому
    командная строка собирается общим кодом, а семейства добавляют лишь то,
    чем у них сообщается о зашифрованном корне.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    for family, release in (("redos", "8.0"), ("debian", "trixie")):
        config = InstallConfig.create_baseline("stick", family)
        config.release = release
        config.boot.kernel_parameters = "intel_iommu=on"
        config.boot.menu_timeout_seconds = 3
        layout = build_media_layout(
            plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
        )
        layout.luks_uuid = "U1"
        backend = get_backend(family)

        grub = backend.config_files(config, layout)["etc/default/grub"]
        assert "intel_iommu=on" in grub, f"{family}: параметры администратора потерялись"
        assert "GRUB_TIMEOUT=3" in grub, f"{family}: таймаут меню не задан"
        # Носитель поедет на чужую машину: чужие диски в меню ему не нужны.
        assert "GRUB_DISABLE_OS_PROBER=true" in grub

    # Про зашифрованный корень сообщают по-разному, и это не стилистика:
    # в Debian ту же роль играет cryptsetup-initramfs, читающий crypttab.
    redos_layout = build_media_layout(
        plan_partitions("/dev/sdb", InstallConfig.create_baseline("x", "redos").disk),
        mapper_name="poa-x", mount_root="/m",
    )
    redos_layout.luks_uuid = "U1"
    assert get_backend("redos").unlock_kernel_parameters(redos_layout)
    assert get_backend("debian").unlock_kernel_parameters(redos_layout) == []


# ----------------------------------------------------------------------
# Ожидание зашифрованных томов при загрузке
# ----------------------------------------------------------------------
def test_service_volumes_get_more_than_the_default_wait():
    """Стоимость argon2id записана сборочной машиной, а платит целевая.

    systemd ждёт устройство 90 секунд и снимает задание. Том, открытие
    которого на слабой машине занимает больше, до этого срока не успевает:
    /var/log не монтируется, auditd не стартует, и всё это выглядит как
    неверный ключ, хотя ключ верный. Замер на стенде: 164 и 206 секунд.

    Проверяются оба файла: crypttab отвечает за исходный раздел, fstab — за
    том, который из него получается. Запаса в одном без другого не хватит.
    """
    from portable_os_admin.install.backends.base import SERVICE_DEVICE_TIMEOUT
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    assert SERVICE_DEVICE_TIMEOUT > 90, "запас меньше значения systemd по умолчанию бессмыслен"

    for family in ("redos", "debian"):
        config = InstallConfig.create_default("stick1", family)
        backend = get_backend(family)
        layout = build_media_layout(
            plan_partitions("/dev/sdb", config.disk),
            mapper_name="poa-x",
            mount_root="/mnt/root",
        )
        files = backend.config_files(config, layout)
        option = f"x-systemd.device-timeout={SERVICE_DEVICE_TIMEOUT}"

        for line in files["etc/fstab"].splitlines():
            if "/var/log" in line or "/mnt/exchange" in line:
                assert option in line, f"{family}: том службы в fstab ждёт по умолчанию: {line}"

        for line in files["etc/crypttab"].splitlines():
            if "-logs" in line or "-exch" in line:
                assert option in line, f"{family}: том службы в crypttab ждёт по умолчанию: {line}"


def test_graphical_boot_parameter_matches_the_family():
    """Regression: `splash` писался обоим семействам, а RHEL слушает `rhgb`.

    Ценой была не заставка: без графического plymouth запрос пароля LUKS
    остаётся текстовым и **без эха**, то есть человек за носителем не видит,
    набралось ли хоть что-нибудь.
    """
    for target_os, expected in (("debian", "splash"), ("astra", "splash"), ("redos", "rhgb")):
        config = InstallConfig.create_default("stick", target_os)
        config.boot.quiet = True
        cmdline = get_backend(target_os).kernel_cmdline(config)
        assert expected in cmdline, target_os
        assert "quiet" in cmdline, target_os

        # И заставка не зависит от `quiet`. Это две разные вещи: `quiet`
        # убирает сообщения ядра, а заставка -- то, чем носитель разговаривает
        # с человеком до загрузки системы. Пока они были связаны, графика
        # пропадала ровно тогда, когда администратор хотел видеть подробности
        # загрузки, и вместе с ней пропадал экран ввода пароля.
        config.boot.quiet = False
        verbose = get_backend(target_os).kernel_cmdline(config)
        assert expected in verbose, target_os
        assert "quiet" not in verbose, target_os
