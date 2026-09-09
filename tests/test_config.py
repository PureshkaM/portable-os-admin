import pytest

from portable_os_admin.config import ConfigManager, InstallConfig
from portable_os_admin.config.validator import (
    BuiltinConfigValidator,
    ConfigValidationUnavailableError,
    ExternalCommandValidator,
)


def test_create_default_config_is_initially_unchecked(container):
    cm = ConfigManager()
    config = cm.create(container, "stick1", "debian")
    assert config.validation_status == "unchecked"
    assert config.target_os == "debian"


def test_valid_config_passes_builtin_validation(make_config):
    config = make_config()
    result = BuiltinConfigValidator().validate(config)
    assert result.ok
    assert result.errors == []


@pytest.mark.parametrize(
    "mutate,expected_substring",
    [
        (lambda c: setattr(c, "target_os", "windows"), "целевая ОС"),
        (lambda c: setattr(c, "release", ""), "релиз"),
        (lambda c: setattr(c.disk, "boot_partition_mb", 10), "/boot"),
        (lambda c: setattr(c.disk, "esp_partition_mb", 10), "EFI-раздела"),
        (lambda c: setattr(c, "arch", "sparc"), "архитектура"),
        (lambda c: setattr(c.luks, "failed_attempts_limit", 0), "лимит попыток"),
        (lambda c: setattr(c.account, "username", "1bad name"), "имя пользователя"),
        (lambda c: setattr(c.remote_access, "enabled", True) or setattr(c.remote_access, "port", 0), "порт"),
    ],
)
def test_invalid_configs_are_rejected(mutate, expected_substring, make_config):
    config = make_config()
    mutate(config)
    result = BuiltinConfigValidator().validate(config)
    assert not result.ok
    assert any(expected_substring in err for err in result.errors)


def test_config_manager_validate_updates_status_in_place(make_config):
    cm = ConfigManager()
    config = make_config("stick1", "redos")
    cm.validate(config)
    assert config.validation_status == "valid"


def test_save_and_open_round_trip(container):
    """Конфигурация возвращается из контейнера такой же, какой ушла.

    Хранилище -- контейнер, а не каталог рядом с ним: с 09.09.2026
    конфигурации не лежат отдельными файлами, потому что в них описано
    устройство защищаемой системы.
    """
    cm = ConfigManager()
    config = cm.create(container, "stick1", "debian")
    config.account.username = "ivanov"
    config.packages = ["vim", "htop"]
    cm.save(container, config)

    reopened = cm.open(container, "stick1")
    assert reopened.account.username == "ivanov"
    assert reopened.packages == ["vim", "htop"]


def test_a_configuration_is_not_a_file_next_to_the_container(container, tmp_path):
    """Рядом с контейнером не появляется ни одного файла конфигурации.

    Проверяется именно отсутствие: до 09.09.2026 `configs/*.json` лежали
    открытым текстом, и заметить это по коду было трудно -- там не было
    ничего похожего на ошибку, просто другое хранилище.
    """
    cm = ConfigManager()
    cm.create(container, "stick1", "debian")

    assert list(tmp_path.glob("**/*.json")) == []
    assert [record.name for record in container.list_configs()] == ["stick1"]


def test_external_validator_unavailable_when_command_missing():
    validator = ExternalCommandValidator("this-command-does-not-exist-poa")
    config = InstallConfig.create_default("stick1", "debian")
    with pytest.raises(ConfigValidationUnavailableError):
        validator.validate(config)


def test_list_configs_is_empty_in_a_fresh_container(container):
    cm = ConfigManager()
    assert cm.list_configs(container) == []


