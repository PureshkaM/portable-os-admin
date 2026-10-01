"""Э11: фраза LUKS по умолчанию генерируется, своя -- от 10 знаков."""
from __future__ import annotations

from portable_os_admin import crypto
from portable_os_admin.config.schema import LuksConfig


def test_generated_passphrase_is_twelve_unambiguous_latin_characters():
    phrase = crypto.generate_passphrase()
    assert len(phrase) == 12
    assert set(phrase) <= set(crypto.PASSPHRASE_ALPHABET)
    assert not set("0Oo1lI") & set(crypto.PASSPHRASE_ALPHABET)
    assert phrase.isascii()


def test_generated_passphrase_carries_about_seventy_bits():
    assert len(crypto.PASSPHRASE_ALPHABET) == 56
    assert 69 < crypto.passphrase_bits() < 70


def test_generated_passphrases_differ():
    assert len({crypto.generate_passphrase() for _ in range(50)}) == 50


def test_own_passphrase_minimum_is_ten():
    assert LuksConfig().min_passphrase_length == 10
