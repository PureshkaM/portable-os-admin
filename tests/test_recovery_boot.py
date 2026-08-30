"""Wiring the recovery module into the boot environment.

Whether it actually greets a user after three wrong passwords can only be
established by booting a medium; that belongs to a run on real hardware. What
these check is everything that would silently produce a medium with no
recovery at all: the attempt limit reaching crypttab, the files being written
before the initramfs that has to contain them, and the hooks saying what they
must say.
"""
import subprocess

import pytest

from portable_os_admin.config import InstallConfig
from portable_os_admin.container import ContainerManager
from portable_os_admin.session import AdminSession
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.backends.base import RECOVERY_DIR
from portable_os_admin.install.disk import build_media_layout, plan_partitions


@pytest.fixture
def session(tmp_path):
    container = ContainerManager().create(tmp_path / "admin.container", "CorrectHorse123")
    return AdminSession(container)


def _layout(config):
    layout = build_media_layout(plan_partitions("/dev/sdb", config.disk), "poa-abc", "/mnt/poa-abc")
    layout.luks_uuid = "11111111-1111-1111-1111-111111111111"
    layout.logs_luks_uuid = "22222222-2222-2222-2222-222222222222"
    layout.exchange_luks_uuid = "33333333-3333-3333-3333-333333333333"
    return layout


def test_the_configured_attempt_limit_reaches_the_medium():
    """failed_attempts_limit was edited in the interface and validated, and
    then went nowhere -- the same way the kdf field once did. It is what makes
    recovery reachable at all: cryptroot asks this many times and then gives
    up, which is when the recovery hook takes over."""
    config = InstallConfig.create_default("stick1", "debian")
    config.luks.failed_attempts_limit = 4
    crypttab = get_backend("debian").config_files(config, _layout(config))["etc/crypttab"]

    root_line = next(line for line in crypttab.splitlines() if line.startswith("poa-abc\t"))
    assert "tries=4" in root_line


def test_a_nonsense_attempt_limit_does_not_produce_an_unbootable_medium():
    """tries=0 means "ask forever" to cryptsetup, which would lock the user out
    of recovery permanently. The interface validates the range, but the medium
    must not depend on the interface having done so."""
    config = InstallConfig.create_default("stick1", "debian")
    config.luks.failed_attempts_limit = 0
    crypttab = get_backend("debian").config_files(config, _layout(config))["etc/crypttab"]
    assert "tries=0" not in crypttab


def test_debian_hook_copies_the_flow_into_the_initramfs():
    config = InstallConfig.create_default("stick1", "debian")
    files = get_backend("debian").recovery_boot_files(_layout(config), config)

    hook, mode = files["etc/initramfs-tools/hooks/poa-recovery"]
    assert mode == 0o755
    # openssl is the only thing a boot environment lacks for scrypt
    assert "openssl" in hook
    assert RECOVERY_DIR in hook

    script, mode = files["etc/initramfs-tools/scripts/local-top/poa-recovery"]
    assert mode == 0o755
    # ordered after cryptroot, or it would run before the passphrase was asked
    assert 'PREREQ="cryptroot"' in script
    # and it must stand aside when unlocking worked
    assert "/dev/mapper/poa-abc" in script


def test_redos_module_is_actually_enabled_not_merely_present():
    """dracut ignores a module that is not listed; leaving that line out is the
    usual way this kind of integration silently does nothing."""
    config = InstallConfig.create_default("stick1", "redos")
    files = get_backend("redos").recovery_boot_files(_layout(config), config)

    assert "usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh" in files
    conf, _ = files["etc/dracut.conf.d/91-poa-recovery.conf"]
    assert "add_dracutmodules" in conf and "poa-recovery" in conf

    setup, _ = files["usr/lib/dracut/modules.d/91poa-recovery/module-setup.sh"]
    assert "inst_hook pre-mount" in setup
    assert "openssl" in setup


