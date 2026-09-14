"""Удалённый отзыв: что должно быть в скрипте, который носитель исполняет сам.

Проверяется не «функция вызвалась», а свойства получившегося скрипта: он
уходит на носитель и работает там без нас, до входа пользователя, и цена
ошибки в нём -- уничтоженный по недоразумению носитель либо, наоборот,
отозванный носитель, который продолжает работать.
"""
from __future__ import annotations

import subprocess

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
    assert "nonce=$NONCE" in script, "случайное число не уходит в запрос"
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
    # Носитель, о котором ресурс промолчал или ответил «в порядке», продолжает
    # работать: срока доверия здесь нет намеренно.
    assert "сети нет, проверка пропущена" in script
    assert "ресурс недоступен, проверка пропущена" in script


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
    files = revocation.config_files(_config(), "a1b2c3d4", "0e3f1a90-1111-2222-3333-444455556666")
    assert set(files) == {revocation.SCRIPT_PATH, revocation.SERVICE_PATH}
    assert files[revocation.SCRIPT_PATH][1] == 0o755, "скрипт должен быть исполняемым"
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
    from portable_os_admin.install import autoupdate

    assert "RequiresMountsFor=/var/log" in revocation.service_unit()
    assert "RequiresMountsFor=/var/log" in autoupdate.service_unit(_config())


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

    # Разметка -- в самом конце и только от проверенного раздела: ошибиться
    # здесь диском страшнее, чем пропустить шаг.
    disk_at = script.index("lsblk -no pkname")
    boot_at = script.index('wipe_boot "$BOOT_UUID"')
    assert boot_at < disk_at
    assert "$SYSTEM_DEV" in script[disk_at - 200 : disk_at]


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
        exchange_partition="/dev/sdz5",
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
