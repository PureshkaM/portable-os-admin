"""Building a distribution once and deploying it to many media.

The property these tests exist to protect is not "the files are written" but
"two media made from one distribution are two systems". That is decided by a
handful of lines -- what gets stripped before the archive is made, and what is
generated per medium -- and getting it wrong produces media that install
perfectly and share a master key.
"""
from __future__ import annotations

import json
import os

import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.config.schema import KioskConfig
from portable_os_admin.container import ContainerManager
from portable_os_admin.errors import ConfigValidationError, InstallError
from portable_os_admin.install import BlockDevice, OSInstaller, distribution
from portable_os_admin.session import AdminSession


@pytest.fixture
def session(tmp_path):
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    return AdminSession(container)


@pytest.fixture
def kiosk_config():
    config = InstallConfig.create_baseline("kiosk-stick", "debian")
    config.release = "trixie"
    config.packages = ["mate-calc"]
    config.kiosk = KioskConfig(enabled=True)
    ConfigManager().validate(config)
    return config


@pytest.fixture
def big_device():
    return BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer",
        removable=True, mountpoints=(None,),
    )


# ----------------------------------------------------------------------
# What must never travel in the artefact
# ----------------------------------------------------------------------
def test_nothing_secret_or_identifying_survives_into_the_archive():
    """The archive is a build artefact, not a secret.

    Each of these is either a key or something that has to differ between two
    media. A distribution carrying any of them turns ten sticks into one
    system with ten copies.
    """
    commands = [" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root")]
    joined = "\n".join(commands)

    for path in (
        "etc/poa/service.key",      # opens the log and exchange volumes
        "etc/poa/recovery",         # recovery code salt
        "etc/fstab",                # addresses volumes by UUID
        "etc/crypttab",
        "etc/ssh/ssh_host_rsa_key",  # shared host keys let sticks impersonate each other
        "etc/ssh/ssh_host_ed25519_key",
    ):
        assert path in joined, f"{path} остаётся в эталоне"


def test_machine_id_is_emptied_rather_than_deleted():
    """systemd regenerates it when the file exists and is empty.

    A *missing* machine-id is a different situation and not the one wanted
    here, so this is not a detail that can be simplified away.
    """
    commands = [" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root")]

    machine_id = [c for c in commands if "machine-id" in c]
    assert machine_id, "machine-id не обрабатывается вовсе"
    for command in machine_id:
        assert ": >" in command, "machine-id удаляется, а должен опустошаться"
        assert "rm -rf /mnt/root/etc/machine-id" not in command


def test_build_caches_are_dropped():
    """Dead weight on every medium made from this distribution."""
    joined = "\n".join(" ".join(argv) for argv in distribution.sanitise_commands("/mnt/root"))

    for path in ("var/cache/apt/archives", "var/lib/apt/lists", "var/log"):
        assert path in joined


def test_log_directories_survive_the_cleanup(tmp_path):
    """Журналы удаляются, каталоги под них — нет.

    Каталоги в /var/log создают пакеты при установке, каждый со своими
    правами и меткой. Служба, не нашедшая своего каталога, не создаёт его —
    она отказывается стартовать. На РЕД ОС так падал auditd, а записи аудита,
    которым некуда деться, заливали консоль.

    Команды здесь именно выполняются, а не сверяются по тексту: разница между
    `rm -rf` и `find -delete` видна только по результату.
    """
    import subprocess

    root = tmp_path / "root"
    (root / "var/log/audit").mkdir(parents=True)
    (root / "var/log/audit/audit.log").write_text("старый журнал сборки")
    (root / "var/log/messages").write_text("тоже сборочный")
    (root / "var/log/sssd").mkdir()

    for argv in distribution.sanitise_commands(str(root)):
        subprocess.run(argv, check=False)

    assert (root / "var/log/audit").is_dir(), "каталог auditd не пережил очистку"
    assert (root / "var/log/sssd").is_dir()
    assert not (root / "var/log/audit/audit.log").exists(), "журнал сборки уехал на носитель"
    assert not (root / "var/log/messages").exists()


