"""Э3b/Э19: разметка носителя v3 -- образы слотов, dm-verity, без раздела обмена."""
from __future__ import annotations

import subprocess

import pytest

from portable_os_admin.config.schema import InstallConfig
from portable_os_admin.install import distribution, overlay
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.disk import LAYOUT_VERSION, build_media_layout, plan_partitions


def _layout():
    config = InstallConfig.create_baseline("stick", "debian")
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-x", "/mnt/x")
    layout.boot_uuid, layout.esp_uuid, layout.root_uuid = "b", "e", "r"
    layout.luks_uuid, layout.logs_luks_uuid = "l", "g"
    return config, layout


def test_partitions_are_esp_boot_state_logs_and_images():
    config = InstallConfig.create_baseline("stick", "debian")
    plan = plan_partitions("/dev/sdb", config.disk)
    assert LAYOUT_VERSION == 3
    assert [p.label for p in plan.partitions] == ["POA_ESP", "POA_BOOT", "POA_SYSTEM", "POA_LOGS", "POA_IMAGES"]
    # фиксированные размеры у всего, кроме образов
    assert plan.partitions[-1].end_mib is None
    assert all(isinstance(p.end_mib, int) for p in plan.partitions[:-1])


def test_there_is_no_exchange_volume_any_more():
    config, layout = _layout()
    backend = get_backend("debian")
    crypttab = [line for line in backend.config_files(config, layout)["etc/crypttab"].splitlines()
                if line and not line.startswith("#")]
    assert len(crypttab) == 2 and "exch" not in " ".join(crypttab)
    fstab = backend.config_files(config, layout)["etc/fstab"]
    row = next(line for line in fstab.splitlines() if "/mnt/exchange" in line)
    fields = row.split("\t")
    assert fields[0] == fields[1] == "/mnt/exchange" and fields[2] == "none"
    assert {"bind", "ro", "noexec", "nosuid", "nodev"} <= set(fields[3].split(","))


@pytest.mark.parametrize("selinux,mount_container", [(False, False), (True, True)])
def test_the_boot_script_mounts_the_slot_image_under_verity(selinux, mount_container):
    script = overlay.boot_script("${POA_MAPPER}", mount_container=mount_container, selinux=selinux)
    assert "veritysetup open" in script and "poa.roothash" in script and "poa.slot" in script
    assert "/dev/mapper/poa-root" in script
    assert "$images/slot-$slot/rootfs.verity" in script
    # без хеша -- не собирать корень вовсе
    assert 'fail "в командной строке ядра нет корневого хеша образа (poa.roothash)"' in script
    # раздел образов переезжает в новый корень
    assert f'"$__ROOTMNT__{overlay.IMAGES_MOUNT}"' in script or f'"${{NEWROOT}}{overlay.IMAGES_MOUNT}"' in script \
        or overlay.IMAGES_MOUNT in script
    checked = subprocess.run(["sh", "-n"], input=script.replace("__ROOTMNT__", "rootmnt")
                             .replace("__LABEL__", "").replace("__BINDS__", ""),
                             text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr


def test_the_cmdline_parsing_in_the_boot_script_works(tmp_path):
    """Разбор poa.slot/poa.roothash -- строками из сгенерированного скрипта.

    Живая загрузка 02.10.2026: в исходнике на Python в `sed` стояло `\\1` с
    одной обратной чертой, и строка превратилась в символ с кодом 1. Проверки
    «не пусто» проходили, путь выходил `slot-<\\x01>`. Прежний тест проверял
    свой собственный sed -- и был зелёным."""
    cmdline = tmp_path / "cmdline"
    cmdline.write_text("BOOT_IMAGE=/slot-a/vmlinuz ro poa.slot=b poa.roothash=" + "ab" * 32 + " quiet\n")
    script = overlay.boot_script("${POA_MAPPER}")
    lines = [line.replace("/proc/cmdline", str(cmdline)) for line in script.splitlines()
             if line.startswith(("slot=", "roothash="))]
    assert len(lines) == 2
    out = subprocess.run(["sh", "-c", "\n".join(lines) + '\nprintf "%s|%s" "$slot" "$roothash"'],
                         text=True, capture_output=True).stdout
    assert out == "b|" + "ab" * 32

def test_grub_boots_the_slot_kernel_with_its_root_hash():
    config, layout = _layout()
    layout.slot, layout.root_hash = "a", "cd" * 32
    script = get_backend("debian").grub_config_script(layout, config, "/boot/grub/grub.cfg")
    assert "ls -1 /boot/slot-a/vmlinuz-*" in script
    assert f"poa.slot=a poa.roothash={'cd' * 32}" in script


def test_initramfs_carries_veritysetup_and_dm_verity():
    config, layout = _layout()
    debian = get_backend("debian").overlay_boot_files(layout)
    hook = next(text for path, (text, _m) in debian.items() if "hooks/" in path)
    assert "manual_add_modules dm-verity" in hook and "veritysetup" in hook
    redos = get_backend("redos").overlay_boot_files(layout)
    setup = next(text for path, (text, _m) in redos.items() if path.endswith("module-setup.sh"))
    assert "dm-verity" in setup and "veritysetup" in setup


def test_root_hash_is_read_from_veritysetup_output():
    out = "VERITY header information for x\nUUID: 1\nHash type: 1\nRoot hash:      " + "0f" * 32 + "\n"
    assert distribution.parse_root_hash(out) == "0f" * 32
    with pytest.raises(ValueError):
        distribution.parse_root_hash("Root hash: nothex\n")