def test_recovery_files_are_written_before_the_initramfs_is_built(session, make_config):
    """The hooks are what copy the flow into the initramfs, so writing them
    afterwards leaves a medium that boots with no recovery at all."""
    from portable_os_admin.config import ConfigManager
    from portable_os_admin.install import BlockDevice, OSInstaller

    config = make_config("stick1", "debian")
    ConfigManager().validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer", removable=True,
        mountpoints=("/media/user/flash",),
    )
    result = OSInstaller(session).run(device, config, b"user-passphrase-123", dry_run=True)
    flat = [" ".join(entry.argv) for entry in result.plan.results]

    hook_idx = next(i for i, line in enumerate(flat) if "hooks/poa-recovery" in line)
    env_idx = next(i for i, line in enumerate(flat) if line.endswith(f"{RECOVERY_DIR}/env"))
    initramfs_idx = next(i for i, line in enumerate(flat) if "update-initramfs" in line)

    assert hook_idx < initramfs_idx, "hook written after the initramfs was built"
    assert env_idx < initramfs_idx, "recovery settings written after the initramfs was built"


def test_the_env_names_the_slot_the_code_was_actually_issued_from(make_config):
    """Номер recovery-слота в env -- тот же, из которого выдан код.

    Раньше здесь стояла константа `RECOVERY_KEYSLOT = 7`, оставшаяся от
    раскладки до привязки к машинам. При двух разрешённых машинах коды
    занимают слоты 3..31, и первый выданный лежит в третьем, а в env уезжала
    семёрка: модуль восстановления проверял бы код против чужого слота и его
    же уничтожал. Восстановление не сработало бы ни разу, причём молча.

    Найдено осмотром носителя 28.08.2026 -- в env стояло 7 при коде из слота 3.
    """
    from portable_os_admin.install import machine_binding
    from portable_os_admin.install.installer import recovery_env

    config = make_config("stick1", "redos")
    for slots in (0, 2, 5):
        config.machine_binding.slots = slots
        expected = machine_binding.recovery_keyslots(config)[0]
        env = recovery_env(config, "abcd1234", "11111111-2222-3333-4444-555555555555")
        assert f"POA_RECOVERY_SLOT={expected}\n" in env
        # Слот пользователя -- нулевой, его модуль заменяет новым паролем.
        assert "POA_USER_SLOT=0\n" in env


def test_the_recovery_script_is_valid_shell():
    """It runs in a boot environment with no way to report a syntax error."""
    import importlib.resources

    path = str(importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh"))
    proc = subprocess.run(["sh", "-n", path], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_the_recovery_script_no_longer_needs_a_binary_that_was_never_written():
    """poa-recovery-kdf is why this feature stayed deferred. openssl computes
    the same scrypt value -- checked against crypto.derive_recovery_passphrase
    byte for byte -- and exists on both families."""
    import importlib.resources

    text = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh").read_text(
        encoding="utf-8"
    )
    # it may still be named in the comment that explains the history; what
    # matters is that nothing calls it
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "poa-recovery-kdf" not in code
    assert "openssl kdf" in code


def test_the_recovery_script_uses_no_tool_the_initramfs_lacks():
    """`dirname` в initramfs нет, а модуль звал его на четвёртой строке.

    При `set -eu` это роняло его немедленно: путь наружу при забытом пароле не
    работал ни разу за всё время существования модуля, и заметить это можно
    было только на настоящей загрузке. Найдено прогоном 28.08.2026 по строке
    «recovery_module.sh: line 24: dirname: command not found» на экране.

    dracut кладёт в образ только то, что перечислено в inst_multiple, поэтому
    каждая внешняя команда здесь -- обязательство. Дешевле обойтись
    подстановкой оболочки.
    """
    import importlib.resources

    text = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh").read_text(
        encoding="utf-8"
    )
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for tool in ("dirname", "basename", "realpath"):
        assert tool not in code, f"{tool} нет в initramfs -- модуль упадёт при set -eu"