# ----------------------------------------------------------------------
# Integrity
# ----------------------------------------------------------------------
def test_checksum_file_is_readable_by_sha256sum(tmp_path):
    """So an administrator can verify a distribution without trusting us."""
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents" * 100)

    path, digest = distribution.write_checksum(str(archive))

    assert path.endswith(".sha256")
    line = open(path, encoding="utf-8").read().strip()
    # `<digest>  <basename>` is what the tool expects.
    assert line == f"{digest}  rootfs.tar.zst"
    assert len(digest) == 64


def test_a_damaged_distribution_is_refused_before_anything_is_erased(tmp_path):
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"original contents")
    metadata = distribution.DistributionMetadata(
        archive="rootfs.tar.zst", sha256=distribution.sha256_of(str(archive))
    )
    distribution.write_metadata(str(tmp_path), metadata)

    # Verifies while intact...
    assert distribution.verify_archive(str(tmp_path), metadata)

    # ...and refuses once damaged.
    archive.write_bytes(b"corrupted contents")
    with pytest.raises(InstallError) as excinfo:
        distribution.verify_archive(str(tmp_path), metadata)
    assert "не совпала" in str(excinfo.value)
    assert "носитель не изменён" in str(excinfo.value).lower()


def test_a_distribution_without_a_checksum_is_refused(tmp_path):
    """Older artefacts, or a metadata file edited by hand."""
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents")
    metadata = distribution.DistributionMetadata(archive="rootfs.tar.zst", sha256="")

    with pytest.raises(InstallError) as excinfo:
        distribution.verify_archive(str(tmp_path), metadata)
    assert "контрольной суммы" in str(excinfo.value)


# ----------------------------------------------------------------------
# Подпись администратора
# ----------------------------------------------------------------------
SECRET = bytes(range(32))
OTHER_SECRET = bytes(range(1, 33))


def _built_distribution(directory):
    """Дистрибутив так, как его оставляет сборка: архив, .sha256, описание."""
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"root filesystem" * 50)
    _, digest = distribution.write_checksum(str(archive))
    metadata = distribution.DistributionMetadata(name="kiosk-stick", sha256=digest)
    distribution.write_metadata(str(directory), metadata)
    return archive


def _edit_metadata(directory, **changes):
    path = directory / distribution.METADATA_NAME
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def _deployable(directory, kiosk_config):
    """Дистрибутив, который установщик согласится развернуть сухим прогоном."""
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)
    distribution.write_checksum(str(archive))
    return directory


def test_the_installer_refuses_a_distribution_whose_signature_is_broken(
    session, kiosk_config, big_device, tmp_path
):
    """Проверка в мастере -- не единственная: установщик читает описание сам."""
    directory = _deployable(tmp_path / "dist", kiosk_config)
    distribution.sign_distribution(str(directory), session.container.admin_secret)
    _edit_metadata(directory, name="not-what-was-signed")

    with pytest.raises(InstallError) as error:
        OSInstaller(session).deploy_distribution(
            big_device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
        )
    assert "подпись" in str(error.value)


def test_the_installer_refuses_when_the_verdict_changed_after_consent(
    session, kiosk_config, big_device, tmp_path
):
    """Согласие давалось на подписанный дистрибутив, а к записи подпись исчезла."""
    directory = _deployable(tmp_path / "dist", kiosk_config)
    signature = distribution.sign_distribution(str(directory), session.container.admin_secret)
    accepted = distribution.verify_distribution(str(directory), session.container.admin_secret)
    assert accepted.status == distribution.SIGNED_BY_THIS_CONTAINER

    os.remove(signature)
    with pytest.raises(InstallError):
        OSInstaller(session).deploy_distribution(
            big_device, str(directory), b"user-passphrase", dry_run=True,
            confirm_device="/dev/sdb", accepted_signature=accepted,
        )


def test_the_installer_writes_a_distribution_signed_by_this_container(
    session, kiosk_config, big_device, tmp_path
):
    directory = _deployable(tmp_path / "dist", kiosk_config)
    distribution.sign_distribution(str(directory), session.container.admin_secret)
    accepted = distribution.verify_distribution(str(directory), session.container.admin_secret)

    result = OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"user-passphrase", dry_run=True,
        confirm_device="/dev/sdb", accepted_signature=accepted,
    )
    assert result.media_record.media_id


