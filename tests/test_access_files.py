"""Э8: что из доступа уходит в образ и что -- на носитель."""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from portable_os_admin.config.schema import InstallConfig
from portable_os_admin.install.access import files as access_files
from portable_os_admin.install.backends import get_backend


def _config(enabled: bool) -> InstallConfig:
    config = InstallConfig.create_baseline("stick", "debian")
    config.revocation.enabled = enabled
    config.revocation.url = "https://access.example.org/"
    return config


def test_nothing_without_a_server():
    assert access_files.image_files(_config(False)) == {}
    assert not set(get_backend("debian").ACCESS_PACKAGES) & set(
        get_backend("debian").desktop_packages(_config(False)))


def test_image_carries_the_client_units_and_autostart():
    files = access_files.image_files(_config(True))
    for name in access_files.RUNTIME_FILES:
        content, mode = files[f"{access_files.RUNTIME_DIR}/{name}"]
        assert content and mode == 0o755
    assert "poa_accessd.py" in files[access_files.SERVICE_PATH][0]
    assert "/run/poa/vpn/%i/client.conf" in files[access_files.VPN_UNIT_PATH][0]
    assert "poa_connect.py" in files[access_files.AUTOSTART_PATH][0]


def test_client_files_reach_the_image_through_the_backend():
    files = get_backend("redos").scripted_files(_config(True))
    assert f"{access_files.RUNTIME_DIR}/poa_accessd.py" in files


def test_family_packages_are_added_to_the_desktop():
    for family in ("debian", "redos"):
        backend = get_backend(family)
        packages = backend.desktop_packages(_config(True))
        assert "openvpn" in packages and set(backend.ACCESS_PACKAGES) <= set(packages)


def test_medium_config_names_the_server_and_its_keys():
    key = ed25519.Ed25519PrivateKey.generate()
    pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    data = json.loads(access_files.medium_config(_config(True), "0a1b2c3d-11223344", pem, ("ab" * 32,)))
    assert data["endpoint"] == "https://access.example.org"
    assert data["tls_spki"] == ["ab" * 32]
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert data["server_sign_keys"] == [base64.b64encode(raw).decode()]
    assert data["device_key"] == "/etc/poa/device.key" and data["state"] == access_files.STATE_PATH


def test_a_kiosk_with_access_needs_no_other_application():
    from portable_os_admin.config.validator import BuiltinConfigValidator as ConfigValidator
    config = _config(True)
    config.packages = []
    assert not [e for e in ConfigValidator().validate(config).errors if "хотя бы один пакет" in e]
    config.revocation.enabled = False
    assert [e for e in ConfigValidator().validate(config).errors if "хотя бы один пакет" in e]


def test_runtime_is_a_package_so_setuptools_ships_it():
    """Без __init__.py setuptools не кладёт каталог в собранную программу, и
    сборка образа на машине администратора не нашла бы код клиента."""
    # Проверяется то, на что смотрит find_packages: каталог -- пакет, если в нём
    # есть __init__.py (setuptools на стенде не установлен).
    assert (access_files.RUNTIME_SOURCE / "__init__.py").is_file()
    assert (access_files.RUNTIME_SOURCE.parent / "__init__.py").is_file()
