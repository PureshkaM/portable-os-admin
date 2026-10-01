"""The starting point of a new configuration, and the change list against it.

A wizard's defaults are a statement about what the tool is for. These fix that
statement in place: a new configuration is the most restricted medium this
program can build, and everything the administrator does afterwards is an
opening they made on purpose -- which is exactly what the last step lists back
to them.
"""
from __future__ import annotations

from portable_os_admin.config import InstallConfig
from portable_os_admin.config.validator import BuiltinConfigValidator


def test_a_new_configuration_starts_as_a_kiosk():
    """Not as an ordinary system someone must know to restrict."""
    config = InstallConfig.create_baseline("stick", "debian")

    assert config.profile == "kiosk"
    assert config.kiosk.enabled is True
    assert config.kiosk.desktop_environment == "mate"


def test_every_optional_opening_starts_closed():
    config = InstallConfig.create_baseline("stick", "debian")

    assert config.kiosk.allow_removable_media is False
    assert config.account.sudo is False
    assert config.remote_access.enabled is False
    assert config.packages == []
    assert config.system_tasks == []


def test_shutdown_is_granted_because_the_alternative_is_worse():
    """Without it the only way to stop the machine is pulling the medium out."""
    assert InstallConfig.create_baseline("stick", "debian").kiosk.allow_shutdown is True


def test_the_baseline_asks_for_the_one_thing_it_cannot_guess():
    """Ни одного прикладного пакета, и киоск без них не проходит проверку.

    В этом и смысл: какие программы нужны конкретному киоску, за
    администратора не угадать, и это единственное решение, которое он обязан
    принять сам. С 02.09.2026 оно принимается выбором пакетов -- система
    собирается минимальной, и что поставлено, то пользователю и доступно.
    """
    config = InstallConfig.create_baseline("stick", "debian")
    assert config.kiosk.hidden_launchers == []

    errors = BuiltinConfigValidator().validate(config).errors
    assert any("хотя бы один пакет" in e for e in errors)

    config.packages = ["mate-calc"]
    assert BuiltinConfigValidator().validate(config).errors == []


def test_create_default_is_left_alone():
    """The installer and the tests rely on it being a plain configuration."""
    config = InstallConfig.create_default("stick", "debian")

    assert config.profile == "console"
    assert config.kiosk.enabled is False


# ----------------------------------------------------------------------
# The change list
# ----------------------------------------------------------------------
def test_an_untouched_configuration_reports_no_changes():
    baseline = InstallConfig.create_baseline("stick", "debian")
    assert InstallConfig.create_baseline("stick", "debian").diff_from(baseline) == []


def test_changes_are_reported_in_the_words_the_interface_uses():
    """A path like kiosk.allow_removable_media tells an administrator nothing."""
    baseline = InstallConfig.create_baseline("stick", "debian")
    config = InstallConfig.create_baseline("stick", "debian")
    config.kiosk.allow_removable_media = True
    config.account.sudo = True

    changes = dict((label, (before, after)) for label, before, after in config.diff_from(baseline))

    assert changes["Съёмные носители"] == ("нет", "да")
    assert changes["Права администратора у пользователя"] == ("нет", "да")


def test_lists_and_empty_values_are_readable():
    baseline = InstallConfig.create_baseline("stick", "debian")
    config = InstallConfig.create_baseline("stick", "debian")
    config.packages = ["mate-calc", "atril"]
    config.arch = "amd64"

    changes = dict((label, (before, after)) for label, before, after in config.diff_from(baseline))

    assert changes["Предустановленные пакеты"] == ("(пусто)", "mate-calc, atril")
    assert changes["Архитектура носителя"] == ("(пусто)", "amd64")


def test_derived_fields_are_not_reported_as_changes():
    """Validation status is computed, not authored -- listing it is noise."""
    baseline = InstallConfig.create_baseline("stick", "debian")
    config = InstallConfig.create_baseline("stick", "debian")
    config.validation_status = "valid"
    config.validation_errors = []

    assert config.diff_from(baseline) == []


def test_a_field_with_no_label_still_appears():
    """A new field must show up in the list the day it is added.

    Falling back to the raw path keeps an unlabelled field visible rather than
    silently unreported, which is the failure that would matter.
    """
    from portable_os_admin.config.schema import FIELD_LABELS, _diff_dicts

    changes = _diff_dicts({"brand_new_field": 1}, {"brand_new_field": 2})

    assert changes == [("brand_new_field", "1", "2")]
    assert "brand_new_field" not in FIELD_LABELS

