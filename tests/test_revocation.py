"""Удалённый отзыв: что должно быть в скрипте, который носитель исполняет сам.

Проверяется не «функция вызвалась», а свойства получившегося скрипта: он
уходит на носитель и работает там без нас, до входа пользователя, и цена
ошибки в нём -- уничтоженный по недоразумению носитель либо, наоборот,
отозванный носитель, который продолжает работать.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from portable_os_admin.config.schema import InstallConfig
from portable_os_admin.config.validator import BuiltinConfigValidator
from portable_os_admin.install import revocation


def _config(url: str = "https://revoke.example.org", enabled: bool = True) -> InstallConfig:
    config = InstallConfig.create_default("stick", "debian")
    config.revocation.enabled = enabled
    config.revocation.url = url
    return config


def _script(config: InstallConfig | None = None) -> str:
    config = config if config is not None else _config()
    return revocation.revocation_script(config, "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666")


def test_the_generated_script_is_valid_shell():
    """`sh -n`, а не чтение глазами: скрипт исполняется на носителе, где
    синтаксическая ошибка выглядит как «проверка отзыва молча не работает»."""
    checked = subprocess.run(["sh", "-n"], input=_script(), text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr


def test_the_request_carries_a_fresh_number_and_the_answer_is_checked_against_it():
    """Защита от повтора старого ответа -- то, что оставалось открытым в
    дизайне. Носитель шлёт случайное число и требует его же в подписанном
    ответе: вчерашнее «всё в порядке» сегодня не подходит."""
    script = _script()

    assert "/dev/urandom" in script, "случайное число запроса не берётся ниоткуда"
    assert '-H "X-POA-Nonce: $NONCE"' in script, "случайное число не уходит в запрос"
    # И ответ обязан назвать и носитель, и это самое число.
    assert '"poa-state $MEDIA_ID $NONCE "*' in script


def test_an_answer_without_a_valid_signature_changes_nothing():
    """Подпись проверяется до всего остального, и без неё ответ не значит
    ничего: иначе отозвать носитель мог бы всякий, кто подменил ответ по
    дороге, а уничтожение необратимо."""
    script = _script()

    verify_at = script.index("openssl pkeyutl -verify")
    erase_at = script.index("cryptsetup luksErase")
    assert verify_at < erase_at, "заголовок уничтожается раньше проверки подписи"
    assert "ПОДПИСЬ НЕ СХОДИТСЯ" in script


def test_only_the_revoked_state_destroys_anything():
    script = _script()
    assert 'if [ "$STATE" != "revoked" ]' in script, "уничтожение не привязано к состоянию"
    # Сервер промолчал -- носитель не уничтожается, а пользователь узнаёт, что
    # связи нет. Каждый такой исход уходит через no_answer, который выходит.
    assert "сети нет, сервер не спрошен" in script
    assert "сервер недоступен" in script
    no_answer = script[script.index("no_answer() {"):]
    assert "exit 0" in no_answer[: no_answer.index("}")]


def test_the_medium_says_what_happened_before_it_powers_off():
    """Человек должен понять, почему машина выключилась, а не решить, что она
    сломалась."""
    script = _script()
    assert "Носитель отозван администратором" in script
    assert "poweroff -f" in script


def test_identifier_and_address_are_written_into_the_script():
    """То, что никогда не попадает в командную строку, нельзя закавычить
    неправильно -- тем же приёмом собран скрипт режима киоска."""
    script = _script()
    assert "MEDIA_ID=a1b2c3d4" in script
    assert "URL=https://revoke.example.org" in script
    assert "LUKS_UUID=0e3f1a90-1111-2222-3333-444455556666" in script


def test_nothing_is_written_when_the_feature_is_off():
    off = _config(enabled=False)
    assert revocation.config_files(off, "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666") == {}

    # И при включённом отзыве, но неизвестном носителе: общий на всех скрипт
    # спрашивал бы ресурс об одном и том же идентификаторе со всех флешек.
    assert revocation.config_files(_config(), "", "") == {}


def test_files_land_where_the_service_can_find_them():
    config = _config()
    files = revocation.config_files(config, "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666")
    expected = {
        revocation.SCRIPT_PATH, revocation.SERVICE_PATH,
        revocation.TIMER_PATH, revocation.DISPATCHER_PATH,
    }
    assert expected <= set(files)
    assert (revocation.GATE_PATH in files) == revocation.gate_enabled(config)
    assert files[revocation.SCRIPT_PATH][1] == 0o755, "скрипт должен быть исполняемым"
    assert files[revocation.DISPATCHER_PATH][1] == 0o755, "диспетчер сети не исполнит файл без права"
    assert f"/{revocation.SCRIPT_PATH}" in files[revocation.SERVICE_PATH][0]


def test_the_validator_refuses_configurations_that_would_install_but_not_work():
    manager = BuiltinConfigValidator()

    no_url = _config(url="")
    assert not manager.validate(no_url).ok

    plain_http = _config(url="http://revoke.example.org")
    result = manager.validate(plain_http)
    assert not result.ok
    assert any("https" in error for error in result.errors)

    zero_timeout = _config()
    zero_timeout.revocation.timeout_seconds = 0
    assert not manager.validate(zero_timeout).ok

    assert manager.validate(_config()).ok


def test_both_families_carry_what_the_check_needs():
    """Проверка отзыва ходит curl и проверяет подпись openssl.

    Ни того, ни другого в минимальной системе нет по умолчанию, а отсутствие
    вылезает худшим образом: носитель выглядит исправным, проверка молча не
    срабатывает ни разу, и заметить это можно только в журнале носителя.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend

    for family in ("debian", "redos"):
        config = InstallConfig.create_baseline("stick", family)
        packages = get_backend(family).boot_packages(config)
        for tool in ("curl", "openssl"):
            assert tool in packages, f"{family}: в наборе нет {tool}"

    # notify-send: у семейств он в пакетах с разными именами -- сверено по
    # образам 15.09.2026 (libnotify-bin_0.8.6 на DVD Debian, libnotify-0.8.3 у
    # РЕД ОС, где программа лежит в самой библиотеке).
    assert "libnotify-bin" in get_backend("debian").boot_packages(
        InstallConfig.create_baseline("stick", "debian")
    )
    assert "libnotify" in get_backend("redos").boot_packages(
        InstallConfig.create_baseline("stick", "redos")
    )

    # Корневые сертификаты -- явно у обоих. У Debian libcurl4t64 их лишь
    # рекомендует, и носитель уехал без них: curl (77), сервер «недоступен»
    # всегда, шлюз сеанса не открывается (живая загрузка 15.09.2026).
    for family in ("debian", "redos"):
        packages = get_backend(family).boot_packages(InstallConfig.create_baseline("stick", family))
        assert "ca-certificates" in packages, f"{family}: в наборе нет ca-certificates"


