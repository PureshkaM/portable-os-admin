import inspect
import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.container import ContainerManager
from portable_os_admin.errors import ConfigValidationError, DeviceNotEligibleError
from portable_os_admin.install import BlockDevice, OSInstaller
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install import machine_binding
from portable_os_admin.install.installer import _Progress
from portable_os_admin.session import AdminSession


@pytest.fixture
def session(tmp_path):
    cm = ContainerManager()
    container = cm.create(tmp_path / "admin.container", "CorrectHorse123")
    return AdminSession(container)


@pytest.fixture
def big_removable_device():
    return BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True, mountpoints=(None,)
    )


def test_run_rejects_unvalidated_config(session, big_removable_device):
    config = InstallConfig.create_default("stick1", "debian")  # validation_status == "unchecked"
    installer = OSInstaller(session)
    with pytest.raises(ConfigValidationError):
        installer.run(big_removable_device, config, b"user-passphrase", dry_run=True)


def test_run_rejects_ineligible_device(session, make_config):
    cfgm = ConfigManager()
    config = make_config()
    cfgm.validate(config)

    tiny_device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=100_000_000, model="Tiny", removable=True, mountpoints=(None,)
    )
    installer = OSInstaller(session)
    with pytest.raises(DeviceNotEligibleError):
        installer.run(tiny_device, config, b"user-passphrase", dry_run=True)


def test_successful_dry_run_produces_media_and_recovery_records(session, big_removable_device, make_config):
    cfgm = ConfigManager()
    config = make_config()
    cfgm.validate(config)
    assert config.validation_status == "valid"

    installer = OSInstaller(session)
    result = installer.run(big_removable_device, config, b"user-passphrase-123", dry_run=True)

    assert result.media_record.target_os == "debian"
    # Первый из выданных кодов. Слот зависит от того, сколько машин разрешено:
    # они занимают начало заголовка, восстановления -- весь остаток.
    assert result.recovery_record.slot == machine_binding.recovery_keyslots(config)[0]
    assert result.recovery_record.status == "available"
    assert all(r.executed is False for r in result.plan.results)

    # records must be reflected in the session's container immediately
    assert session.container.find_media(result.media_record.media_id) is not None
    assert session.container.find_recovery(result.media_record.media_id, 7) is not None


def _plan_argvs(session, make_config, target_os="debian", image_path=""):
    cfgm = ConfigManager()
    config = make_config("stick1", target_os)
    cfgm.validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True,
        mountpoints=("/media/user/flash",),
    )
    result = OSInstaller(session).run(
        device, config, b"user-passphrase-123", image_path=image_path, dry_run=True
    )
    return [r.argv for r in result.plan.results], result


def test_target_system_is_actually_written_onto_the_medium(session, make_config):
    """Regression test for the defect that made this project useless in practice.

    The original pipeline formatted the LUKS container and then bootstrapped
    the OS into `/mnt/poa-install-<id>` without ever opening that container or
    mounting anything there -- so the base system landed in a directory on the
    administrator's own machine and the medium stayed empty. Every assertion
    here is a link in the chain that was missing.
    """
    argvs, _ = _plan_argvs(session, make_config)
    flat = [" ".join(a) for a in argvs]

    open_idx = next(i for i, a in enumerate(argvs) if a[0] == "cryptsetup" and "luksOpen" in a)
    mapper = argvs[open_idx][3]
    mkfs_idx = next(i for i, a in enumerate(argvs) if a[0] == "mkfs.ext4" and a[-1].endswith(mapper))
    # Путь к устройству — последний аргумент минус точка монтирования: между
    # `mount` и им теперь стоят параметры записи (noatime и прочее).
    mount_idx = next(
        i for i, a in enumerate(argvs)
        if a[0] == "mount" and any(part.endswith(mapper) for part in a[1:])
    )
    # "debootstrap" is not necessarily argv[0] -- it may be wrapped in eatmydata
    bootstrap_idx = next(i for i, a in enumerate(argvs) if "debootstrap" in a)

    # unlock -> make a filesystem -> mount it -> only then install into it
    assert open_idx < mkfs_idx < mount_idx < bootstrap_idx
    mount_root = argvs[mount_idx][-1]
    assert mount_root in argvs[bootstrap_idx], "bootstrap does not target the mounted medium"

    # the medium is unmounted first: a stick is automounted by the desktop the
    # moment it is plugged in, and partitioning under a live filesystem corrupts it
    assert argvs[0] == ["umount", "/media/user/flash"]

    # a bootable result needs a kernel, an initramfs that can unlock the root,
    # and a bootloader -- debootstrap/dnf install none of the three
    assert any("linux-image" in f for f in flat)
    assert any("cryptsetup-initramfs" in f for f in flat)
    assert any("update-initramfs" in f for f in flat)
    assert any("grub-install" in f for f in flat)

    # and it needs to know what to mount at boot
    assert f"<write-file> {mount_root}/etc/fstab" in flat
    assert f"<write-file> {mount_root}/etc/crypttab" in flat