def test_a_distribution_signed_by_this_container_verifies(tmp_path):
    archive = _built_distribution(tmp_path)
    path = distribution.sign_distribution(str(tmp_path), SECRET)

    assert path == f"{archive}.sig"
    lines = open(path, encoding="utf-8").read().splitlines()
    assert lines[0] == distribution.SIGNATURE_HEADER
    assert lines[1] == f"key {distribution.key_fingerprint(SECRET)}"

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.SIGNED_BY_THIS_CONTAINER
    assert verdict.fingerprint == distribution.key_fingerprint(SECRET)


def test_reformatting_the_description_does_not_break_the_signature(tmp_path):
    """Подписан смысл описания, а не отступы, которыми его записали."""
    _built_distribution(tmp_path)
    distribution.sign_distribution(str(tmp_path), SECRET)
    _edit_metadata(tmp_path)  # тот же словарь, другое форматирование

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.SIGNED_BY_THIS_CONTAINER


def test_a_distribution_without_a_signature_is_unsigned(tmp_path):
    """Так выглядят все дистрибутивы 1.0.0."""
    _built_distribution(tmp_path)

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.UNSIGNED


def test_a_signature_by_another_container_is_foreign_with_its_fingerprint(tmp_path):
    _built_distribution(tmp_path)
    distribution.sign_distribution(str(tmp_path), OTHER_SECRET)

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.FOREIGN
    assert verdict.fingerprint == distribution.key_fingerprint(OTHER_SECRET)


def test_a_replaced_archive_digest_is_invalid(tmp_path):
    """Подмена архива вместе с хешем в описании и в .sha256 -- ради этого подпись."""
    archive = _built_distribution(tmp_path)
    distribution.sign_distribution(str(tmp_path), SECRET)

    archive.write_bytes(b"substituted root")
    _, digest = distribution.write_checksum(str(archive))
    _edit_metadata(tmp_path, sha256=digest)

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.INVALID


def test_a_checksum_file_disagreeing_with_the_description_is_invalid(tmp_path):
    archive = _built_distribution(tmp_path)
    distribution.sign_distribution(str(tmp_path), SECRET)
    (tmp_path / f"{archive.name}.sha256").write_text(f"{'0' * 64}  {archive.name}\n")

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.INVALID


@pytest.mark.parametrize(
    "changes",
    [{"profile": "kiosk"}, {"has_bootloader": True}, {"added_by_hand": "x"}],
)
def test_an_edited_description_is_invalid(tmp_path, changes):
    """Включая ключ, которого программа не знает: from_dict его отбросил бы."""
    _built_distribution(tmp_path)
    distribution.sign_distribution(str(tmp_path), SECRET)
    _edit_metadata(tmp_path, **changes)

    verdict = distribution.verify_distribution(str(tmp_path), SECRET)
    assert verdict.status == distribution.INVALID


def test_a_damaged_signature_is_invalid(tmp_path):
    import base64

    archive = _built_distribution(tmp_path)
    path = distribution.sign_distribution(str(tmp_path), SECRET)
    header, key_line, sig_line = open(path, encoding="utf-8").read().splitlines()
    raw = bytearray(base64.b64decode(sig_line.split(" ", 1)[1]))
    raw[0] ^= 0x01
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"{header}\n{key_line}\nsignature {base64.b64encode(bytes(raw)).decode()}\n")

    assert distribution.verify_distribution(str(tmp_path), SECRET).status == distribution.INVALID

    (tmp_path / f"{archive.name}.sig").write_text("мусор\n", encoding="utf-8")
    assert distribution.verify_distribution(str(tmp_path), SECRET).status == distribution.INVALID


def test_the_distribution_key_is_not_the_revocation_authority_key():
    """Один секрет, разные назначения -- разные ключи."""
    from portable_os_admin.admin import revocation_client

    distribution_raw = distribution._raw_public(distribution.signing_key(SECRET).public_key())
    authority_raw = distribution._raw_public(revocation_client.signing_key(SECRET).public_key())
    assert distribution_raw != authority_raw
    assert distribution.key_fingerprint(SECRET) != revocation_client.key_id(SECRET)


def test_a_newer_format_is_refused_with_an_explanation(tmp_path):
    archive = tmp_path / "rootfs.tar.zst"
    archive.write_bytes(b"contents")
    data = distribution.DistributionMetadata(archive="rootfs.tar.zst").to_dict()
    data["format_version"] = distribution.FORMAT_VERSION + 5
    (tmp_path / distribution.METADATA_NAME).write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(InstallError) as excinfo:
        distribution.read_metadata(str(tmp_path))
    assert "более новой версией" in str(excinfo.value)


