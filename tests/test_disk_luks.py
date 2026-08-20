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


def test_disk_manager_commands_are_well_formed_and_not_executed_by_default():
    layout = DiskLayoutConfig(boot_partition_mb=512, log_partition_mb=256, exchange_partition_mb=1024)
    plan = plan_partitions("/dev/sdb", layout)
    dm = DiskManager()
    commands = dm.commands_for(plan)

    assert commands[0] == ["wipefs", "--all", "/dev/sdb"]
    assert commands[1] == ["parted", "--script", "/dev/sdb", "--", "mklabel", "gpt"]
    assert ["parted", "--script", "/dev/sdb", "set", "1", "esp", "on"] in commands
    # the kernel must be given a chance to notice the new partitions before
    # anything tries to mkfs /dev/sdbN
    assert commands[-2] == ["partprobe", "/dev/sdb"]
    assert commands[-1] == ["udevadm", "settle"]
    assert any("POA_EXCHANGE" in c for c in commands)

    result = dm.apply(plan, dry_run=True)
    assert all(r.executed is False for r in result.results)
    assert len(result.results) == len(commands)


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