def test_dangling_resolver_symlink_is_removed_before_it_is_written(session, make_config):
    """На РЕД ОС /etc/resolv.conf -- ссылка на стаб systemd-resolved, которого в
    неподнятом корне не существует, и запись по ней падает с «no such file or
    directory», указывая на файл, хотя каталог на месте.

    Проверяется именно путь установки на носитель: в сборке дистрибутива `rm -f`
    стоял с 27.08.2026, а сюда тот же код скопировали руками и без него. Прогон
    12.09.2026 умер на этом на шаге 11 из 19, когда носитель был уже стёрт.
    """
    argvs, _ = _plan_argvs(session, make_config)
    flat = [" ".join(a) for a in argvs]

    write = next(
        line for line in flat
        if line.startswith("<write-file> ") and line.endswith("/etc/resolv.conf")
    )
    removal = "rm -f " + write.split(" ", 1)[1]
    assert removal in flat, "resolv.conf пишется, но висячая ссылка перед этим не снесена"
    assert flat.index(removal) < flat.index(write)


def test_install_labels_the_whole_tree_on_the_rhel_family(session, make_config):
    """Метки мандатного контроля ставятся по всему корню, а не по нескольким
    каталогам.

    Система разворачивается с машины администратора, где SELinux нет вовсе, и
    файлы приезжают без меток. Проверено на носителе, собранном 12.09.2026: у
    `/etc/passwd` метка есть (его дописывает установщик и метит самопроверка), а
    у `/usr/lib/systemd/systemd` и `/bin/bash` нет ни одной. При
    `SELINUX=enforcing` systemd с непомеченным `/usr` не стартует: загрузка
    дошла до регистрации машины и осталась чёрным экраном на восемнадцать
    минут.
    """
    argvs, _ = _plan_argvs(session, make_config, target_os="redos")
    flat = [" ".join(a) for a in argvs]

    labelling = [line for line in flat if "setfiles" in line]
    assert labelling, "метки не расставляются вовсе"
    assert any("file_contexts /" in line for line in labelling), (
        "проход идёт по отдельным каталогам, а по всему дереву — нет"
    )


def test_everything_opened_by_the_install_is_released_afterwards(session, make_config):
    """A medium left mounted cannot be safely unplugged, and a LUKS mapping left
    open collides with the next install."""
    argvs, _ = _plan_argvs(session, make_config)
    mount_targets = [a[-1] for a in argvs if a[0] == "mount"]
    umounted = [a[-1] for a in argvs if a[0] == "umount"]

    for target in mount_targets:
        assert target in umounted, f"{target} mounted but never unmounted"
    assert any(a[0] == "cryptsetup" and "luksClose" in a for a in argvs)