def test_a_directory_that_is_not_a_distribution_says_so(tmp_path):
    with pytest.raises(InstallError) as excinfo:
        distribution.read_metadata(str(tmp_path))
    assert "не дистрибутив" in str(excinfo.value)


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------
def test_building_touches_no_medium(session, kiosk_config, tmp_path):
    """The long half of the work is also the safe half."""
    installer = OSInstaller(session)

    result = installer.build_distribution(
        kiosk_config, str(tmp_path / "dist"), image_path="", dry_run=True
    )

    assert installer.medium_modified is False
    # The *command* being run, not the whole line: "cryptsetup" also appears
    # as the name of a package being installed, which is not the same thing at
    # all and would make this test fail for the wrong reason.
    programs = {os.path.basename(r.argv[0]) for r in result.plan.results if r.argv}
    for destructive in ("wipefs", "sgdisk", "parted", "cryptsetup", "mkfs.ext4", "mkfs.vfat"):
        assert destructive not in programs, f"сборка выполняет {destructive}"


def test_the_build_writes_every_script_it_then_runs(session, kiosk_config, tmp_path):
    """Каждый скрипт, который сборка запускает в chroot, она должна и положить.

    Найдено живым прогоном, не тестами: сборка дистрибутива писала только
    `temporary_files`, а `scripted_files` — где лежит `poa-kiosk-apply` —
    оставалась за путём обычной установки. Прогон падал на шаге «Создание
    учётной записи и служб» с «нет такого файла» — после того, как базовая
    система и всё графическое окружение уже установлены, то есть ценой
    двадцати минут.

    Проверка нарочно общая: она смотрит не на конкретное имя, а на само
    свойство «запускаем то, что положили», и поймает следующий скрипт,
    добавленный в chroot-шаги и забытый в файлах.
    """
    result = OSInstaller(session).build_distribution(
        kiosk_config, str(tmp_path / "dist"), image_path="", dry_run=True
    )

    written = {
        entry.argv[1] for entry in result.plan.results
        if entry.argv and entry.argv[0] == "<write-file>"
    }
    # `backend.chroot(root, ...)` даёт argv вида ["chroot", <корень>, ...], так
    # что корень берётся из самой команды, а не угадывается.
    executed_scripts = [
        (entry.argv[1], entry.argv[-1]) for entry in result.plan.results
        if entry.argv and os.path.basename(entry.argv[0]) == "chroot"
        and entry.argv[-1].startswith("/") and entry.argv[-2] in ("/bin/sh", "sh", "/bin/bash")
    ]

    assert executed_scripts, "сборка киоска перестала запускать скрипты — тест потерял смысл"
    for root, script in executed_scripts:
        assert f"{root}{script}" in written, (
            f"сборка запускает {script}, но нигде его не пишет"
        )


def test_building_refuses_an_unvalidated_configuration(session, tmp_path):
    config = InstallConfig.create_baseline("stick", "debian")  # validation_status unchecked

    with pytest.raises(ConfigValidationError):
        OSInstaller(session).build_distribution(config, str(tmp_path), dry_run=True)


def test_the_metadata_describes_what_was_built(session, kiosk_config, tmp_path):
    """Read back on deployment to refuse a mismatch before erasing anything."""
    kiosk_config.arch = "amd64"
    kiosk_config.kiosk.desktop_environment = "mate"
    ConfigManager().validate(kiosk_config)

    result = OSInstaller(session).build_distribution(
        kiosk_config, str(tmp_path / "dist"), image_path="", dry_run=True
    )

    assert result.metadata.profile == "kiosk"
    assert result.metadata.desktop_environment == "mate"
    assert result.metadata.arch == "amd64"
    assert result.metadata.target_os == "debian"
    # The configuration travels whole, so deployment writes fstab and the
    # account exactly as the build intended.
    assert result.metadata.config["packages"] == ["mate-calc"]