def test_the_check_names_the_tool_it_lacks():
    """Скрипт не должен молча уходить при нехватке инструмента."""
    script = _script()

    assert "command -v" in script
    assert "проверка отзыва невозможна" in script


def test_the_service_waits_for_the_volume_its_log_lives_on():
    """Иначе строки ложатся на корень под точкой монтирования.

    `/var/log` на носителе -- отдельный зашифрованный том. Служба, написавшая
    раньше монтирования, пишет фактически в никуда: раздел «Журналы»
    приложения читает том, а не корень. Найдено живой загрузкой 13.09.2026 --
    две строки службы отзыва оказались недоступны администратору.
    """
    assert "RequiresMountsFor=/var/log" in revocation.service_unit()


def test_nothing_enables_the_service_through_chroot_any_more():
    """Включение переехало туда, где пишутся файлы, и это принципиально.

    Сначала оно жило в командах chroot и шло безусловно -- сборка дистрибутива
    падала на «unit poa-revocation.service does not exist». Потом признаком
    стал известный носитель -- и служба перестала включаться на пути
    развёртывания готового дистрибутива, где команды chroot не выполняются
    вовсе: носитель РЕД ОС 14.09.2026 уехал со скриптом, ключом и без
    автозапуска.
    """
    assert not hasattr(revocation, "chroot_commands"), (
        "включение службы вернулось в команды chroot -- оно выполняется не на всех путях"
    )


