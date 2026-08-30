from portable_os_admin.config import DiskLayoutConfig
from portable_os_admin.install.disk import DiskManager, plan_partitions
from portable_os_admin.install.luks import LuksManager


def test_plan_partitions_layout_order_and_sizes():
    layout = DiskLayoutConfig(
        esp_partition_mb=512, boot_partition_mb=1024, log_partition_mb=256, exchange_partition_mb=1024
    )
    plan = plan_partitions("/dev/sdb", layout)
    labels = [p.label for p in plan.partitions]
    assert labels == ["POA_ESP", "POA_BOOT", "POA_SYSTEM", "POA_LOGS", "POA_EXCHANGE"]
    assert plan.partitions[0].start_mib == 1
    assert plan.partitions[0].end_mib == 512
    # /boot is its own unencrypted partition after the ESP -- GRUB cannot read
    # a LUKS2/argon2id volume, so the kernel cannot live inside the container.
    assert plan.partitions[1].start_mib == 512
    assert plan.partitions[1].end_mib == 1536
    assert plan.partitions[2].fs_hint == ""  # the LUKS container itself


def test_partition_path_for_plain_and_numbered_devices():
    layout = DiskLayoutConfig()
    plan_sd = plan_partitions("/dev/sdb", layout)
    assert plan_sd.partition_path(plan_sd.partitions[1]) == "/dev/sdb2"

    plan_nvme = plan_partitions("/dev/nvme0n1", layout)
    assert plan_nvme.partition_path(plan_nvme.partitions[1]) == "/dev/nvme0n1p2"


def test_every_existing_partition_is_erased_before_the_disk_is_repartitioned():
    """Not a check that a particular command list is emitted -- that would only
    restate the implementation. What matters is the property: signatures inside
    each old partition must be destroyed while their offsets are still known,
    i.e. before the partition table is replaced. A medium previously installed
    by this same tool is the likeliest case, and a surviving LUKS or filesystem
    signature makes blkid and the initramfs find two candidates at once.
    """
    plan = plan_partitions("/dev/sdb", DiskLayoutConfig())
    commands = DiskManager().commands_for(plan, existing_partitions=("/dev/sdb1", "/dev/sdb2"))
    flat = [" ".join(c) for c in commands]

    wiped_partitions = [i for i, c in enumerate(commands) if c[0] == "wipefs" and c[-1].startswith("/dev/sdb")
                        and c[-1] != "/dev/sdb"]
    new_table = next(i for i, c in enumerate(commands) if "mklabel" in c)
    assert wiped_partitions, "old partitions are never wiped"
    assert max(wiped_partitions) < new_table

    # the backup GPT lives at the end of the device; clearing only the head
    # leaves it for the kernel to restore the old layout from
    assert any("sgdisk --zap-all" in f for f in flat)


def test_luks_format_dry_run_never_touches_disk_and_never_leaks_passphrase_in_argv():
    lm = LuksManager()
    plan = lm.format("/dev/sdb2", b"top-secret-passphrase", dry_run=True)
    assert all(r.executed is False for r in plan.results)
    argv = plan.results[0].argv
    assert "top-secret-passphrase" not in argv
    assert "--key-file" in argv


def test_luks_add_keyslot_dry_run():
    lm = LuksManager()
    plan = lm.add_keyslot("/dev/sdb2", b"existing-pass", b"new-pass", slot=7, dry_run=True)
    argv = plan.results[0].argv
    assert "existing-pass" not in argv
    assert "new-pass" not in argv
    assert "--key-slot" in argv
    assert "7" in argv


