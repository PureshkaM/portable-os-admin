import pytest

from portable_os_admin import crypto


def test_derive_key_deterministic_for_same_salt():
    params = crypto.default_kdf_params(salt=b"0" * 16)
    key1 = crypto.derive_key("hunter2-hunter2", params)
    key2 = crypto.derive_key("hunter2-hunter2", params)
    assert key1 == key2
    assert len(key1) == crypto.KEY_LENGTH


def test_derive_key_differs_for_different_password():
    params = crypto.default_kdf_params(salt=b"0" * 16)
    assert crypto.derive_key("password-one", params) != crypto.derive_key("password-two", params)


def test_encrypt_decrypt_round_trip():
    key = b"k" * 32
    nonce, ciphertext = crypto.encrypt(key, b"top secret payload")
    assert crypto.decrypt(key, nonce, ciphertext) == b"top secret payload"


def test_decrypt_fails_on_tampered_ciphertext():
    key = b"k" * 32
    nonce, ciphertext = crypto.encrypt(key, b"top secret payload")
    tampered = bytes([ciphertext[0] ^ 0xFF]) + ciphertext[1:]
    with pytest.raises(ValueError):
        crypto.decrypt(key, nonce, tampered)


def test_decrypt_fails_on_wrong_key():
    nonce, ciphertext = crypto.encrypt(b"a" * 32, b"payload")
    with pytest.raises(ValueError):
        crypto.decrypt(b"b" * 32, nonce, ciphertext)


@pytest.mark.parametrize(
    "password,expect_ok",
    [
        ("short1", False),
        ("nodigitshere", False),
        ("12345678901234", False),
        ("CorrectHorse123", True),
    ],
)
def test_validate_password_strength(password, expect_ok):
    problems = crypto.validate_password_strength(password)
    assert (len(problems) == 0) is expect_ok


def test_generate_recovery_code_shape():
    code = crypto.generate_recovery_code()
    groups = code.split("-")
    assert len(groups) == 4
    assert all(len(g) == 4 for g in groups)
    for char in code.replace("-", ""):
        assert char in crypto._RECOVERY_ALPHABET


def test_derive_recovery_passphrase_is_not_the_code_itself():
    code = crypto.generate_recovery_code()
    salt = crypto.new_salt()
    passphrase = crypto.derive_recovery_passphrase(code, salt)
    assert passphrase != code.encode()
    assert len(passphrase) == 64


def test_calibration_picks_a_power_of_two_within_the_agreed_bounds(monkeypatch):
    """Стоимость подбирается, а не берётся из кода.

    Зашитое число пришлось бы выбирать по самой слабой мыслимой машине, то есть
    отдавать стойкость даром на всех остальных. Границы при этом жёсткие: ниже
    прежнего значения опускаться нельзя (это был бы откат), выше потолка --
    тоже, иначе контейнер перестанет открываться там, где мало памяти.
    """
    monkeypatch.setattr(crypto, "_calibrated_n", None)
    monkeypatch.setenv("POA_KDF_BUDGET_SECONDS", "0.01")

    n = crypto.calibrate_scrypt_n()

    assert n >= crypto.SCRYPT_N, "калибровка опустилась ниже прежней стоимости"
    assert n <= crypto.SCRYPT_N_CEILING, "калибровка вышла за потолок памяти"
    assert n & (n - 1) == 0, "N обязан быть степенью двойки, иначе scrypt откажет"


def test_a_larger_budget_buys_a_more_expensive_key(monkeypatch):
    """Бюджет времени -- это и есть та ручка, которой задаётся стойкость."""
    monkeypatch.setattr(crypto, "_calibrated_n", None)
    monkeypatch.setenv("POA_KDF_BUDGET_SECONDS", "0.01")
    cheap = crypto.calibrate_scrypt_n()

    monkeypatch.setattr(crypto, "_calibrated_n", None)
    monkeypatch.setenv("POA_KDF_BUDGET_SECONDS", "3600")
    rich = crypto.calibrate_scrypt_n()

    assert rich > cheap
    assert rich == crypto.SCRYPT_N_CEILING


def test_a_container_made_with_older_parameters_still_opens():
    """Старый контейнер не требует миграции -- и не должен требовать.

    Параметры лежат в самом контейнере, поэтому поднятие стоимости касается
    только новых. Контейнер, созданный прежней версией, откроется своими --
    слабее, но откроется; сменить их можно сменой пароля, которая
    перешифровывает контейнер целиком.
    """
    old = crypto.default_kdf_params()
    assert old.n == crypto.SCRYPT_N

    payload = "секрет парка".encode("utf-8")
    nonce, ciphertext = crypto.encrypt(crypto.derive_key("CorrectHorse123", old), payload)

    # Ровно то, что делает `open`: параметры восстанавливаются из записанного,
    # ключ выводится ими, и содержимое читается.
    restored = crypto.KdfParams.from_dict(old.to_dict())
    reopened = crypto.derive_key("CorrectHorse123", restored)
    assert crypto.decrypt(reopened, nonce, ciphertext) == payload


def test_short_checksum_stable():
    assert crypto.short_checksum("abc", "1") == crypto.short_checksum("abc", "1")
    assert crypto.short_checksum("abc", "1") != crypto.short_checksum("abc", "2")
