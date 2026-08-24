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
    # the base system comes from the local image, never from a network mirror
    assert "file:///mnt/image" in argv


def test_debian_bootstrap_command():
    config = InstallConfig.create_default("stick1", "debian")
    backend = DebianInstallBackend()
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release), config)


def test_redos_bootstrap_command():
    config = InstallConfig.create_default("stick1", "redos")
    backend = RedOSInstallBackend()
    argv = backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
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
    _assert_debootstrap(backend.bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release), config)


def test_astra_uses_astra_admin_group_not_sudo():
    config = InstallConfig.create_default("stick1", "astra")
    config.account.sudo = True

    backend = AstraInstallBackend()
    commands = backend.chroot_setup_commands("/mnt/poa", config)

    assert any("astra-admin" in c for c in commands)
    assert not any(c[-1] == "sudo" and "usermod" in c for c in commands)


def test_debootstrap_skips_signature_check_for_a_local_image():
    """Installation images carry dists/<suite>/Release but no Release.gpg and
    no InRelease -- measured on debian-13.6.0-amd64-DVD-1.iso. Without
    --no-check-gpg debootstrap aborts with "Failed getting release signature
    file" before unpacking anything, so installing from a local image (the
    only supported source) fails every time."""
    config = InstallConfig.create_default("stick1", "debian")
    argv = DebianInstallBackend().bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
    assert "--no-check-gpg" in argv
    # the flag must precede the positional arguments, or debootstrap reads it
    # as the suite name
    assert argv.index("--no-check-gpg") < argv.index(config.release)


def test_debootstrap_keeps_signature_check_for_a_network_mirror():
    """A network mirror does publish signatures, so there the check stays on."""
    config = InstallConfig.create_default("stick1", "debian")
    argv = DebianInstallBackend().bootstrap_command(
        "/mnt/poa", config, "https://deb.debian.org/debian", config.release
    )
    assert "--no-check-gpg" not in argv


def test_astra_inherits_the_local_image_signature_behaviour():
    config = InstallConfig.create_default("stick1", "astra")
    argv = AstraInstallBackend().bootstrap_command("/mnt/poa", config, "file:///mnt/image", config.release)
    assert "--no-check-gpg" in argv


def test_redos_leaves_the_distributions_own_repository_files_alone():
    """`@core` installs redos-release, which writes correct repository files
    and ships the signing key. Verified by unpacking that package off a RED OS
    8 image: RedOS-Base.repo and RedOS-Updates.repo, gpgcheck=1 and
    gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT. Writing our own on top
    would replace something correct with something guessed."""
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = ""
    assert RedOSInstallBackend().sources_list(config, "8.0") is None


def test_redos_writes_a_mirror_next_to_them_not_over_them():
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = "https://repo.example.org/redos"
    backend = RedOSInstallBackend()

    text = backend.sources_list(config, "8.0")
    assert "repo.example.org" in text
    # a file of our own, so the distribution's stay untouched
    assert backend.sources_path == "etc/yum.repos.d/poa-mirror.repo"
    assert "RedOS-Base" not in backend.sources_path


def test_signing_settings_come_from_the_image_and_default_to_honest_silence():
    """With no image to read, the file says packages are not verified rather
    than pointing at a key path that may not exist -- a wrong gpgkey makes
    every dnf command fail with a confusing error."""
    config = InstallConfig.create_default("stick1", "redos")
    config.mirror = "https://repo.example.org/redos"
    text = RedOSInstallBackend().sources_list(config, "8.0", image_mountpoint="")
    assert "gpgcheck=0" in text
    assert "gpgkey=" not in text


def test_each_family_writes_its_sources_where_that_family_keeps_them():
    assert DebianInstallBackend().sources_path == "etc/apt/sources.list"
    assert AstraInstallBackend().sources_path == "etc/apt/sources.list"
    assert RedOSInstallBackend().sources_path.startswith("etc/yum.repos.d/")


def test_redos_package_installs_use_the_image_not_the_host_repositories():
    """dnf runs outside the chroot with --installroot, so it does not inherit
    the target's /etc/yum.repos.d. Without being told, it reaches for the
    administrator machine's own repositories -- which answer nothing on an
    isolated network and are not the chosen image in any case. The bootstrap
    command always said this; the later package installs did not."""
    backend = RedOSInstallBackend()
    backend.bind_install_source("file:///mnt/poa-image-x", "8.0")

    argv = backend.package_install_command("/mnt/poa", ["kernel"])
    assert "--repofrompath=poa-image,file:///mnt/poa-image-x" in argv
    assert "--disablerepo=*" in argv
    # an image carries no signature; the trust anchor is the file itself
    assert "--nogpgcheck" in argv
    assert "--releasever" in argv


def test_redos_package_installs_stay_plain_when_no_source_is_bound():
    """Nothing bound means nothing to say -- the installed system's own
    configuration is then the right answer, not an invented one."""
    argv = RedOSInstallBackend().package_install_command("/mnt/poa", ["kernel"])
    assert "--repofrompath" not in " ".join(argv)


def test_a_network_mirror_is_not_marked_as_unsigned():
    backend = RedOSInstallBackend()
    backend.bind_install_source("https://repo.example.org/redos/8.0/x86_64/os", "8.0")
    argv = backend.package_install_command("/mnt/poa", ["kernel"])
    assert "--nogpgcheck" not in argv