def test_fstab_and_crypttab_reference_uuids_not_device_names(session):
    """The whole point is a medium that boots on a machine it was not built on,
    where /dev/sdX ordering is different."""
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick1", "debian")
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    layout.root_uuid = "11111111-1111-1111-1111-111111111111"
    layout.boot_uuid = "22222222-2222-2222-2222-222222222222"
    layout.esp_uuid = "3333-4444"
    layout.logs_uuid = "55555555-5555-5555-5555-555555555555"
    layout.exchange_uuid = "66666666-6666-6666-6666-666666666666"
    layout.luks_uuid = "77777777-7777-7777-7777-777777777777"

    files = get_backend("debian").config_files(config, layout)
    fstab = files["etc/fstab"]
    assert "/dev/sd" not in fstab
    assert f"UUID={layout.root_uuid}\t/\t" in fstab
    assert "/boot/efi" in fstab
    assert f"UUID={layout.luks_uuid}" in files["etc/crypttab"]
    assert "poa-abc" in files["etc/crypttab"]


def test_locked_account_still_gets_a_way_to_log_in(session):
    """The account password is deliberately locked (the LUKS passphrase is the
    only front-door secret), so without autologin the booted medium would show
    a login prompt nobody can satisfy."""
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick1", "debian")
    config.account.username = "portable"
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    files = get_backend("debian").config_files(config, layout)

    autologin = files["etc/systemd/system/getty@tty1.service.d/autologin.conf"]
    assert "--autologin portable" in autologin


def test_foreign_architecture_is_refused_only_when_the_machine_cannot_do_it(session, make_config, monkeypatch, tmp_path):
    """Building for another architecture is allowed -- every chroot step runs
    under the emulator, which was verified by running it. What must still fail,
    and fail *before* the medium is erased, is starting on a machine where no
    emulator is registered: there the install would die with "Exec format
    error" deep inside a bootstrap, with the previous contents already gone."""
    import platform

    from portable_os_admin.errors import InstallError
    from portable_os_admin.install import emulation

    config = make_config()
    config.arch = "amd64" if platform.machine() in ("aarch64", "arm64") else "arm64"
    ConfigManager().validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True, mountpoints=()
    )
    # an empty directory stands in for a machine with no registrations at all
    monkeypatch.setattr(emulation, "BINFMT_DIR", str(tmp_path))
    with pytest.raises(InstallError, match="qemu-user-static"):
        OSInstaller(session).run(device, config, b"user-passphrase-123", dry_run=True)


def test_log_and_exchange_volumes_are_encrypted_and_formatted_inside(session, make_config):
    """The same shape of defect as the original one: a partition can be
    LUKS-formatted and then have its filesystem written onto the *raw*
    partition, which silently destroys the header and leaves an unusable
    volume. Order and target are what this pins down."""
    argvs, _ = _plan_argvs(session, make_config)

    def device_of(argv):
        # cryptsetup takes options before the device, so the position varies
        return next(part for part in argv if part.startswith("/dev/"))

    formatted = [device_of(a) for a in argvs if a[0] == "cryptsetup" and "luksFormat" in a]
    # the system volume plus the two service volumes
    assert len(formatted) == 3

    opened = {a[3]: device_of(a) for a in argvs if a[0] == "cryptsetup" and "luksOpen" in a}
    service_mappers = [name for name in opened if name.endswith(("-logs", "-exch"))]
    assert len(service_mappers) == 2, "log and exchange volumes are not opened"

    for mapper in service_mappers:
        partition = opened[mapper]
        assert partition in formatted, f"{mapper} opened on a partition that was never encrypted"
        open_idx = next(i for i, a in enumerate(argvs) if a[0] == "cryptsetup" and "luksOpen" in a and a[3] == mapper)
        mkfs_idx = next(
            i for i, a in enumerate(argvs) if a[0].startswith("mkfs") and a[-1] == f"/dev/mapper/{mapper}"
        )
        assert open_idx < mkfs_idx, f"{mapper}: filesystem made before the volume was opened"
        # and never onto the bare partition, which would wipe the LUKS header
        assert not any(a[0].startswith("mkfs") and a[-1] == partition for a in argvs)


