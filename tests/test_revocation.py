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
    assert '"poa-state-v3 $MEDIA_ID $NONCE ok"' in script


def test_revocation_script_cannot_write_or_erase_a_volume():
    script = _script()
    for operation in ("luksErase", "wipefs", "wipe_data", "dd if=", "poweroff", "/dev/disk/"):
        assert operation not in script


def test_identifier_and_address_are_written_into_the_script():
    """То, что никогда не попадает в командную строку, нельзя закавычить
    неправильно -- тем же приёмом собран скрипт режима киоска."""
    script = _script()
    assert "MEDIA_ID=a1b2c3d4" in script
    assert "URL=https://revoke.example.org" in script
    assert "0e3f1a90-1111-2222-3333-444455556666" not in script


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
    assert "printf 'poa-ask-v3 %s %s %s' \"$MEDIA_ID\" \"$NONCE\" \"$INFO\"" in script
    assert f"DEVICE_KEY=/{revocation.DEVICE_KEY_PATH}" in script
    for header in ("X-POA-Media", "X-POA-Nonce", "X-POA-Info", "X-POA-Signature"):
        assert f'-H "{header}: ' in script, f"в запросе нет заголовка {header}"


def test_nothing_about_the_medium_travels_in_the_address():
    """Адрес оседает в журналах посредников, заголовки -- нет."""
    script = _script()
    assert '"$URL/v3/media-state"' in script
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
    nonce_at = script.index('"poa-state-v3 $MEDIA_ID $NONCE ok"')
    open_at = script.index('mv -f "$AUTHORIZED.tmp" "$AUTHORIZED"')
    assert verify_at < nonce_at < open_at
    assert 'if [ "$STATE" = "ok" ]' in script[nonce_at:open_at]
    assert "/proc/sys/kernel/random/boot_id" in script[nonce_at:open_at]


def test_one_pass_at_a_time():
    assert "flock -n 9 || exit 0" in _script()


def test_persistent_revocation_prevents_starting_the_access_service():
    from portable_os_admin.install.access import files as access_files
    from portable_os_admin.install.revocation.paths import REVOKED_FILE, REVOKED_RUNTIME_FILE

    for path in (REVOKED_FILE, REVOKED_RUNTIME_FILE):
        assert f"ConditionPathExists=!{path}" in access_files.service_unit()
        assert path in revocation.gate_script()


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
    signature = key.sign(b"poa-ask-v3 a1b2c3d4 00 version=1;machine=00")
    ed25519.Ed25519PublicKey.from_public_bytes(raw).verify(
        signature, b"poa-ask-v3 a1b2c3d4 00 version=1;machine=00"
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
