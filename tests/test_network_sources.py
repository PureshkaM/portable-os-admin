"""Этап Э22: сборка из актуального репозитория поверх образа.

База ставится с образа, затем подключаются сетевые репозитории -- репозиторий
компании (`mirror`) или официальный, записанный в образе, -- и всё обновляется.
`network_packages = False` -- сборка только с образа.
"""
from __future__ import annotations

import pytest

from portable_os_admin.config.schema import InstallConfig
from portable_os_admin.errors import InstallError
from portable_os_admin.install.backends import get_backend
from portable_os_admin.install import repo_metadata, repo_trust


def _config(**overrides) -> InstallConfig:
    """Базовая конфигурация: `mirror` в ней -- официальный адрес по умолчанию."""
    config = InstallConfig.create_baseline("stick", overrides.get("target_os", "debian"))
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _debian(config):
    backend = get_backend("debian")
    backend.bind_install_source("file:///mnt/image", "trixie", arch="amd64")
    backend.bind_network(config, "/mnt/image", "/target")
    return backend


def test_debian_takes_the_official_repository_with_updates_and_security():
    backend = _debian(_config())
    _, text = backend.install_time_sources("trixie", "file:///mnt/image", ("main",))
    assert "deb [trusted=yes] file:///mnt/image trixie main" in text
    assert "deb http://deb.debian.org/debian trixie main" in text
    assert "deb http://deb.debian.org/debian trixie-updates main" in text
    assert "deb http://security.debian.org/debian-security trixie-security main" in text
    # Сетевые строки подписаны и проверяются -- trusted=yes только у образа.
    assert text.count("trusted=yes") == 1


def test_a_company_mirror_replaces_the_official_repository():
    backend = _debian(_config(mirror="http://repo.corp.local/debian/"))
    _, text = backend.install_time_sources("trixie", "file:///mnt/image", ("main",))
    assert "deb http://repo.corp.local/debian trixie main" in text
    assert "deb http://repo.corp.local/debian trixie-updates main" in text
    assert "deb.debian.org" not in text and "security.debian.org" not in text


def test_isolated_build_uses_the_image_only_and_upgrades_nothing():
    backend = _debian(_config(network_packages=False))
    _, text = backend.install_time_sources("trixie", "file:///mnt/image", ("main",))
    assert "http" not in text
    assert backend.system_upgrade_command("/target") is None


def test_debian_upgrade_keeps_the_files_this_program_wrote():
    argv = _debian(_config()).system_upgrade_command("/target")
    assert argv[:2] == ["chroot", "/target"]
    assert "dist-upgrade" in argv and "Dpkg::Options::=--force-confold" in argv


def test_redos_takes_enabled_repositories_from_the_release_package(monkeypatch):
    monkeypatch.setattr(repo_metadata, "discover_repos", lambda mountpoint, arch: (
        repo_metadata.Repo("base", f"https://repo1.red-soft.ru/redos/8.0c/{arch}/os",
                           "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"),
        repo_metadata.Repo("updates", f"https://repo1.red-soft.ru/redos/8.0c/{arch}/updates",
                           "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"),
    ))
    backend = get_backend("redos")
    backend.bind_install_source("file:///mnt/image", "8.0", arch="x86_64")
    backend.bind_network(_config(target_os="redos"), "/mnt/image", "/target")
    argv = backend.system_upgrade_command("/target")
    joined = " ".join(argv)
    assert "--repofrompath=poa-net-base,https://repo1.red-soft.ru/redos/8.0c/x86_64/os" in joined
    assert "--repo=poa-net-updates" in joined
    # Ключ ищется в целевом корне: dnf запущен снаружи, у хоста этого файла нет.
    assert "--setopt=poa-net-base.gpgkey=file:///target/etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT" in joined
    assert "--setopt=poa-net-base.gpgcheck=1" in joined
    assert argv[-1] == "upgrade"
    # Ключ RED SOFT подписан SHA-1: строгая политика rpm-sequoia хоста его не
    # примет, поэтому dnf сборки получает свою политику (только этот процесс).
    assert argv[0] == "env" and argv[1].startswith("RPM_SEQUOIA_CRYPTO_POLICY=")
    policy = open(argv[1].split("=", 1)[1]).read()
    assert "sha1.collision_resistance" in policy


@pytest.fixture
def cert_image(monkeypatch):
    repos = [
        repo_metadata.Repo(channel, f"https://repo1.red-soft.ru/redos/8.0c/x86_64/{channel}/",
                           "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT")
        for channel in ("os", "updates")
    ]
    monkeypatch.setattr(repo_metadata, "discover_repos", lambda _mountpoint, _arch: tuple(repos))
    monkeypatch.setattr(repo_trust, "build_bundle", lambda *_args: "")
    return repos