def test_the_volume_is_found_by_uuid_and_checked_before_it_is_destroyed():
    """Путь раздела с машины администратора на носителе не значит ничего.

    Живая проверка отзыва 14.09.2026: носитель получил «revoked», дошёл до
    уничтожения и записал «Device /dev/loop1p3 does not exist or access
    denied» -- в скрипт был вписан путь, каким раздел назывался на машине,
    где носитель записывали.

    Опаснее другое: на чужой машине путь вроде `/dev/sdb3` существует сплошь и
    рядом и принадлежит постороннему диску, а `luksErase` необратим. Поэтому
    том ищется по UUID заголовка, а перед уничтожением UUID сверяется ещё раз.
    """
    script = _script()

    assert "/dev/disk/by-uuid/$_uuid" in script, "том не ищется по UUID"
    assert "blkid -t UUID=" in script, "нет запасного способа найти том"

    check_at = script.index("cryptsetup luksUUID")
    erase_at = script.index("cryptsetup luksErase")
    assert check_at < erase_at, "UUID сверяется после уничтожения -- бесполезно"
    assert '_found" != "$_uuid' in script, "несовпадение UUID ничего не меняет"

    # Не нашли том или нашли не тот -- честная строка и ни одной попытки
    # стереть что-нибудь наугад.
    assert "не найден" in script
    assert "не тот том" in script and "не трогаю" in script


def _full_script() -> str:
    """Скрипт со всеми частями раскладки -- как его получает настоящий носитель."""
    return revocation.revocation_script(
        _config(),
        "a1b2c3d4",
        "0e3f1a90-1111-2222-3333-444455556666",
        logs_luks_uuid="11111111-aaaa-bbbb-cccc-222222222222",
        exchange_luks_uuid="33333333-dddd-eeee-ffff-444444444444",
        esp_uuid="AAAA-BBBB",
        boot_uuid="55555555-6666-7777-8888-999999999999",
    )


def test_every_volume_of_the_medium_is_destroyed_not_just_the_system_one():
    """Служебные тома -- отдельные контейнеры со своими ключами.

    `luksErase` системного тома их не касается вовсе: отозванный носитель
    оставался бы с живыми томами журналов и обмена, то есть с читаемым
    содержимым. Требование поставлено прямо: после отзыва носитель непригоден
    безвозвратно.
    """
    script = _full_script()

    for uuid_name in ("$LUKS_UUID", "$EXCHANGE_UUID", "$LOGS_UUID"):
        assert f'destroy_luks "{uuid_name}"' in script, f"{uuid_name} не уничтожается"

    # И каждый UUID действительно вписан в текст скрипта, а не потерян по дороге.
    for value in (
        "11111111-aaaa-bbbb-cccc-222222222222",
        "33333333-dddd-eeee-ffff-444444444444",
    ):
        assert value in script


def test_the_log_volume_is_destroyed_after_everything_it_recorded():
    """Иначе журнал уничтожения обрывается на середине уничтожения.

    В том журналов пишется каждый шаг, поэтому он идёт последним из томов, а
    дальше журнал переезжает в память -- прочитать его можно до выключения.
    """
    script = _full_script()

    logs_at = script.index('destroy_luks "$LOGS_UUID"')
    system_at = script.index('destroy_luks "$LUKS_UUID"')
    exchange_at = script.index('destroy_luks "$EXCHANGE_UUID"')
    assert system_at < logs_at and exchange_at < logs_at, "том журналов уничтожен слишком рано"

    move_at = script.index("LOG=/run/poa-revocation.log")
    assert move_at < logs_at, "журнал переезжает в память уже после уничтожения тома"