def test_the_user_keyslot_is_only_destroyed_after_the_code_is_accepted():
    """An earlier version killed it on entry, so anyone who merely mistyped
    their password lost it without recovering anything."""
    import importlib.resources

    text = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh").read_text(
        encoding="utf-8"
    )
    accepted = text.index('[ -n "$accepted" ]')
    kill_user = text.index('luksKillSlot "$device" "$POA_USER_SLOT"')
    assert accepted < kill_user


def test_the_recovery_slot_is_removed_with_the_new_password():
    """cryptsetup refuses to destroy a keyslot using the very key that slot
    holds -- "Key is not available with this passphrase". Running the flow
    against a real LUKS2 container is how that was found, and using the new
    user password instead is the fix."""
    import importlib.resources

    text = importlib.resources.files("portable_os_admin.recovery").joinpath("recovery_module.sh").read_text(
        encoding="utf-8"
    )
    # Слот здесь -- переменная, а не константа из env: коды расходуются по
    # одному, и модуль берёт первый ещё живой, иначе второе восстановление
    # подряд било бы в уже уничтоженный слот.
    tail = text[text.index('luksKillSlot "$device" "$recovery_slot"'):]
    assert '--key-file "$NEW_KEY"' in tail.split("||")[0]


def _module_text() -> str:
    import importlib.resources

    return importlib.resources.files("portable_os_admin.recovery").joinpath(
        "recovery_module.sh"
    ).read_text(encoding="utf-8")


def test_recovery_talks_through_the_shared_screen_not_the_console():
    """Экран в этот момент занят plymouth, и консоли человек не видит.

    Модуль печатал запрос через `cat` и читал ответ через `read`: на прогоне
    29.08.2026 пользователь получал чёрный экран и не узнавал ни строки,
    которую надо продиктовать администратору, ни того, что у него вообще
    спрашивают. Весь диалог идёт через общий слой -- тот же, которым
    пользуются разблокировка и регистрация машины.
    """
    text = _module_text()
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )

    # Запрос, ввод кода и ввод пароля -- через общий слой.
    assert "poa_say " in body
    assert "poa_ask " in body
    assert "poa_ask_secret " in body
    # И ничего из этого не печатается мимо него.
    assert "cat <<EOF" not in body
    assert "read -r recovery_code" not in body
    assert "read -r new_password" not in body
    # Причина отказа тоже должна быть видна, а не уходить в stderr.
    assert 'echo "Ошибка восстановления' not in body

    # Код запроса -- в приглашении вопроса, а не отдельным сообщением.
    # plymouth показывает сообщения в одном месте, и каждое новое перекрывает
    # предыдущее: на прогоне 29.08.2026 до человека дошло только последнее, а
    # сам код, который надо продиктовать, он так и не увидел. Звонить есть
    # зачем, а сказать нечего -- восстановление становится бесполезным.
    prompt = next(line for line in body.splitlines() if "recovery_code=$(poa_ask" in line)
    assert "${request}" in prompt


def test_the_medium_decides_itself_what_went_wrong():
    """Носитель не спрашивает у человека, что случилось, а смотрит в заголовок.

    Регистрационный слот жив -- фраза открыла бы том на любой машине, и раз не
    открыла, забыт пароль. Слот уничтожен -- значит машины исчерпаны, и верная
    фраза здесь бессильна по устройству механизма. Спрашивать «что у вас?»
    значило бы требовать от пользователя понимания keyslot'ов LUKS.
    """
    text = _module_text()

    assert 'if slot_taken "$POA_USER_SLOT"; then' in text
    assert "kind=password" in text
    assert "kind=machines" in text


def test_the_reset_forgets_every_machine_and_registers_none():
    """Сброс возвращает носитель в исходное состояние, и только.

    Текущая машина не записывается, хотя человек звонил администратору именно
    ради неё: регистрация спрашивает разрешение, а вопрос, заданный посреди
    восстановления, получит «да» не глядя. После перезагрузки он ответит на
    него штатно, увидев счётчик занятых слотов.
    """
    text = _module_text()
    reset = text[text.index('if [ "$kind" = machines ]; then'):]

    # Перебираются все машинные слоты, а не один.
    assert '$POA_MACHINE_FIRST' in reset and '$POA_MACHINE_LAST' in reset
    assert 'luksKillSlot "$device" "$slot"' in reset
    # Регистрация здесь не вызывается ни в каком виде.
    assert "luksAddKey" not in reset.split("while :;")[0]
    assert "reboot" in text, "после сброса нужна перезагрузка"