@pytest.mark.parametrize("release, mirror, host", [
    ("8.0", "https://repo1.red-soft.ru/redos/8.0/x86_64/os", "repo1.red-soft.ru"),
    ("8.0c", "https://mirror.yandex.ru/redos/8.0c/x86_64/os/", "mirror.yandex.ru"),
])
def test_redos_certified_image_enables_only_signed_certified_repos(cert_image, release, mirror, host):
    cert_image.append(repo_metadata.Repo("extra", "https://elsewhere.example/os"))
    config = _config(target_os="redos", arch="amd64", release=release, mirror=mirror)
    backend = get_backend("redos")
    backend.prepare_package_sources(config, image_mountpoint="/mnt/image")
    backend.bind_install_source("file:///mnt/image", "8.0", arch="x86_64")

    bootstrap = backend.bootstrap_command("/target", config, "file:///mnt/image", "8.0")
    assert "--setopt=poa-image.gpgcheck=0" in bootstrap and "--nogpgcheck" not in bootstrap

    backend.bind_network(config, "/mnt/image", "/target")
    argv = backend.system_upgrade_command("/target")
    assert "--nogpgcheck" not in argv and "--setopt=poa-image.gpgcheck=0" in argv
    assert [arg for arg in argv if arg.startswith("--repo=")] == [
        "--repo=poa-image", "--repo=poa-net-os", "--repo=poa-net-updates",
    ]
    for channel in ("os", "updates"):
        assert f"--repofrompath=poa-net-{channel},https://{host}/redos/8.0c/x86_64/{channel}" in argv
        assert f"--setopt=poa-net-{channel}.gpgcheck=1" in argv
        assert f"--setopt=poa-net-{channel}.gpgkey=file:///target/etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT" in argv
        assert f"--setopt=poa-net-{channel}.sslverify=1" in argv
        assert f"--setopt=poa-net-{channel}.skip_if_unavailable=False" in argv


@pytest.mark.parametrize("mirror", [
    "https://mirror.yandex.ru/redos/8.0/x86_64/os",
    "https://mirror.yandex.ru.example.org/redos/8.0c/x86_64/os",
    "http://repo1.red-soft.ru/redos/8.0c/x86_64/os",
])
def test_redos_certified_image_rejects_unverified_mirrors(cert_image, mirror):
    config = _config(target_os="redos", arch="amd64", release="8.0", mirror=mirror)
    backend = get_backend("redos")
    with pytest.raises(InstallError, match="официальное HTTPS-зеркало"):
        backend.prepare_package_sources(config, image_mountpoint="/mnt/image")


def test_redos_certified_image_can_build_offline_but_checks_the_key_for_network_repos(cert_image):
    updates = cert_image.pop()
    cert_image.append(repo_metadata.Repo(updates.repo_id, updates.baseurl, "file:///some-other-key"))
    config = _config(target_os="redos", arch="amd64", release="8.0c", network_packages=False)
    backend = get_backend("redos")
    backend.prepare_package_sources(config, image_mountpoint="/mnt/image")
    backend.bind_network(config, "/mnt/image", "/target")
    assert backend.system_upgrade_command("/target") is None

    config.network_packages = True
    with pytest.raises(InstallError, match="настройки Base и Updates с ключом"):
        backend.prepare_package_sources(config, image_mountpoint="/mnt/image")


def test_redos_mirror_reuses_the_distribution_key(monkeypatch):
    monkeypatch.setattr(repo_metadata, "discover_repos", lambda mountpoint, arch: (
        repo_metadata.Repo("base", "https://x/os", "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"),))
    backend = get_backend("redos")
    backend.bind_install_source("file:///mnt/image", "8.0", arch="x86_64")
    backend.bind_network(_config(target_os="redos", mirror="https://repo.corp/redos/8.0c/x86_64/os"),
                         "/mnt/image", "/target")
    joined = " ".join(backend.system_upgrade_command("/target"))
    assert "--repofrompath=poa-mirror,https://repo.corp/redos/8.0c/x86_64/os" in joined
    assert "--repofrompath=poa-mirror-updates,https://repo.corp/redos/8.0c/x86_64/updates" in joined
    assert "--setopt=poa-mirror.skip_if_unavailable=False" in joined
    assert "--setopt=poa-mirror-updates.skip_if_unavailable=False" in joined
    assert "repo1.red-soft.ru" not in joined and "https://x/os" not in joined
    assert "poa-mirror.gpgkey=file:///target/etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT" in joined


def test_the_default_official_address_is_not_taken_for_a_company_mirror():
    """`mirror` по умолчанию -- официальный адрес семейства; он не считается
    репозиторием компании, и официальные репозитории берутся из образа."""
    config = _config()
    assert config.mirror, "базовая конфигурация заполняет mirror"
    assert get_backend("debian").company_mirror(config) == ""
    config.mirror = "http://repo.corp.local/debian"
    assert get_backend("debian").company_mirror(config) == "http://repo.corp.local/debian"


def test_redos_drops_the_kernel_left_from_the_image_after_upgrade(monkeypatch):
    """Обновление приносит новое ядро, а прежнее остаётся (installonly) --
    430 МБ модулей в каждом выпуске (найдено на сборке 02.10.2026)."""
    monkeypatch.setattr(repo_metadata, "discover_repos", lambda mountpoint, arch: (
        repo_metadata.Repo("base", "https://x/os", "file:///etc/pki/rpm-gpg/RPM-GPG-KEY-RED-SOFT"),))
    backend = get_backend("redos")
    backend.bind_install_source("file:///mnt/image", "8.0", arch="x86_64")
    backend.bind_network(_config(target_os="redos"), "/mnt/image", "/target")
    script = backend.old_kernels_command("/target")[-1]
    assert "sort -V" in script and "rpm --root /target -qf" in script
    # Из rpm -qf -- только успешный ответ: текст ошибки в dnf remove недопустим.
    assert '2>/dev/null) && old="$old $p"' in script


def test_isolated_build_keeps_its_only_kernel():
    backend = get_backend("redos")
    backend.bind_install_source("file:///mnt/image", "8.0", arch="x86_64")
    backend.bind_network(_config(target_os="redos", network_packages=False), "/mnt/image", "/target")
    assert backend.old_kernels_command("/target") is None
