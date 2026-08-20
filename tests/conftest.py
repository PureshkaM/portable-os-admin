"""Shared fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture
def iso_path(tmp_path) -> str:
    """Stands in for a distribution image.

    Validation only checks that the file exists -- whether it is a usable image
    is decided at install time by reading what is inside it, which needs a real
    ISO and root. Tests that care about that check `image_source` directly.
    """
    path = tmp_path / "debian-13.6.0-amd64-DVD-1.iso"
    path.write_bytes(b"not a real image")
    return str(path)


@pytest.fixture
def make_config():
    """A configuration that passes validation.

    The installation image is deliberately not part of it: the image is chosen
    per run, in the install wizard, because its path belongs to one machine.
    """
    from portable_os_admin.config import InstallConfig

    def _make(name: str = "stick1", target_os: str = "debian") -> InstallConfig:
        return InstallConfig.create_default(name, target_os)

    return _make