def test_the_image_keeps_ownership_and_extended_attributes():
    """Разворачивается на машине, чей /etc/passwd — не целевой.

    squashfs хранит владельцев численно сам, а вот xattr пишутся только по
    явному ключу — без них у программ пропадают capabilities, и понять это по
    симптомам трудно. Копирование наружу должно сохранять то же самое.
    """
    argv = distribution.archive_command("/mnt/root", "/out/rootfs.squashfs", compressor="zstd")

    assert argv[0] == "mksquashfs"
    assert "-xattrs" in argv
    assert "-noappend" in argv

    mount = distribution.mount_command("/d/rootfs.squashfs", "/mnt/dist")
    assert "ro" in " ".join(mount) and "squashfs" in mount
    # -a — это --preserve=all: владельцы, права, времена, ACL и xattr.
    assert distribution.copy_command("/mnt/dist", "/mnt/target")[:2] == ["cp", "-a"]


def test_a_version_1_distribution_still_deploys():
    """Дистрибутив, собранный до перехода на squashfs, должен разворачиваться.

    Формат образа — деталь переноса эталона, а не свойство носителя, и менять
    его нельзя ценой того, что уже собранное перестаёт ставиться.
    """
    assert distribution.is_squashfs("rootfs.squashfs") is True
    for legacy in distribution.LEGACY_ARCHIVE_NAMES:
        assert distribution.is_squashfs(legacy) is False
        assert "--numeric-owner" in distribution.unpack_command(f"/d/{legacy}", "/mnt/target")


# ----------------------------------------------------------------------
# Deploying
# ----------------------------------------------------------------------
def test_deploying_checks_the_distribution_before_erasing(session, big_device, tmp_path):
    """A missing distribution must not cost a medium."""
    with pytest.raises(InstallError):
        OSInstaller(session).deploy_distribution(
            big_device, str(tmp_path / "nothing-here"), b"passphrase", dry_run=True
        )


def test_deploying_generates_the_encryption_afresh(session, kiosk_config, big_device, tmp_path):
    """The property the whole split rests on.

    A byte-for-byte copy of a finished medium shares its LUKS master key, so
    breaking one breaks all. Deployment therefore runs luksFormat itself,
    per medium.
    """
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    result = OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
    )

    commands = [" ".join(r.argv) for r in result.plan.results]
    assert any("luksFormat" in c for c in commands), "шифрование не создаётся заново"
    assert any("mount" in c and "squashfs" in c for c in commands), "образ не монтируется"
    # Система кладётся на носитель одним файлом: `cp` образа в контейнер, а не
    # раскладка дерева. Дерево остаётся способом на случай `system_image=нет`.
    assert any("system.squashfs" in c and c.startswith("cp ") for c in commands), (
        "образ системы не попадает в контейнер"
    )
    # And the medium gets its own identity.
    assert result.media_record.media_id
    # Код восстановления в записи не хранится: он выводится из секрета
    # администратора в момент выдачи. Проверяется, что вывести его можно, --
    # это и есть «у носителя есть коды».
    from portable_os_admin import crypto

    assert crypto.derive_recovery_code(
        session.container.admin_secret,
        result.recovery_record.media_id,
        result.recovery_record.slot,
    )


def test_service_volumes_are_mounted_before_the_system_is_copied(
    session, kiosk_config, big_device, tmp_path
):
    """Иначе пустая файловая система накрывает собой то, что должна была принять.

    Дважды пройденные грабли. Сначала так потерялся /boot: ядро и записи
    загрузчика легли на корневой раздел, а сверху встал пустой /boot.
    Потом — /var/log: каталоги, которые пакеты создают при установке
    (auditd, sssd, samba), остались на корне, а том журналов пришёл пустым.
    auditd без своего каталога не стартует, и записи аудита, которым некуда
    деться, ядро печатает на консоль.

    Проверяется порядок команд: монтирование обоих томов должно стоять до
    копирования, а не после.
    """
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    result = OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"user-passphrase", dry_run=True, confirm_device="/dev/sdb"
    )
    commands = [" ".join(r.argv) for r in result.plan.results]

    # Корень собирается overlay поверх образа, и служебные разделы
    # подключаются к уже собранному корню -- но обязательно до того, как в
    # него что-нибудь записано. Первая такая запись -- настройка носителя.
    built = next(i for i, c in enumerate(commands) if c.startswith("mount -t overlay"))
    written = next(i for i, c in enumerate(commands) if "chroot" in c or "initramfs" in c)
    for mountpoint in ("/boot", "/var/log"):
        mounts = [
            i for i, c in enumerate(commands)
            if c.startswith("mount ") and c.rstrip().endswith(mountpoint)
        ]
        assert mounts, f"{mountpoint} не подключается вовсе"
        assert built < min(mounts) < written, (
            f"{mountpoint} подключается не между сборкой корня и записью в него"
        )