def test_each_service_volume_also_holds_the_administrator_key(session, make_config):
    """Two keyslots per service volume is the whole mechanism: the medium opens
    them from a key file on its own encrypted root, the administrator opens
    them with a key that exists only in the administrator container."""
    argvs, _ = _plan_argvs(session, make_config)
    added = [a for a in argvs if a[0] == "cryptsetup" and "luksAddKey" in a]
    # По одному administrator-ключу на каждый служебный том плюс коды
    # восстановления на системном: их столько, сколько keyslot остаётся после
    # слотов машин, потому что каждый расходуется безвозвратно.
    recovery = len(machine_binding.recovery_keyslots(make_config()))
    assert len(added) == recovery + 2


def test_the_boot_key_file_lands_on_the_encrypted_root(session, make_config):
    """It must be inside the LUKS root -- reachable only after the user's
    passphrase has already unlocked it -- and readable by root alone."""
    argvs, result = _plan_argvs(session, make_config)
    flat = [" ".join(a) for a in argvs]
    key_writes = [line for line in flat if line.startswith("<write-file>") and "etc/poa/service.key" in line]
    assert key_writes, "no key file written; the medium could not open its own volumes at boot"

    entry = next(r for r in result.plan.results if "etc/poa/service.key" in " ".join(r.argv))
    assert entry.argv[1].endswith("/etc/poa/service.key")


def test_crypttab_opens_the_service_volumes_from_that_key_file(session):
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_default("stick1", "debian")
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    layout.luks_uuid = "11111111-1111-1111-1111-111111111111"
    layout.logs_luks_uuid = "22222222-2222-2222-2222-222222222222"
    layout.exchange_luks_uuid = "33333333-3333-3333-3333-333333333333"
    crypttab = get_backend("debian").config_files(config, layout)["etc/crypttab"]

    lines = [line for line in crypttab.splitlines() if line and not line.startswith("#")]
    assert len(lines) == 3
    # the root still asks a human; the service volumes never do
    assert lines[0].split("\t")[2] == "none"
    for line in lines[1:]:
        assert line.split("\t")[2] == "/etc/poa/service.key"
        # a damaged service volume must not strand the user in an emergency
        # shell they have no password for
        assert "nofail" in line


def test_the_medium_record_keeps_the_key_and_the_volume_uuids(session, make_config):
    """Without these the volumes can never be opened from an administrator
    machine again -- there is no second copy of that key anywhere."""
    import base64

    _, result = _plan_argvs(session, make_config)
    record = result.media_record
    assert record.service_key_b64
    assert len(base64.b64decode(record.service_key_b64)) >= 32
    assert record.logs_luks_uuid and record.exchange_luks_uuid
    assert record.logs_luks_uuid != record.exchange_luks_uuid


def test_service_volumes_are_closed_when_the_install_ends(session, make_config):
    argvs, _ = _plan_argvs(session, make_config)
    opened = {a[3] for a in argvs if a[0] == "cryptsetup" and "luksOpen" in a}
    closed = {a[-1] for a in argvs if a[0] == "cryptsetup" and "luksClose" in a}
    assert opened <= closed, f"left open: {opened - closed}"


def test_installing_from_the_network_needs_a_repository_and_a_release(session, make_config):
    """A local image is the intended source; the network is the fallback. It
    has to be chosen deliberately -- an empty image plus an empty mirror is a
    mistake, and saying so beats debootstrap failing on an empty URL."""
    from portable_os_admin.errors import InstallError
    from portable_os_admin.install.image_source import MountedImage

    installer = OSInstaller.__new__(OSInstaller)
    installer._progress = _Progress(None)
    config = make_config()
    config.mirror = ""

    with pytest.raises(InstallError, match="сетевого репозитория"):
        installer._prepare_source(
            MountedImage("/nonexistent.iso", mountpoint="/mnt/poa-image-test"),
            "",
            config,
            get_backend(config.target_os),
            dry_run=False,
        )


