from portable_os_admin.config import InstallConfig
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install.backends.astra import AstraInstallBackend
from portable_os_admin.install.backends.debian import DebianInstallBackend
from portable_os_admin.install.backends.redos import RedOSInstallBackend


def test_get_backend_returns_expected_types():
    assert isinstance(get_backend("debian"), DebianInstallBackend)
    assert isinstance(get_backend("redos"), RedOSInstallBackend)
    assert isinstance(get_backend("astra"), AstraInstallBackend)


def _assert_debootstrap(argv, config):
    # debootstrap is wrapped in eatmydata when the host provides it, so this
    # must not assume it sits at argv[0] -- otherwise the suite would pass or
    # fail depending on which packages the test machine happens to have.
    assert argv[0] in ("debootstrap", "eatmydata")
    assert "debootstrap" in argv
    assert config.release in argv
    assert "/mnt/poa" in argv


def test_debian_bootstrap_command():
    config = InstallConfig.create_default("stick1", "debian")
    backend = DebianInstallBackend()
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config), config)


def test_redos_bootstrap_command():
    config = InstallConfig.create_default("stick1", "redos")
    backend = RedOSInstallBackend()
    argv = backend.bootstrap_command("/mnt/poa", config)
    assert argv[0] == "dnf"
    assert "--installroot" in argv
    assert "/mnt/poa" in argv


def test_chroot_setup_commands_include_account_sudo_and_system_tasks():
    config = InstallConfig.create_default("stick1", "debian")
    config.account.username = "ivanov"
    config.account.sudo = True
    config.system_tasks = ["echo hi > /etc/motd"]

    backend = DebianInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("useradd" in c and "ivanov" in c for c in commands)
    assert any("usermod" in c and "sudo" in c for c in commands)
    assert any("sh" in c and "-c" in c and "echo hi > /etc/motd" in c for c in commands)
    # the OS login password is always locked -- LUKS passphrase is the front door
    assert any("passwd" in c and "-l" in c for c in commands)


def test_redos_uses_wheel_group_and_sshd_service():
    config = InstallConfig.create_default("stick1", "redos")
    config.account.sudo = True
    config.remote_access.enabled = True
    config.remote_access.method = "openssh"

    backend = RedOSInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("wheel" in c for c in commands)
    assert any("sshd" in c for c in commands)


def test_astra_bootstrap_command_reuses_debootstrap():
    config = InstallConfig.create_default("stick1", "astra")
    backend = AstraInstallBackend()
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config), config)


def test_astra_uses_astra_admin_group_not_sudo():
    config = InstallConfig.create_default("stick1", "astra")
    config.account.sudo = True

    backend = AstraInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("astra-admin" in c for c in commands)
    assert not any(c[-1] == "sudo" and "usermod" in c for c in commands)