def test_two_deployments_get_different_identities(session, kiosk_config, big_device, tmp_path):
    """Same distribution, two media -- and nothing shared between them."""
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real archive")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    distribution.write_metadata(str(directory), metadata)

    installer = OSInstaller(session)
    first = installer.deploy_distribution(
        big_device, str(directory), b"passphrase-one", dry_run=True, confirm_device="/dev/sdb"
    )
    second = installer.deploy_distribution(
        big_device, str(directory), b"passphrase-two", dry_run=True, confirm_device="/dev/sdb"
    )

    from portable_os_admin import crypto

    assert first.media_record.media_id != second.media_record.media_id
    # Коды разные, хотя секрет администратора один: media_id входит в вывод,
    # поэтому носители из одного дистрибутива не делят ни одного кода.
    secret = session.container.admin_secret
    assert crypto.derive_recovery_code(
        secret, first.recovery_record.media_id, first.recovery_record.slot
    ) != crypto.derive_recovery_code(
        secret, second.recovery_record.media_id, second.recovery_record.slot
    )
    assert first.media_record.service_key_b64 != second.media_record.service_key_b64


# ----------------------------------------------------------------------
# RED OS backend, first executed 25.08.2026
# ----------------------------------------------------------------------
def test_dnf_is_not_given_two_flags_it_refuses_together():
    """Regression: the RED OS backend died on its very first real command.

        argument --repo/--repoid: not allowed with argument --disablerepo

    `--repo` already enables what it names and disables the rest, so the pair
    was redundant as well as invalid. It appeared in two places -- the
    bootstrap and the arguments used for every package install afterwards --
    and neither had ever been run before, which is exactly why a backend
    "written to the family's conventions" is not the same as a working one.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    config = InstallConfig.create_default("stick", "redos")

    bootstrap = backend.bootstrap_command("/mnt/root", config, "file:///mnt/image", "8.0")
    assert "--repo=poa-image" in bootstrap
    assert not any(a.startswith("--disablerepo") for a in bootstrap)

    backend.bind_install_source("file:///mnt/image", "8.0")
    install = backend.package_install_command("/mnt/root", ["mate-calc"])
    assert "--repo=poa-image" in install
    assert not any(a.startswith("--disablerepo") for a in install)


def test_redos_does_not_ask_dnf_to_resolve_a_group():
    """Regression: resolving `@core` segfaults dnf on the build host.

    Reproduced precisely before changing anything: a single package from the
    same repository installs fine, `repoquery` works, and removing
    `--forcearch` changes nothing -- only the group does it, and dnf dies
    right after reading the metadata. Nothing in this project can fix a crash
    inside dnf's group handling, so the group is not used: the base system is
    named package by package and dependencies pull in the rest.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    config = InstallConfig.create_default("stick", "redos")
    argv = backend.bootstrap_command("/mnt/root", config, "file:///mnt/image", "8.0")

    assert not any(a.startswith("@") for a in argv), "группа comps снова передаётся dnf"
    # The leaves that pull a working base system in behind them.
    for package in ("basesystem", "systemd", "bash", "coreutils", "redos-release"):
        assert package in argv


def test_redos_installs_every_package_for_the_target_architecture():
    """Regression: --forcearch was on the bootstrap and nowhere else.

    dnf runs on the host and installs into a root of another architecture, so
    without it every package after the base system is refused with "does not
    have a compatible architecture" -- which the first RED OS build hit
    immediately after successfully unpacking a base system made of those very
    packages.
    """
    from portable_os_admin.install.backends import get_backend

    backend = get_backend("redos")
    config = InstallConfig.create_default("stick", "redos")
    config.arch = "x86_64"

    bootstrap = backend.bootstrap_command("/mnt/root", config, "file:///mnt/image", "8.0")
    assert "--forcearch" in bootstrap

    backend.bind_install_source("file:///mnt/image", "8.0", (), "x86_64")
    install = backend.package_install_command("/mnt/root", ["kernel"])
    assert "--forcearch" in install
    assert "x86_64" in install

    # A native build passes no --forcearch at all, rather than one that says
    # "the architecture you already are".
    backend.bind_install_source("file:///mnt/image", "8.0", (), "")
    native = backend.package_install_command("/mnt/root", ["kernel"])
    assert "--forcearch" not in native