def test_the_data_is_wiped_and_not_only_the_keys():
    """Против заранее снятой копии заголовка.

    `luksHeaderBackup` доступен всякому, у кого есть root на собственной
    машине. Копия плюс известная пользователю фраза -- это восстановленный
    доступ, и копия лежит вне носителя, отобрать её нельзя. Обесценивает её
    только затирание того, что она открывает.

    Сплошной проход по флешке идёт часами и будет прерван выдёргиванием,
    поэтому проход двойной: сплошь по началу тома и дальше с шагом.
    """
    script = _full_script()

    assert "wipe_data" in script
    assert "count=1024" in script, "начало тома не затирается сплошь"
    assert "seek=\"$_at\"" in script and "_at + 32" in script, "нет прохода с шагом"

    # Затирание -- после уничтожения ключей: выдернутое на этом шаге питание
    # не должно оставлять носитель открываемым.
    keys_at = script.index("cryptsetup luksErase")
    data_at = script.index("wipe_data \"$SYSTEM_DEV\"")
    assert keys_at < data_at, "данные затираются раньше, чем уничтожены ключи"


def test_the_revoked_medium_stops_being_bootable():
    """Носитель, который грузится, выглядит сломавшимся, а не отозванным."""
    script = _full_script()

    assert 'wipe_boot "$BOOT_UUID"' in script
    assert 'wipe_boot "$ESP_UUID"' in script
    assert "AAAA-BBBB" in script and "55555555-6666-7777-8888-999999999999" in script

    # Разметка -- только от проверенного раздела: ошибиться здесь диском
    # страшнее, чем пропустить шаг. Родительский диск вычисляется от системного
    # тома, найденного по UUID.
    parent_at = script.index("lsblk -no pkname")
    assert "$SYSTEM_DEV" in script[parent_at - 200 : parent_at]
    boot_at = script.index('wipe_boot "$BOOT_UUID"')
    table_at = script.index('of="/dev/$PARENT" bs=1M count=4 conv=fsync')
    assert boot_at < table_at


def test_everything_needed_later_is_known_before_the_first_destructive_step():
    """Живая проверка 15.09.2026: затирание данных отрезает системе корень.

    После него lsblk и blockdev уже не запускались («SQUASHFS error: Failed to
    read block»), и GPT осталась целой. Устройство, диск и размеры вычисляются
    до первого luksErase, а dd копируется в память.
    """
    script = _full_script()
    first_erase = script.index('destroy_luks "$LUKS_UUID"')
    for fact in (
        "SYSTEM_DEV=$(find_volume",
        "lsblk -no pkname",
        "DISK_MB=$(( _bytes",
        "SYSTEM_MB=$(( _bytes",
        "echo 1 > /proc/sys/kernel/sysrq",
        "stage_binary /bin/sh sh",
        'stage_binary "$(command -v dd)" dd',
    ):
        assert script.index(fact) < first_erase, f"{fact} делается уже после разрушения"


def _wipe_tail(script: str) -> str:
    start = script.index("<<'POA_WIPE'\n") + len("<<'POA_WIPE'\n")
    return script[start:script.index("POA_WIPE\n", start)]