def test_installing_from_the_network_uses_the_configured_mirror(session, make_config):
    from portable_os_admin.install.image_source import MountedImage

    installer = OSInstaller.__new__(OSInstaller)
    installer._progress = _Progress(None)
    config = make_config()
    config.mirror = "https://mirror.example.org/debian"
    config.release = "trixie"

    release, source = installer._prepare_source(
        MountedImage("/nonexistent.iso", mountpoint="/mnt/poa-image-test"),
        "",
        config,
        get_backend(config.target_os),
        dry_run=False,
    )
    assert source == "https://mirror.example.org/debian"
    assert release == "trixie"


def test_without_an_image_the_release_has_to_come_from_the_configuration(session, make_config):
    """With an image the release is read off it, which is what keeps version
    numbers out of configurations. Without one there is nowhere else to get it,
    so the configuration must say -- and if it does not, that is worth an error
    rather than an empty suite name handed to debootstrap."""
    from portable_os_admin.errors import InstallError
    from portable_os_admin.install.image_source import MountedImage

    installer = OSInstaller.__new__(OSInstaller)
    installer._progress = _Progress(None)
    config = make_config()
    config.mirror = "https://mirror.example.org/debian"
    config.release = ""

    with pytest.raises(InstallError, match="выпуск"):
        installer._prepare_source(
            MountedImage("/nonexistent.iso", mountpoint="/mnt/poa-image-test"),
            "",
            config,
            get_backend(config.target_os),
            dry_run=False,
        )


def test_the_image_is_visible_from_inside_the_chroot(session, make_config):
    """debootstrap points the target's sources.list at the URL it was given --
    file:///mnt/poa-image-<id> -- and that path exists only on the
    administrator's machine. Without a bind mount into the target tree,
    apt-get inside the chroot finds no packages at all and the medium ends up
    with no kernel and no bootloader. Found by running an install from a local
    image to the end for the first time."""
    argvs, _ = _plan_argvs(session, make_config, image_path="/images/debian.iso")

    binds = [a for a in argvs if a[0] == "mount" and "--bind" in a and "poa-image-" in a[-1]]
    assert binds, "the image is never made visible inside the chroot"

    bind = binds[0]
    source, target = bind[2], bind[3]
    # same path on both sides, or the file:// URL still does not resolve
    assert target.endswith(source), f"{target} does not expose {source} at its own path"

    bind_idx = argvs.index(bind)
    refresh_idx = next(i for i, a in enumerate(argvs) if "apt-get" in a and "update" in a)
    assert bind_idx < refresh_idx, "the image is mounted after apt has already looked for packages"


def test_apt_in_the_chroot_is_told_where_the_packages_are(session, make_config):
    """After a bootstrap from a local image the target's sources.list is empty:
    debootstrap writes it for an http mirror and not for a file:// one. Checked
    on the medium after a real run, where the consequence was "Unable to locate
    package linux-image-amd64" for every boot package."""
    argvs, _ = _plan_argvs(session, make_config, image_path="/images/debian.iso")
    flat = [" ".join(a) for a in argvs]

    write_idx = next(i for i, line in enumerate(flat) if line.startswith("<write-file>") and line.endswith("/etc/apt/sources.list"))
    refresh_idx = next(i for i, a in enumerate(argvs) if "apt-get" in a and "update" in a)
    assert write_idx < refresh_idx, "apt is asked for packages before it is told where they are"


def test_a_local_image_is_trusted_explicitly_because_it_carries_no_signature():
    """An installation image has dists/<suite>/Release and no signature over
    it, and apt drops an unsigned repository entirely rather than warning."""
    backend = get_backend("debian")
    _, text = backend.install_time_sources("trixie", "file:///mnt/poa-image-x", ("main", "contrib"))
    assert "[trusted=yes]" in text
    assert "main contrib" in text

    # a network mirror is signed, and must not be marked trusted blindly
    _, text = backend.install_time_sources("trixie", "https://deb.debian.org/debian", ("main",))
    assert "[trusted=yes]" not in text