def test_configuration_is_checked_by_the_distributions_own_tool(make_config, monkeypatch):
    """The project's own rules only prove a configuration agrees with itself.

    Each target family ships an official validator for its own automated-install
    file, so the configuration is rendered into that format and handed to it --
    ksvalidator for the RHEL family, debconf-set-selections for Debian.
    """
    from portable_os_admin.config import native
    from portable_os_admin.config.validator import NATIVE_VALIDATORS

    assert native.render(make_config("s", "debian"))[0] == "preseed"
    assert native.render(make_config("s", "redos"))[0] == "kickstart"

    # The Debian tool reports a malformed file as a *warning* and still exits 0
    # -- verified against the real tool -- so its verdict has to come from its
    # output. Trusting the exit status would pass everything.
    assert NATIVE_VALIDATORS["preseed"][2] == "output"
    assert NATIVE_VALIDATORS["kickstart"][2] == "status"


def test_missing_distribution_tool_is_reported_not_silently_skipped(make_config, monkeypatch):
    """pykickstart is not packaged for Debian, so a RedOS configuration written
    on a Debian workstation genuinely cannot be checked this way. Saying the
    configuration is fine would overstate what was verified."""
    from portable_os_admin.config import validator as validator_module

    monkeypatch.setattr(validator_module.shutil, "which", lambda name: None)
    manager = ConfigManager()
    config = make_config("stick1", "redos")
    manager.validate(config)

    assert config.validation_status == "valid"  # internal rules passed
    assert "не установлено" in manager.native_validation_note


def test_native_rendering_carries_the_authored_values(make_config):
    config = make_config("stick1", "redos")
    config.account.username = "ivanov"
    config.account.sudo = True
    config.packages = ["htop"]
    config.remote_access.enabled = True

    from portable_os_admin.config import native

    text = native.to_kickstart(config)
    assert "--name=ivanov" in text
    assert "--groups=wheel" in text  # the RHEL family's admin group, not sudo
    assert "htop" in text
    assert "openssh-server" in text
    assert f"--cipher={config.luks.cipher}" in text


def test_localisation_reaches_both_the_native_file_and_the_medium():
    """Язык, раскладка и пояс должны доходить до системы, а не только до валидатора.

    До 26.08.2026 они были зашиты (`en_US.UTF-8`, `us`, `UTC`) и попадали
    только в файл для проверки: администратор мог задать что угодно, а
    носитель всё равно получал английскую систему по UTC. Для РЕД ОС в
    российской организации это неверное умолчание, которое ещё и не
    исправлялось.
    """
    from portable_os_admin.config import native
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.hostname = "kiosk-07"
    config.localisation.locale = "ru_RU.UTF-8"
    config.localisation.keymap = "ru"
    config.localisation.x11_layouts = "us,ru"
    config.localisation.x11_options = "grp:alt_shift_toggle"
    config.localisation.timezone = "Europe/Moscow"

    fmt, text = native.render(config)
    assert fmt == "kickstart"
    assert "lang ru_RU.UTF-8" in text
    assert "keyboard --vckeymap=ru" in text
    assert "timezone Europe/Moscow" in text
    assert "kiosk-07" in text

    backend = get_backend("redos")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )
    files = backend.config_files(config, layout)
    assert files["etc/locale.conf"] == "LANG=ru_RU.UTF-8\n"
    # Раскладка и шрифт: FONT появился 01.09.2026, потому что dracut кладёт в
    # initramfs ровно тот шрифт, что назван здесь, а консольный по умолчанию
    # кириллицы не знает.
    assert "KEYMAP=ru" in files["etc/vconsole.conf"]
    assert "FONT=LatArCyrHeb-16" in files["etc/vconsole.conf"]
    assert files["etc/hostname"] == "kiosk-07\n"
    assert "kiosk-07" in files["etc/hosts"]
    keyboard = files["etc/X11/xorg.conf.d/00-poa-keyboard.conf"]
    assert '"us,ru"' in keyboard, "раскладка графического сеанса не задана"
    assert "grp:alt_shift_toggle" in keyboard

    # И часовой пояс — командой, потому что /etc/localtime это ссылка.
    commands = [" ".join(argv) for argv in backend.localisation_commands("/mnt/root", config)]
    assert any("zoneinfo/Europe/Moscow" in c for c in commands)


