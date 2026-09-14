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
    return revocation.revocation_script(config, "a1b2c3d4", "/dev/sdb3")


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
    assert "DEVICE=/dev/sdb3" in script


def test_nothing_is_written_when_the_feature_is_off():
    off = _config(enabled=False)
    assert revocation.config_files(off, "a1b2c3d4", "/dev/sdb3") == {}
    assert revocation.chroot_commands("/mnt/root", off, backend=None) == []

    # И при включённом отзыве, но неизвестном носителе: общий на всех скрипт
    # спрашивал бы ресурс об одном и том же идентификаторе со всех флешек.
    assert revocation.config_files(_config(), "", "") == {}


def test_files_land_where_the_service_can_find_them():
    files = revocation.config_files(_config(), "a1b2c3d4", "/dev/sdb3")
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
    script = revocation.revocation_script(_config(), "a1b2c3d4", "/dev/sdb3")

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


def test_the_service_is_enabled_only_where_its_file_exists():
    """Иначе сборка дистрибутива падает на пустом месте.

    Файлы отзыва пишутся при записи носителя: скрипт называет идентификатор
    этого носителя и его раздел. Включение же шло по одному лишь «отзыв
    включён в конфигурации», и сборка РЕД ОС 14.09.2026 встала на шаге
    учётной записи и служб: «Failed to enable unit, unit
    poa-revocation.service does not exist».
    """

    class _Backend:
        def systemctl_enable(self, root, unit):
            return ["chroot", root, "systemctl", "enable", unit]

    config = _config()

    # Сборка дистрибутива: носителя ещё нет, файлов тоже.
    assert revocation.chroot_commands("/mnt/root", config, _Backend()) == []

    # Запись носителя: файлы написаны, службу можно включать.
    enabled = revocation.chroot_commands(
        "/mnt/root", config, _Backend(), media_id="a1b2c3d4"
    )
    assert enabled and enabled[0][-1] == revocation.SERVICE_NAME