def test_medium_is_not_reported_as_erased_when_nothing_was_written(session, make_config):
    """Regression: the interface announced an erased medium on every failure.

    Several failures land before the first write -- a device too small, an
    image of the wrong family or architecture, no emulator registered for the
    target. Telling an administrator their stick was wiped when it was intact
    costs them the data they then do not try to recover.
    """
    cfgm = ConfigManager()
    config = make_config()
    cfgm.validate(config)

    tiny_device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=100_000_000, model="Tiny", removable=True,
        mountpoints=(None,),
    )
    installer = OSInstaller(session)
    assert installer.medium_modified is False

    with pytest.raises(DeviceNotEligibleError):
        installer.run(tiny_device, config, b"user-passphrase", dry_run=True)

    assert installer.medium_modified is False, "носитель объявлен изменённым, хотя запись не начиналась"


def test_a_dry_run_never_claims_the_medium_was_touched(session, big_removable_device, make_config):
    cfgm = ConfigManager()
    config = make_config()
    cfgm.validate(config)

    installer = OSInstaller(session)
    installer.run(big_removable_device, config, b"user-passphrase-123", dry_run=True)

    assert installer.medium_modified is False


def test_a_slow_medium_is_given_time_to_answer_a_probe():
    """Проба blkid не должна убивать развёртывание таймаутом.

    Найдено живым прогоном: развёртывание падало на шаге 12 из 14, после часа
    записи, потому что `_read_output` жёстко задавал 30 секунд. Проба
    читающая, но читает она носитель, в очередь которого ещё сливаются
    гигабайты записанного, и на дешёвой флешке чтение встаёт за этой
    очередью.
    """
    from portable_os_admin.install import installer as installer_module

    assert installer_module.PROBE_TIMEOUT >= 600


def test_teardown_waits_for_the_volume_instead_of_giving_up_silently(tmp_path, monkeypatch):
    """Прерванный прогон не должен оставлять открытый том.

    Прежняя уборка делала `umount -l` и сразу `luksClose`: ленивое
    размонтирование отцепляет точку сразу, но том освобождается, только когда
    допишется кэш — на медленном носителе это минуты. Закрытие натыкалось на
    «устройство занято», ошибка съедалась, и от прогона оставался открытый
    /dev/mapper, из-за которого следующий запуск отказывался работать.
    """
    from portable_os_admin.install import installer as installer_module

    assert installer_module.TEARDOWN_CLOSE_ATTEMPTS * installer_module.TEARDOWN_CLOSE_PAUSE >= 60

    source = inspect.getsource(installer_module.OSInstaller._teardown)
    # Обычное размонтирование первым, ленивое — только запасным путём.
    assert '["umount", mountpoint]' in source
    assert source.index('["umount", mountpoint]') < source.index('["umount", "-l", mountpoint]')
    # И кэш выгоняется до того, как что-то размонтируется.
    assert source.index('["sync"]') < source.index('["umount", mountpoint]')


def test_an_abort_stops_the_running_command_and_refuses_the_next(tmp_path):
    """Прерывание должно доходить до запущенного процесса, а не только до экрана.

    Раньше остановить начатую запись было нечем: CommandRunner не хранил
    ссылку на дочерний процесс, и единственным способом оставалось закрыть
    программу целиком.
    """
    from portable_os_admin.errors import InstallError
    from portable_os_admin.install.command_runner import (
        CommandPlan,
        CommandRunner,
        InstallAborted,
    )

    runner = CommandRunner()
    plan = CommandPlan(device="/dev/null")

    runner.abort()
    assert runner.aborted is True

    # После прерывания следующая команда не начинается вовсе.
    with pytest.raises(InstallAborted):
        runner.run(["true"], plan, dry_run=False, confirm_device="/dev/null")

    # И это отдельный тип, а не «ошибка команды»: обходиться с ним надо иначе.
    assert issubclass(InstallAborted, InstallError)


