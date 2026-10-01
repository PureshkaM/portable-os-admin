"""Свой QR-кодировщик окна активации -- проверяется настоящим декодером zbar."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime/poa_qr.py"
_spec = importlib.util.spec_from_file_location("poa_qr", _PATH)
qr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qr)

needs_zbar = pytest.mark.skipif(shutil.which("zbarimg") is None, reason="нет zbarimg")


def _decode(tmp_path, text: str) -> str:
    path = tmp_path / "code.pbm"
    path.write_bytes(qr.to_pbm(qr.encode(text)))
    out = subprocess.run(["zbarimg", "--quiet", "--raw", str(path)], capture_output=True, text=True)
    return out.stdout.rstrip("\n")


@needs_zbar
@pytest.mark.parametrize("text", [
    "A", "hello",
    "otpauth://totp/POA:0a1b2c3d-11223344?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=POA&digits=6&period=30",
    "x" * 60, "y" * 120, "z" * 200,
])
def test_zbar_reads_back_what_was_encoded(tmp_path, text):
    assert _decode(tmp_path, text) == text


def test_size_grows_with_the_version():
    assert len(qr.encode("A")) == 21
    assert len(qr.encode("y" * 120)) == 4 * 7 + 17


def test_too_long_is_refused():
    with pytest.raises(ValueError):
        qr.encode("x" * 300)