def test_every_keyslot_is_capped_to_a_memory_a_weak_machine_has():
    """Носитель переносной, а стоимость argon2id пишется в заголовок навсегда.

    cryptsetup подбирает её под машину-изготовитель. Носитель, собранный на
    машине с 32 ГБ, требовал бы при разблокировке больше, чем есть на машине,
    куда его принесли. Наблюдалось на живой загрузке: «keyslot operation could
    fail as it requires more than available memory».

    Проверяются оба пути: создание тома и добавление слота. Слот, добавленный
    без ограничения, свёл бы меру на нет — том открывался бы паролем
    пользователя, но не служебным ключом и не кодом восстановления.
    """
    from portable_os_admin.install import luks as luks_module

    formatted = LuksManager().format(
        "/dev/sdb2", b"passphrase", dry_run=True, confirm_device="/dev/sdb2"
    )
    added = LuksManager().add_keyslot(
        "/dev/sdb2", b"passphrase", b"recovery", dry_run=True, confirm_device="/dev/sdb2"
    )

    for plan in (formatted, added):
        argv = plan.results[0].argv
        assert "--pbkdf-memory" in argv, " ".join(argv)
        assert argv[argv.index("--pbkdf-memory") + 1] == str(luks_module.MAX_PBKDF_MEMORY_KIB)

    # 256 МиБ: достаточно для стойкости argon2id и есть на любой машине,
    # на которой имеет смысл запускать этот носитель.
    assert luks_module.MAX_PBKDF_MEMORY_KIB == 262144

    # Та же болезнь, что и с памятью, только незаметнее: число потоков
    # cryptsetup тоже берёт с машины-изготовителя. Четыре дорожки,
    # подобранные на восьмиядерной станции, на двухъядерном терминале
    # считаются вдвое дольше — и это уже разница между «том открылся» и
    # «systemd перестал его ждать».
    for plan in (formatted, added):
        argv = plan.results[0].argv
        assert "--pbkdf-parallel" in argv, " ".join(argv)
        assert argv[argv.index("--pbkdf-parallel") + 1] == str(luks_module.PBKDF_PARALLEL)
    assert luks_module.PBKDF_PARALLEL == 2

    # И третья из тех же граблей -- число проходов. Его калибровка стоила
    # дороже всего: замер 31.08.2026 показал `Time cost: 40` вместо 4, то есть
    # каждая проверка пароля на машине пользователя вдесятеро дороже, чем
    # задумано. Отсюда были и минуты на разблокировку, и «зависшая» на
    # восемнадцать минут регистрация компьютера, и семь минут на двадцать
    # девять слотов при развёртывании.
    for plan in (formatted, added):
        argv = plan.results[0].argv
        assert "--pbkdf-force-iterations" in argv, " ".join(argv)
        assert argv[argv.index("--pbkdf-force-iterations") + 1] == str(
            luks_module.PBKDF_ITERATIONS
        )
    assert luks_module.PBKDF_ITERATIONS == 4


def test_the_medium_is_trimmed_before_partitioning_but_a_refusal_is_not_fatal():
    """Запись в стёртый блок быстрее, а TRIM умеют не все флешки.

    Замер на тестовом носителе: 2,9 МБ/с последовательной записи против
    0,49 МБ/с, которые давал прогон, — и это одна из причин разрыва.
    """
    plan = plan_partitions("/dev/sdb", DiskLayoutConfig())
    commands = [" ".join(argv) for argv in DiskManager().commands_for(plan)]

    discard = [c for c in commands if "blkdiscard" in c]
    assert discard, "носитель не очищается перед разметкой"
    assert "|| true" in discard[0], "отказ TRIM сорвёт установку"
    # До разметки, а не после: смысл в том, чтобы писать в уже стёртые блоки.
    assert commands.index(discard[0]) < min(i for i, c in enumerate(commands) if "mklabel" in c)


def test_encryption_uses_a_sector_size_flash_memory_likes():
    """512-байтные секторы заставляют контроллер переписывать целую страницу.

    Страница флеш-памяти — 16 КБ и больше, так что запись по 512 байт
    превращается в чтение-изменение-запись. 4096 принимают и устройства с
    логическим сектором 512.
    """
    from portable_os_admin.install import luks as luks_module

    plan = LuksManager().format(
        "/dev/sdb2", b"passphrase", dry_run=True, confirm_device="/dev/sdb2"
    )
    argv = plan.results[0].argv

    assert "--sector-size" in argv
    assert argv[argv.index("--sector-size") + 1] == str(luks_module.DEFAULT_SECTOR_SIZE)
    assert luks_module.DEFAULT_SECTOR_SIZE == 4096