def test_a_configuration_written_before_localisation_existed_still_loads():
    """Старые файлы не имеют этого ключа и должны получать прежние значения."""
    config = InstallConfig.from_dict({"name": "old", "target_os": "debian", "release": "trixie"})

    assert config.localisation.locale == "en_US.UTF-8"
    assert config.localisation.timezone == "UTC"
    assert config.hostname == "portable-os"


def test_the_new_fields_reach_the_installed_system_not_just_the_schema():
    """Поле, которое никуда не передаётся, — худший вид «настраиваемости».

    В этом проекте такое уже находили: `luks.kdf` существовал, но в
    cryptsetup не попадал, и заданное значение ничего не меняло. Поэтому
    каждое новое поле проверяется по тому, доходит ли оно до системы.
    """
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "redos")
    config.release = "8.0"
    config.network.method = "static"
    config.network.address = "192.168.1.50/24"
    config.network.gateway = "192.168.1.1"
    config.network.nameservers = "192.168.1.1,8.8.8.8"
    config.firewall.selinux = "permissive"
    config.firewall.enabled = False
    config.extra_users = [
        {"username": "service", "sudo": True, "ssh_keys": ["ssh-ed25519 AAAA test@host"]}
    ]

    backend = get_backend("redos")
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )
    files = backend.config_files(config, layout)

    network = files["etc/NetworkManager/system-connections/poa-static.nmconnection"]
    assert "192.168.1.50/24,192.168.1.1" in network
    assert "8.8.8.8" in network
    assert "SELINUX=permissive" in files["etc/selinux/config"]

    commands = [" ".join(argv) for argv in backend.chroot_setup_commands("/mnt/root", config)]
    joined = "\n".join(commands)
    assert "useradd" in joined and "service" in joined
    assert "authorized_keys" in joined
    assert "disable firewalld" in joined

    # А при включённом брандмауэре открытые порты должны доходить до правил.
    config.firewall.enabled = True
    config.firewall.open_ports = ["22/tcp", "5900/tcp"]
    with_ports = "\n".join(
        " ".join(argv) for argv in backend.firewall_commands("/mnt/root", config)
    )
    assert "firewall-offline-cmd --add-port=22/tcp" in with_ports
    assert "5900/tcp" in with_ports


def test_dhcp_writes_no_network_file_at_all():
    """Переносной носитель не знает, в какой сети окажется.

    Лишний файл настройки тут не нейтрален: он помешал бы NetworkManager
    поднять интерфейс так, как надо на месте.
    """
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    config.release = "trixie"
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    files = get_backend("debian").config_files(config, layout)
    assert not any("NetworkManager" in path for path in files)


def test_kernel_parameters_reach_the_bootloader():
    from portable_os_admin.install.backends import get_backend
    from portable_os_admin.install.disk import build_media_layout, plan_partitions

    config = InstallConfig.create_baseline("stick", "debian")
    config.release = "trixie"
    config.boot.kernel_parameters = "intel_iommu=on"
    config.boot.menu_timeout_seconds = 0
    config.boot.quiet = True
    layout = build_media_layout(
        plan_partitions("/dev/sdb", config.disk), mapper_name="poa-x", mount_root="/mnt/root"
    )

    grub = get_backend("debian").config_files(config, layout)["etc/default/grub"]
    # Теперь строка одна на оба семейства: пользовательские параметры и
    # «тихая загрузка» складываются в неё вместе.
    assert "intel_iommu=on" in grub
    assert "GRUB_TIMEOUT=0" in grub
    # Заставка и «тихая загрузка» -- рядом, но независимо: первая рисует экран
    # пользователю и стоит всегда, вторая убирает сообщения ядра по просьбе
    # администратора.
    assert "splash" in grub
    assert "quiet" in grub