def test_the_request_string_is_taken_from_a_table_not_computed():
    """Кодирование запроса живёт в одном месте -- в recovery/request.py.

    Вторая реализация base32 и sha256 в shell разошлась бы с первой при первой
    же правке, причём молча: пользователь диктовал бы строку, которую
    администратор не может разобрать, и выяснилось бы это на живой загрузке у
    человека, оставшегося без системы.
    """
    text = _module_text()

    assert '"$HERE/requests"' in text
    # Ничего похожего на собственное вычисление контрольной суммы запроса.
    assert "sha256sum | cut -c1-8" not in text


def test_the_theme_gets_the_images_the_password_field_is_drawn_from(session, make_config):
    """Плагин two-step рисует поле ввода из готовых картинок.

    Их у темы не было вовсе -- и plymouth, которому нечем показать запрос,
    молча уходил в текстовый режим: моноширинный шрифт, серый фон, кириллица
    блоками. По экрану это выглядело как «графика не включилась», хотя тема
    лежала на месте и была выбрана. Найдено 29.08.2026 сравнением со штатной
    темой РЕД ОС, где рядом с `.plymouth` лежат entry, bullet, box и lock.
    """
    from portable_os_admin.config import ConfigManager
    from portable_os_admin.install import BlockDevice, OSInstaller

    config = make_config("stick1", "debian")
    ConfigManager().validate(config)
    device = BlockDevice(
        name="sdb", path="/dev/sdb", size_bytes=32_000_000_000, model="Cruzer",
        removable=True, mountpoints=(),
    )
    result = OSInstaller(session).run(device, config, b"user-passphrase-123", dry_run=True)
    flat = [" ".join(entry.argv) for entry in result.plan.results]

    copy = next((c for c in flat if "entry.png" in c), None)
    assert copy, "элементы поля ввода не кладутся в тему"
    assert "bullet.png" in copy
    # Ищется первая тема, где они есть, а не жёсткое имя: у семейств темы
    # называются по-разному, и пропавшая картинка стоила бы немого экрана.
    assert "themes" in copy


def test_the_env_carries_the_slot_ranges_the_reset_needs():
    from portable_os_admin.install import machine_binding
    from portable_os_admin.install.installer import recovery_env

    config = InstallConfig.create_default("stick1", "redos")
    config.machine_binding.slots = 2
    env = recovery_env(config, "abcd1234", "11111111-2222-3333-4444-555555555555")

    machines = machine_binding.machine_keyslots(config)
    recovery = machine_binding.recovery_keyslots(config)
    assert f"POA_MACHINE_FIRST={machines[0]}" in env
    assert f"POA_MACHINE_LAST={machines[-1]}" in env
    assert f"POA_RECOVERY_FIRST={recovery[0]}" in env
    assert f"POA_RECOVERY_LAST={recovery[-1]}" in env


def test_the_hook_still_copies_the_flow_if_openssl_is_missing():
    """The copies are what put recovery into the image at all. An early exit
    when openssl cannot be copied would produce a medium with no recovery and
    no complaint -- the worst of both."""
    config = InstallConfig.create_default("stick1", "debian")
    hook, _ = get_backend("debian").recovery_boot_files(_layout(config), config)[
        "etc/initramfs-tools/hooks/poa-recovery"
    ]
    assert "|| exit 0" not in hook
    copy_line = next(i for i, line in enumerate(hook.splitlines()) if "copy_exec" in line)
    cp_line = next(i for i, line in enumerate(hook.splitlines()) if line.startswith("cp -a"))
    assert copy_line < cp_line