def test_the_tail_after_the_data_wipe_runs_from_memory():
    """Живая проверка РЕД ОС 15.09.2026: после затирания данных скрипт и dd
    упали с дампом -- bash подгружал код с уже затёртого корня, и носитель так
    и не выключился. Хвост исполняется копиями из tmpfs через загрузчик."""
    script = _full_script()
    table_at = script.index('of="/dev/$PARENT" bs=1M count=4 conv=fsync')
    exec_at = script.index('LC_ALL=C exec "$LOADER" --library-path "$STAGE/lib" "$STAGE/bin/sh"')
    assert table_at < exec_at

    tail = _wipe_tail(script)
    checked = subprocess.run(["sh", "-n"], input=tail, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    # В хвосте -- только встроенные команды и dd через скопированный загрузчик.
    assert '"$loader" --library-path "$lib" "$dd"' in tail
    for outside in ("poweroff", "blockdev", "lsblk", "cryptsetup", "date "):
        assert outside not in tail, f"хвост зовёт {outside.strip()} с диска"
    lines = [line.strip() for line in tail.splitlines()]
    assert "sync" not in lines, "хвост зовёт sync с диска"
    assert "echo o > /proc/sysrq-trigger" in tail


def test_the_staging_ground_is_executable_and_the_handover_is_guarded():
    """Живой прогон Debian 16.09.2026: на `/run` там стоит `noexec`, копия
    загрузчика не запускалась, а `exec`, который не смог запуститься, завершает
    неинтерактивную оболочку. Уничтожение остановилось сразу после затирания
    разметки: ключи томов уничтожены, данные целы, носитель не выключился и
    запасной путь затирания не выполнился вовсе."""
    script = _full_script()

    # Площадка делается исполняемой до того, как в неё копируют.
    mount_at = script.index("mount -t tmpfs -o exec")
    assert mount_at < script.index("stage_binary /bin/sh sh")
    assert "mount -o remount,exec" in script, "нужен запасной путь, если своя tmpfs не встала"

    # Пробный запуск стоит дважды: при подготовке и прямо перед передачей.
    assert script.count("\"$STAGE/bin/sh\" -c 'exit 0'") >= 2
    exec_at = script.index('LC_ALL=C exec "$LOADER"')
    assert script.rindex("-c 'exit 0'", 0, exec_at) < exec_at

    # Запасной путь остаётся достижимым: затирание с диска идёт после блока.
    assert script.index('wipe_data "$SYSTEM_DEV" "$SYSTEM_MB"') > exec_at


def test_destruction_cannot_be_killed_from_outside():
    """Живая проверка РЕД ОС 15.09.2026: через ~15 секунд после начала
    затирания оболочка и dd получали сигнал 6 извне и носитель оставался
    разрушенным наполовину. Уничтожение глушит сигналы и уходит из cgroup
    службы -- и то, и другое до первого разрушительного шага."""
    script = _full_script()
    ignore_at = script.index("trap '' TERM INT HUP QUIT ABRT PIPE")
    cgroup_at = script.index("cgroup.procs")
    first_destructive = script.index('destroy_luks "$LUKS_UUID"')
    assert ignore_at < first_destructive
    assert cgroup_at < first_destructive

    # Хвост ставит игнор заново: exec сбрасывает перехватчики, а игнор dd
    # достаётся по наследству.
    tail = _wipe_tail(script)
    assert "trap '' TERM INT HUP QUIT ABRT PIPE" in tail
    assert tail.index("trap ''") < tail.index("run_dd if=/dev/zero")


def test_a_staged_shell_really_runs_from_its_copies(tmp_path):
    """Не «скопировано», а «исполняется»: копия оболочки запускается загрузчиком
    с одними скопированными библиотеками."""
    if not pathlib.Path("/proc/sys/kernel/random/boot_id").exists():
        pytest.skip("нужен Linux: ld.so и ldd")
    script = _full_script()
    start = script.index("stage_binary() {")
    function = script[start:script.index("\n}\n", start) + 3]
    probe = (
        f'STAGE="{tmp_path}/stage"\n{function}'
        'stage_binary /bin/sh sh && stage_binary "$(command -v dd)" dd || exit 3\n'
        'for _ld in "$STAGE"/lib/ld-linux*.so*; do LOADER=$_ld; break; done\n'
        'LC_ALL=C "$LOADER" --library-path "$STAGE/lib" "$STAGE/bin/sh" -c "echo staged-ok"\n'
        '"$LOADER" --library-path "$STAGE/lib" "$STAGE/bin/dd" if=/dev/zero of=/dev/null bs=1 count=1\n'
    )
    run = subprocess.run(["sh", "-c", probe], text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert "staged-ok" in run.stdout


def test_the_data_wipe_comes_after_every_quick_step():
    """Затирание данных губит работающую систему -- всё быстрое идёт раньше него."""
    script = _full_script()
    data_at = script.index('wipe_data "$SYSTEM_DEV" "$SYSTEM_MB"')
    for step in (
        'destroy_luks "$LUKS_UUID"',
        'destroy_luks "$EXCHANGE_UUID"',
        'wipe_boot "$BOOT_UUID"',
        'wipe_boot "$ESP_UUID"',
        'destroy_luks "$LOGS_UUID"',
        'of="/dev/$PARENT" bs=1M count=4 conv=fsync',
    ):
        assert script.index(step) < data_at, f"{step} стоит после затирания данных"


def test_the_service_has_no_deadline_that_would_stop_destruction_halfway():
    """Пять минут на службу означали бы SIGTERM посреди затирания.

    Часть носителя затёрта, часть цела, и какая именно -- как повезёт. Сетевые
    шаги ограничены своим `--max-time`, поэтому служба без предела не зависает
    на недоступном ресурсе.
    """
    unit = revocation.service_unit()

    assert "TimeoutStartSec=infinity" in unit
    assert "--max-time" in _script()


def test_the_installer_hands_the_script_every_part_of_the_medium():
    """UUID собираются в раскладке -- и должны дойти до скрипта целиком.

    Ровно здесь ломались прошлые правки: путь сборки дистрибутива и путь записи
    носителя опираются на разные признаки, и половина данных терялась по
    дороге.
    """
    from portable_os_admin.config import InstallConfig
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import MediaLayout

    config = InstallConfig.create_baseline("stick", "debian")
    config.revocation.enabled = True
    config.revocation.url = "https://revoke.example.org"
    layout = MediaLayout(
        device="/dev/sdz",
        esp_partition="/dev/sdz1",
        boot_partition="/dev/sdz2",
        system_partition="/dev/sdz3",
        logs_partition="/dev/sdz4",
        images_partition="/dev/sdz5",
        mapper_name="poa-a1b2c3d4",
        mapper_path="/dev/mapper/poa-a1b2c3d4",
        mount_root="/mnt/poa",
        esp_uuid="AAAA-BBBB",
        boot_uuid="55555555-6666-7777-8888-999999999999",
        luks_uuid="0e3f1a90-1111-2222-3333-444455556666",
        logs_luks_uuid="11111111-aaaa-bbbb-cccc-222222222222",
        exchange_luks_uuid="33333333-dddd-eeee-ffff-444444444444",
    )

    files = get_backend("debian").scripted_files(config, layout)
    script = files[revocation.SCRIPT_PATH][0]

    assert "LOGS_UUID=11111111-aaaa-bbbb-cccc-222222222222" in script
    assert "EXCHANGE_UUID=33333333-dddd-eeee-ffff-444444444444" in script
    assert "ESP_UUID=AAAA-BBBB" in script
    assert "BOOT_UUID=55555555-6666-7777-8888-999999999999" in script


def test_a_step_that_fails_is_told_apart_from_a_medium_that_is_gone():
    """Человеку и администратору нужны разные слова для разных исходов."""
    script = _full_script()

    assert "FAILED=1" in script
    assert "носитель уничтожен" in script
    assert "уничтожение прошло не полностью" in script


# ----------------------------------------------------------------------
# Протокол v2: подписанный запрос, опрос, шлюз сеанса
# ----------------------------------------------------------------------
def _kiosk_config():
    config = InstallConfig.create_baseline("stick", "debian")
    config.revocation.enabled = True
    config.revocation.url = "https://revoke.example.org"
    return config


def test_the_request_is_signed_by_the_medium_before_it_leaves():
    """Без подписи ключом носителя сервер отвечает 403 -- и должен.

    Подпись ставится до отправки и накрывает ровно то, что сервер сверит:
    идентификатор, случайное число и сведения о носителе, в этом порядке.
    """
    script = _script()

    sign_at = script.index("openssl pkeyutl -sign")
    send_at = script.index("curl --silent")
    assert sign_at < send_at, "запрос уходит раньше, чем подписан"
    assert "printf 'poa-ask %s %s %s' \"$MEDIA_ID\" \"$NONCE\" \"$INFO\"" in script
    assert f"DEVICE_KEY=/{revocation.DEVICE_KEY_PATH}" in script
    for header in ("X-POA-Media", "X-POA-Nonce", "X-POA-Info", "X-POA-Signature"):
        assert f'-H "{header}: ' in script, f"в запросе нет заголовка {header}"


def test_nothing_about_the_medium_travels_in_the_address():
    """Адрес оседает в журналах посредников, заголовки -- нет."""
    script = _script()
    assert '"$URL/v2/state"' in script
    assert "?media=" not in script and "nonce=" not in script


def test_the_medium_tells_the_server_what_it_is():
    """Учёт парка: версия и отпечаток машины уходят в запросе."""
    script = _script()
    assert 'INFO="version=$VERSION;machine=$MACHINE"' in script
    assert "poa_fingerprint_id()" in script, "отпечаток машины не вычисляется"
    from portable_os_admin import __version__

    assert f"VERSION={__version__}" in script


def test_a_refusal_is_not_mistaken_for_an_answer():
    """403 означает «сервер не признал носитель» -- ни уничтожения, ни авторизации."""
    script = _script()
    refusal_at = script.index("    403)")
    verify_at = script.index("openssl pkeyutl -verify")
    assert refusal_at < verify_at
    assert "носитель не признан" in script


def test_only_a_verified_ok_opens_the_session():
    """Файл авторизации пишется после проверки подписи и числа, и только на «ok»."""
    script = _script()

    verify_at = script.index("openssl pkeyutl -verify")
    nonce_at = script.index('"poa-state $MEDIA_ID $NONCE "*')
    open_at = script.index('mv -f "$AUTHORIZED.tmp" "$AUTHORIZED"')
    assert verify_at < nonce_at < open_at
    assert 'if [ "$STATE" = "ok" ]' in script[nonce_at:open_at]
    assert "/proc/sys/kernel/random/boot_id" in script[nonce_at:open_at]


def test_a_revoked_medium_closes_the_session_before_destroying_itself():
    script = _script()
    closed_at = script.index('rm -f "$AUTHORIZED"')
    erase_at = script.index("cryptsetup luksErase")
    assert closed_at < erase_at
    assert script.index(': > "$STATE_DIR/destroying"') < erase_at


def test_one_pass_at_a_time():
    """Таймер и диспетчер сети могут сработать разом; уничтожение не повторяется."""
    script = _script()
    assert "flock -n 9 || exit 0" in script
    assert '[ -e "$STATE_DIR/destroying" ] && exit 0' in script


def test_the_user_is_told_when_the_server_cannot_be_reached():
    """Решение пользователя 15.09.2026: без связи -- системное уведомление."""
    script = _script()
    assert 'notify_user critical "Нет связи с сервером авторизации"' in script
    assert "runuser -u \"$_user\"" in script
    # Адрес шины -- из окружения графического процесса пользователя, а не
    # путь /run/user/UID/bus: у MATE под SDDM шина живёт в /tmp, и по пути
    # уведомление не доходило (живая загрузка 15.09.2026).
    assert '/proc/$_pid/environ' in script
    assert "s/^DBUS_SESSION_BUS_ADDRESS=//p" in script
    assert "*DISPLAY=*" in script
    assert 'DBUS_SESSION_BUS_ADDRESS="$_addr"' in script


def test_the_server_is_asked_every_five_minutes_and_when_the_network_comes_up():
    timer = revocation.timer_unit()
    assert "OnUnitActiveSec=5min" in timer
    assert "WantedBy=timers.target" in timer

    dispatcher = revocation.dispatcher_script()
    checked = subprocess.run(["sh", "-n"], input=dispatcher, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    assert "up|" in dispatcher and "--no-block" in dispatcher
    assert revocation.SERVICE_NAME in dispatcher


def test_the_service_itself_is_not_started_at_boot_any_more():
    """Запускает её таймер; своя ссылка в multi-user.target дала бы второй проход."""
    assert "[Install]" not in revocation.service_unit()


def test_the_gate_is_valid_shell_and_bound_to_this_boot():
    gate = revocation.gate_script()
    checked = subprocess.run(["sh", "-n"], input=gate, text=True, capture_output=True)
    assert checked.returncode == 0, checked.stderr
    assert f"AUTHORIZED={revocation.AUTHORIZED_FILE}" in gate
    assert "/proc/sys/kernel/random/boot_id" in gate
    assert "notify-send" in gate


def test_the_gate_refuses_without_authorization_and_waits_when_asked(tmp_path):
    """Шлюз исполняется по-настоящему: без файла отказ, с файлом -- запуск."""
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id")
    if not boot_id.exists():
        pytest.skip("нужен Linux: /proc/sys/kernel/random/boot_id")

    authorized = tmp_path / "authorized"
    gate = tmp_path / "gate"
    gate.write_text(
        revocation.gate_script().replace(revocation.AUTHORIZED_FILE, str(authorized))
    )
    gate.chmod(0o755)

    refused = subprocess.run([str(gate), "true"], capture_output=True)
    assert refused.returncode == 1

    authorized.write_text(boot_id.read_text())
    allowed = subprocess.run([str(gate), "sh", "-c", "exit 7"], capture_output=True)
    assert allowed.returncode == 7

    authorized.write_text("another-boot\n")
    assert subprocess.run([str(gate), "true"], capture_output=True).returncode == 1


def test_the_gate_is_written_only_for_a_kiosk():
    kiosk_config = _kiosk_config()
    files = revocation.config_files(kiosk_config, "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666")
    assert revocation.GATE_PATH in files

    console = _kiosk_config()
    console.kiosk.enabled = False
    files = revocation.config_files(console, "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666")
    assert revocation.GATE_PATH not in files


def test_a_device_key_is_an_ed25519_pair_in_the_shape_the_server_expects():
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    private_pem, public_b64 = revocation.new_device_key()
    key = serialization.load_pem_private_key(private_pem.encode("ascii"), password=None)
    assert isinstance(key, ed25519.Ed25519PrivateKey)

    raw = base64.b64decode(public_b64, validate=True)
    assert len(raw) == 32 and len(public_b64) == 44
    signature = key.sign(b"poa-ask a1b2c3d4 00 version=1;machine=00")
    ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(
        signature, b"poa-ask a1b2c3d4 00 version=1;machine=00"
    )

    assert revocation.new_device_key()[1] != public_b64, "ключи носителей совпали"


def test_server_is_recognised_by_the_pinned_tls_key_when_one_is_known():
    """Проверка отзыва узнаёт сервер по отпечатку, как протокол v3, а не по УЦ:
    иначе подменный сертификат любого УЦ проходил бы, а свой УЦ компании --
    нет (найдено живой загрузкой 02.10.2026: «сервер недоступен»)."""
    from portable_os_admin.install.access import files as access_files

    script = _script()
    assert '${PIN:+--insecure --pinnedpubkey "$PIN"}' in script
    assert "/var/lib/poa/tls-pin /etc/poa/tls-pin" in script
    assert access_files.curl_pins(["00" * 32, "ff" * 32]) == (
        "sha256//AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=;"
        "sha256//" + "/" * 42 + "8=")