def test_filesystems_are_made_readable_by_the_target_not_just_by_this_machine():
    """Форматирует одна система, а читать будет другая.

    Найдено загрузкой: Debian trixie (e2fsprogs 1.47) включает по умолчанию
    orphan_file, а e2fsck из РЕД ОС 8 (1.46) о такой возможности не знает и
    отказывается проверять раздел — «has unsupported feature(s): FEATURE_C12,
    fsck failed with exit status 12». systemd после этого не монтирует корень
    и уходит в аварийный режим, хотя сама файловая система исправна.

    Требование общее для обоих семейств, поэтому проверяется на уровне
    установщика, а не бэкенда.
    """
    from portable_os_admin.install import installer as installer_module

    assert "^orphan_file" in installer_module.CONSERVATIVE_EXT4_FEATURES
    assert "^metadata_csum_seed" in installer_module.CONSERVATIVE_EXT4_FEATURES

    source = inspect.getsource(installer_module.OSInstaller)
    made = [line for line in source.splitlines() if "mkfs.ext4" in line]
    assert made, "файловые системы больше не создаются — тест потерял смысл"
    for line in made:
        assert "CONSERVATIVE_EXT4_FEATURES" in line, (
            "раздел создаётся с возможностями по умолчанию: " + line.strip()
        )


_PUBLIC_KEY = (
    "-----BEGIN PUBLIC KEY-----\n"
    "MCowBQYDK2VwAyEAVDgvWTswVDgvWTswVDgvWTswVDgvWTswVDgvWTswVDg=\n"
    "-----END PUBLIC KEY-----\n"
)


def _revoking_config(make_config):
    config = make_config()
    config.revocation.enabled = True
    config.revocation.url = "https://revoke.example.org"
    ConfigManager().validate(config)
    return config


def test_the_install_path_writes_the_revocation_key(session, big_removable_device, make_config):
    """Оба пути записи носителя обязаны положить ключ ресурса отзыва.

    До 13.09.2026 его писал только путь развёртывания. Носитель, собранный
    «Установкой ОС», уезжал со службой отзыва, скриптом и адресом ресурса -- и
    без ключа: проверка при каждой загрузке писала «нет открытого ключа» и
    выходила. Найдено осмотром собранного носителя, не тестом.
    """
    config = _revoking_config(make_config)
    session.container.data.revocation_public_key_pem = _PUBLIC_KEY

    result = OSInstaller(session).run(
        big_removable_device, config, b"user-passphrase-123", dry_run=True
    )

    written = [r.argv[1] for r in result.plan.results if r.argv[0] == "<write-file>"]
    assert any(path.endswith("etc/poa/revocation.pub") for path in written), (
        "носитель уезжает с проверкой отзыва, но без ключа, которым её проверять"
    )


def test_a_container_without_the_resource_key_refuses_to_write_a_medium(
    session, big_removable_device, make_config
):
    """Молчаливый носитель хуже отказа: он выглядит защищённым и не проверяет
    ничего."""
    config = _revoking_config(make_config)

    with pytest.raises(Exception) as error:
        OSInstaller(session).run(
            big_removable_device, config, b"user-passphrase-123", dry_run=True
        )
    assert "ресурс" in str(error.value).lower()


def test_the_medium_gets_the_revocation_service_switched_on(session, big_removable_device, make_config):
    """Файлы без автозапуска -- это проверка, которой не бывает.

    Носитель РЕД ОС 14.09.2026 уехал со скриптом отзыва и открытым ключом, но
    без ссылки в multi-user.target.wants: служба не запускалась ни разу, и в
    журнале носителя не было ни строчки. Включение живёт там же, где запись
    файлов, и потому одинаково работает на обоих путях.
    """
    config = _revoking_config(make_config)
    session.container.data.revocation_public_key_pem = _PUBLIC_KEY

    result = OSInstaller(session).run(
        big_removable_device, config, b"user-passphrase-123", dry_run=True
    )

    links = [
        " ".join(r.argv) for r in result.plan.results
        if r.argv[0] == "ln" and "poa-revocation.service" in " ".join(r.argv)
    ]
    assert links, "служба отзыва не включается на носителе"
    assert any("multi-user.target.wants" in line for line in links)
