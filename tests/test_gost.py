"""ГОСТ Р 34.11-2012 и 34.10-2012 -- эталонные примеры RFC 6986 и RFC 7091."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

RUNTIME = pathlib.Path(__file__).resolve().parents[1] / "src/portable_os_admin/install/access/runtime"
_spec = importlib.util.spec_from_file_location("poa_gost", RUNTIME / "poa_gost.py")
gost = importlib.util.module_from_spec(_spec)
sys.modules["poa_gost"] = gost
_spec.loader.exec_module(gost)


def rfc(text: str) -> bytes:
    """Вектор из RFC (старший разряд первым) -> байты «младший первым»."""
    return bytes.fromhex("".join(text.split()))[::-1]


M1 = rfc("""32313039383736353433323130393837 36353433323130393837363534333231
            30393837363534333231303938373635 343332313039383736353433323130""")
M2 = rfc("""fbe2e5f0eee3c820fbeafaebef20fffb f0e1e0f0f520e0ed20e8ece0ebe5f0f2
            f120fff0eeec20f120faf2fee5e2202c e8f6f3ede220e8e6eee1e8f0f2d1202c
            e8f0f2e5e220e5d1""")


def test_m1_is_the_digits_string():
    assert M1 == b"012345678901234567890123456789012345678901234567890123456789012"


def test_streebog_rfc6986_example_1():
    assert gost.streebog(M1, 512) == rfc("""486f64c1917879417fef082b3381a4e2 11c324f074654c38823a7b76f830ad00
                                            fa1fbae42b1285c0352f227524bc9ab1 6254288dd6863dccd5b9f54a1ad0541b""")
    assert gost.streebog(M1, 256) == rfc("00557be5e584fd52a449b16b0251d05d 27f94ab76cbaa6da890b59d8ef1e159d")


def test_streebog_rfc6986_example_2_crosses_a_block_boundary():
    assert gost.streebog(M2, 512) == rfc("""28fbc9bada033b1460642bdcddb90c3f b3e56c497ccd0f62b8a2ad4935e85f03
                                            7613966de4ee00531ae60f3b5a47f8da e06915d5f2f194996fcabf2622e6881e""")
    assert gost.streebog(M2, 256) == rfc("508f7e553c06501d749a66fc28c6cac0 b005746d97537fa85d9e40904efed29d")


RFC7091 = gost.Curve(
    "rfc7091-test",
    p=0x8000000000000000000000000000000000000000000000000000000000000431,
    a=7,
    b=0x5FBFF498AA938CE739B8E022FBAFEF40563F6E6A3472FC2A514C0CE9DAE23B7E,
    q=0x8000000000000000000000000000000150FE8A1892976154C59CFC193ACCF5B3,
    x=2,
    y=0x8E2A8A0E65147D4BD6316030E16D19C85C97F0A9CA267122B96ABBCEA7E8FC8,
)
D = 0x7A929ADE789BB9BE10ED359DD39A72C11B60961F49397EEE1D19CE9891EC3B28
E = 0x2DFBC1B372D89A1188C09C52E0EEC61FCE52032AB1022E8E67ECE6672B043EE5
K = 0x77105C9B20BCD3122823C8CF6FCC7B956DE33814E95B7FE64FED924594DCEAB3
R = 0x41AA28D2F1AB148280CD9ED56FEDA41974053554A42767B83AD043FD39DC0493
S = 0x1456C64BA4642A1653C235A98A60249BCD6D3F746B631DF928014F6C5BF9C40


def test_rfc7091_signature_example():
    public = gost.public_key(RFC7091, D)
    assert public == (0x7F2B49E270DB6D90D8595BEC458B50C58585BA1D4E9B788F6689DBD8E56FD80B,
                      0x26F1B489D6701DD185C8413A977B3CBBAF64D1C593D26627DFFB101A87FF77DA)
    digest = E.to_bytes(32, "little")  # e = alpha mod q
    signature = gost.sign_digest(RFC7091, D, digest, k=K)
    assert signature == S.to_bytes(32, "big") + R.to_bytes(32, "big")
    assert gost.verify_digest(RFC7091, public, digest, signature)


def test_paramset_a_is_a_curve_of_order_q():
    c = gost.TC26_256_A
    assert gost.on_curve(c, (c.x, c.y))
    assert gost._mul(c, c.q, (c.x, c.y)) is None


def test_sign_and_verify_on_paramset_a_and_tamper_detection():
    c = gost.TC26_256_A
    d = gost.new_private_key(c)
    public = gost.public_key(c, d)
    signature = gost.sign(c, d, b"poa-admin-key")
    assert gost.verify(c, public, b"poa-admin-key", signature)
    assert not gost.verify(c, public, b"poa-admin-kez", signature)
    bad = bytes([signature[0] ^ 1]) + signature[1:]
    assert not gost.verify(c, public, b"poa-admin-key", bad)
    other = gost.public_key(c, gost.new_private_key(c))
    assert not gost.verify(c, other, b"poa-admin-key", signature)
    assert gost.decode_public(gost.encode_public(public)) == public


def test_a_point_off_the_curve_is_refused():
    c = gost.TC26_256_A
    assert not gost.verify(c, (1, 1), b"x", bytes(64))


def test_a_signature_made_by_openssl_gost_engine_verifies():
    """Подпись OpenSSL gost-engine (образ РЕД ОС 8, 02.10.2026) над b"poa-admin-key"."""
    c = gost.TC26_256_A
    d = 0x1D23646E5AD8AE7B374D0C2CFB85BDC0E14EAB277E257803AD996712E5875B1
    public = gost.public_key(c, d)
    assert public[0] == 0x9CCC54AD8C762FCF90D80DC04731C15781E1265D099A3804EB01432941F318C9
    assert gost.streebog(b"poa-admin-key").hex() == \
        "be358bad57a5cd38ba59beed5d5ab6a8848c8104dd7c233f895f5a0e301106b7"
    signature = bytes.fromhex(
        "2c35329a5e8fcabc7ca10e4e1667254c436ceef383f80e3a075049a2e59f9f30"
        "1751bcdf0c095c7d0cb6ca107e06dafbc9dd941535b0acfed52b39c702553036")
    assert gost.verify(c, public, b"poa-admin-key", signature)