def test_a_configuration_written_by_the_previous_version_still_opens():
    """Конфигурации живут дольше кода, и правка схемы не должна их хоронить.

    Проверено 02.09.2026 живым приложением: после переименования одного поля
    список конфигураций упал с `TypeError: unexpected keyword argument
    'allowed_apps'`, и экран остался пустым -- у администратора при этом лежало
    рядом пять готовых файлов.
    """
    old = InstallConfig.create_baseline("stick", "debian").to_dict()
    old["kiosk"]["allowed_apps"] = ["mate-calc", "atril"]
    old["packages"] = []

    config = InstallConfig.from_dict(old)

    # Имена не выбрасываются: администратор перечислял приложения, которые
    # хотел дать пользователю, и теперь это ровно то, что ставится пакетами.
    assert config.packages == ["mate-calc", "atril"]
    # И конфигурация помечена непроверенной: она изменилась, подтверждать её
    # надо заново.
    assert config.validation_status == "unchecked"


def test_a_configuration_from_a_newer_version_opens_too():
    """Незнакомый ключ -- повод не понять его, а не повод не открыться."""
    data = InstallConfig.create_baseline("stick", "debian").to_dict()
    data["ещё_не_придумано"] = {"какое-то": "значение"}
    data["kiosk"]["будущий_ключ"] = True

    config = InstallConfig.from_dict(data)

    assert config.name == "stick"
    assert config.kiosk.enabled is True


def test_migration_does_not_overwrite_packages_that_are_already_there():
    """Если пакеты заданы, старое поле не должно их подменять."""
    old = InstallConfig.create_baseline("stick", "debian").to_dict()
    old["kiosk"]["allowed_apps"] = ["atril"]
    old["packages"] = ["mate-calc"]

    config = InstallConfig.from_dict(old)

    assert config.packages == ["mate-calc"]


def test_a_configuration_written_with_package_autoupdate_still_opens():
    """Пакетное автообновление упразднено в LIVE-2.0 -- носитель обновляется
    выпуском образа целиком. Конфигурации, сохранённые раньше, несут ключ
    `auto_update`; он должен молча отбрасываться, а не ронять загрузку."""
    data = InstallConfig.create_baseline("stick", "debian").to_dict()
    data["auto_update"] = {"enabled": True, "include_kernel": True, "delay_minutes": 5}

    config = InstallConfig.from_dict(data)

    assert not hasattr(config, "auto_update")
    assert "auto_update" not in config.to_dict()


def test_a_kde_configuration_opens_as_mate():
    """KDE Plasma упразднён 01.10.2026 (LIVE-2.0). Конфигурация, собиравшая
    киоск на KDE, должна открываться -- как MATE-киоск, с ключом типа сеанса
    (x11|wayland), который был нужен только KDE, молча отброшенным."""
    data = InstallConfig.create_baseline("stick", "redos").to_dict()
    data["kiosk"]["desktop_environment"] = "kde"
    data["kiosk"]["session_type"] = "wayland"
    data["validation_status"] = "valid"

    config = InstallConfig.from_dict(data)

    assert config.kiosk.desktop_environment == "mate"
    assert not hasattr(config.kiosk, "session_type")
    assert "session_type" not in config.to_dict()["kiosk"]
    # Собираться будет уже другая система -- подтверждать её надо заново.
    assert config.validation_status == "unchecked"
    errors = BuiltinConfigValidator().validate(config).errors
    assert not any("рабочая среда" in error for error in errors)


def test_a_mate_configuration_with_the_old_session_key_keeps_its_status():
    """Ключ сеанса у MATE-конфигурации просто лишний: он отбрасывается, а
    проверенная конфигурация остаётся проверенной."""
    data = InstallConfig.create_baseline("stick", "debian").to_dict()
    data["kiosk"]["session_type"] = "x11"
    data["validation_status"] = "valid"

    config = InstallConfig.from_dict(data)

    assert config.kiosk.desktop_environment == "mate"
    assert "session_type" not in config.to_dict()["kiosk"]
    assert config.validation_status == "valid"


def test_kde_set_directly_is_refused_by_validation():
    """Миграция работает при чтении; значение, вписанное в обход неё,
    отклоняется проверкой, а не собирается молча."""
    config = InstallConfig.create_baseline("stick", "debian")
    config.kiosk.desktop_environment = "kde"

    errors = BuiltinConfigValidator().validate(config).errors

    assert any("рабочая среда" in error for error in errors)