def test_boot_partition_is_mounted_before_the_system_is_copied(session, kiosk_config, big_device, tmp_path):
    """Иначе ядро ложится на корневой раздел, а сверху монтируется пустой /boot.

    Найдено живым прогоном: дистрибутив копировался целиком, и только потом
    подключались служебные разделы. Всё, что дистрибутив держит в /boot —
    ядро, initramfs, записи загрузчика — оказывалось спрятано под пустым
    разделом. По содержимому корня это не видно: файлы на месте, просто не на
    том разделе, а носитель не загружается.

    Проверяется способ «распакованным деревом»: он остаётся на случай, когда
    администратор выключил `disk.system_image`. Порядок при системе из
    образа -- в тесте про служебные тома: там корень собирается overlay, и
    разделы подключаются к уже собранному.
    """
    kiosk_config.disk.system_image = False
    directory = tmp_path / "dist"
    directory.mkdir()
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
    commands = [" ".join(r.argv) for r in result.plan.results]

    boot_mount = next(i for i, c in enumerate(commands) if "mount" in c and c.endswith("/boot"))
    copy = next(i for i, c in enumerate(commands) if c.startswith("cp -a"))
    assert boot_mount < copy, "/boot подключается после копирования — ядро окажется спрятано"

    # А раздел EFI — наоборот, после: на FAT нет владельцев, и `cp -a` на нём
    # спотыкается о невозможность их сохранить.
    esp_mount = next(i for i, c in enumerate(commands) if "mount" in c and c.endswith("/boot/efi"))
    assert esp_mount > copy, "ESP подключён до копирования — cp -a на FAT откажет"


def test_a_distribution_without_a_bootloader_is_refused_before_the_medium_is_touched(
    session, big_device, tmp_path
):
    """Час работы не должен пропадать на последнем шаге.

    Ровно это и случилось: дистрибутив был собран версией программы, которая
    вычищала boot/efi/EFI, развёртывание отработало почти целиком и умерло на
    установке загрузчика — когда носитель уже стёрт. Для RHEL-семейства такой
    дистрибутив бесполезен: собрать загрузчик на месте нечем.
    """
    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    ConfigManager().validate(config)

    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real image")
    metadata = distribution.DistributionMetadata.for_config(
        config, release="8.0", arch="x86_64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    metadata.has_bootloader = False
    distribution.write_metadata(str(directory), metadata)

    installer = OSInstaller(session)
    with pytest.raises(InstallError) as excinfo:
        installer.deploy_distribution(
            big_device, str(directory), b"passphrase", dry_run=True, confirm_device="/dev/sdb"
        )

    assert "загрузчик" in str(excinfo.value).lower()
    assert installer.medium_modified is False, "носитель тронут, хотя отказ был заранее"


def test_the_progress_counter_matches_the_real_number_of_steps(session, kiosk_config, big_device, tmp_path):
    """Занижённое число упирает полосу в край задолго до конца.

    Человек видит «шаг 14 из 14», а работа продолжается ещё три шага, и
    выглядит это как зависание.
    """
    directory = tmp_path / "dist"
    directory.mkdir()
    archive = directory / distribution.ARCHIVE_NAME
    archive.write_bytes(b"not a real image")
    metadata = distribution.DistributionMetadata.for_config(
        kiosk_config, release="trixie", arch="amd64", rootfs_bytes=1024, emulated=False
    )
    metadata.sha256 = distribution.sha256_of(str(archive))
    metadata.has_bootloader = True
    distribution.write_metadata(str(directory), metadata)

    seen = []
    OSInstaller(session).deploy_distribution(
        big_device, str(directory), b"passphrase", dry_run=True, confirm_device="/dev/sdb",
        on_step=lambda index, total, text: seen.append((index, total)),
    )

    assert seen, "шаги не сообщаются вовсе"
    last_index, total = seen[-1]
    assert last_index <= total, f"шагов больше, чем обещано: {last_index} из {total}"
    # И обещано не сильно больше, чем есть: полоса не должна замирать на 60%.
    assert total - last_index <= 1, f"обещано {total} шагов, выполнено {last_index}"
