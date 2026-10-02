"""ГОСТ Р 34.11-2012 («Стрибог») и ГОСТ Р 34.10-2012 на чистом Python (LIVE-2.0, Э18).

Зачем своё: подпись корня организации -- ГОСТ (решение 02.10.2026), а проверять
её нужно и в программе администратора, и на носителе обоих семейств. В Python
`cryptography` ГОСТа нет, в Debian 13 нет gost-engine для OpenSSL (в РЕД ОС
есть). Своя реализация -- одна на всех.

**Проверка** работает только с открытыми данными, и побочных каналов у неё
нет. **Подпись** здесь нужна резервному корню-файлу: Python не обещает
постоянного времени операций с большими числами, поэтому подписывают им редко
и только на машине администратора; основной корень подписывает чип токена.

Константы «Стрибога» сгенерированы из текста RFC 6986 программой, не набраны
руками; параметры кривой -- RFC 7836 (id-tc26-gost-3410-2012-256-paramSetA).
Правильность -- эталонные примеры RFC 6986 и RFC 7091 в тестах.

Порядок байтов. RFC пишет векторы старшим разрядом вперёд; здесь, как во всех
байтовых реализациях, сообщение и хеш -- байты «младший первым»: хеш строки
`M` равен `bytes.fromhex(H)[::-1]` из RFC -- и совпадает с выводом OpenSSL
gost-engine. Подпись -- 64 байта s||r, каждое число 32 байта big-endian: так
её пишет OpenSSL gost-engine (сверено подписью из образа РЕД ОС, 02.10.2026).
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

PI = bytes((
    252, 238, 221, 17, 207, 110, 49, 22, 251, 196, 250, 218, 35, 197, 4, 77,
    233, 119, 240, 219, 147, 46, 153, 186, 23, 54, 241, 187, 20, 205, 95, 193,
    249, 24, 101, 90, 226, 92, 239, 33, 129, 28, 60, 66, 139, 1, 142, 79,
    5, 132, 2, 174, 227, 106, 143, 160, 6, 11, 237, 152, 127, 212, 211, 31,
    235, 52, 44, 81, 234, 200, 72, 171, 242, 42, 104, 162, 253, 58, 206, 204,
    181, 112, 14, 86, 8, 12, 118, 18, 191, 114, 19, 71, 156, 183, 93, 135,
    21, 161, 150, 41, 16, 123, 154, 199, 243, 145, 120, 111, 157, 158, 178, 177,
    50, 117, 25, 61, 255, 53, 138, 126, 109, 84, 198, 128, 195, 189, 13, 87,
    223, 245, 36, 169, 62, 168, 67, 201, 215, 121, 214, 246, 124, 34, 185, 3,
    224, 15, 236, 222, 122, 148, 176, 188, 220, 232, 40, 80, 78, 51, 10, 74,
    167, 151, 96, 115, 30, 0, 98, 68, 26, 184, 56, 130, 100, 159, 38, 65,
    173, 69, 70, 146, 39, 94, 85, 47, 140, 163, 165, 125, 105, 213, 149, 59,
    7, 88, 179, 64, 134, 172, 29, 247, 48, 55, 107, 228, 136, 217, 231, 137,
    225, 27, 131, 73, 76, 63, 248, 254, 141, 83, 170, 144, 202, 216, 133, 97,
    32, 113, 103, 164, 45, 43, 9, 91, 203, 155, 37, 208, 190, 229, 108, 82,
    89, 166, 116, 210, 230, 244, 180, 192, 209, 102, 175, 194, 57, 75, 99, 182,
))

TAU = (
    0, 8, 16, 24, 32, 40, 48, 56, 1, 9, 17, 25, 33, 41, 49, 57,
    2, 10, 18, 26, 34, 42, 50, 58, 3, 11, 19, 27, 35, 43, 51, 59,
    4, 12, 20, 28, 36, 44, 52, 60, 5, 13, 21, 29, 37, 45, 53, 61,
    6, 14, 22, 30, 38, 46, 54, 62, 7, 15, 23, 31, 39, 47, 55, 63,
)

A = (
    0x8e20faa72ba0b470, 0x47107ddd9b505a38, 0xad08b0e0c3282d1c, 0xd8045870ef14980e,
    0x6c022c38f90a4c07, 0x3601161cf205268d, 0x1b8e0b0e798c13c8, 0x83478b07b2468764,
    0xa011d380818e8f40, 0x5086e740ce47c920, 0x2843fd2067adea10, 0x14aff010bdd87508,
    0x0ad97808d06cb404, 0x05e23c0468365a02, 0x8c711e02341b2d01, 0x46b60f011a83988e,
    0x90dab52a387ae76f, 0x486dd4151c3dfdb9, 0x24b86a840e90f0d2, 0x125c354207487869,
    0x092e94218d243cba, 0x8a174a9ec8121e5d, 0x4585254f64090fa0, 0xaccc9ca9328a8950,
    0x9d4df05d5f661451, 0xc0a878a0a1330aa6, 0x60543c50de970553, 0x302a1e286fc58ca7,
    0x18150f14b9ec46dd, 0x0c84890ad27623e0, 0x0642ca05693b9f70, 0x0321658cba93c138,
    0x86275df09ce8aaa8, 0x439da0784e745554, 0xafc0503c273aa42a, 0xd960281e9d1d5215,
    0xe230140fc0802984, 0x71180a8960409a42, 0xb60c05ca30204d21, 0x5b068c651810a89e,
    0x456c34887a3805b9, 0xac361a443d1c8cd2, 0x561b0d22900e4669, 0x2b838811480723ba,
    0x9bcf4486248d9f5d, 0xc3e9224312c8c1a0, 0xeffa11af0964ee50, 0xf97d86d98a327728,
    0xe4fa2054a80b329c, 0x727d102a548b194e, 0x39b008152acb8227, 0x9258048415eb419d,
    0x492c024284fbaec0, 0xaa16012142f35760, 0x550b8e9e21f7a530, 0xa48b474f9ef5dc18,
    0x70a6a56e2440598e, 0x3853dc371220a247, 0x1ca76e95091051ad, 0x0edd37c48a08a6d8,
    0x07e095624504536c, 0x8d70c431ac02a736, 0xc83862965601dd1b, 0x641c314b2b8ee083,
)

C_HEX = (
    "b1085bda1ecadae9ebcb2f81c0657c1f2f6a76432e45d016714eb88d7585c4fc4b7ce09192676901a2422a08a460d31505767436cc744d23dd806559f2a64507",
    "6fa3b58aa99d2f1a4fe39d460f70b5d7f3feea720a232b9861d55e0f16b501319ab5176b12d699585cb561c2db0aa7ca55dda21bd7cbcd56e679047021b19bb7",
    "f574dcac2bce2fc70a39fc286a3d843506f15e5f529c1f8bf2ea7514b1297b7bd3e20fe490359eb1c1c93a376062db09c2b6f443867adb31991e96f50aba0ab2",
    "ef1fdfb3e81566d2f948e1a05d71e4dd488e857e335c3c7d9d721cad685e353fa9d72c82ed03d675d8b71333935203be3453eaa193e837f1220cbebc84e3d12e",
    "4bea6bacad4747999a3f410c6ca923637f151c1f1686104a359e35d7800fffbdbfcd1747253af5a3dfff00b723271a167a56a27ea9ea63f5601758fd7c6cfe57",
    "ae4faeae1d3ad3d96fa4c33b7a3039c02d66c4f95142a46c187f9ab49af08ec6cffaa6b71c9ab7b40af21f66c2bec6b6bf71c57236904f35fa68407a46647d6e",
    "f4c70e16eeaac5ec51ac86febf240954399ec6c7e6bf87c9d3473e33197a93c90992abc52d822c3706476983284a05043517454ca23c4af38886564d3a14d493",
    "9b1f5b424d93c9a703e7aa020c6e41414eb7f8719c36de1e89b4443b4ddbc49af4892bcb929b069069d18d2bd1a5c42f36acc2355951a8d9a47f0dd4bf02e71e",
    "378f5a541631229b944c9ad8ec165fde3a7d3a1b258942243cd955b7e00d0984800a440bdbb2ceb17b2b8a9aa6079c540e38dc92cb1f2a607261445183235adb",
    "abbedea680056f52382ae548b2e4f3f38941e71cff8a78db1fffe18a1b3361039fe76702af69334b7a1e6c303b7652f43698fad1153bb6c374b4c7fb98459ced",
    "7bcd9ed0efc889fb3002c6cd635afe94d8fa6bbbebab076120018021148466798a1d71efea48b9caefbacd1d7d476e98dea2594ac06fd85d6bcaa4cd81f32d1b",
    "378ee767f11631bad21380b00449b17acda43c32bcdf1d77f82012d430219f9b5d80ef9d1891cc86e71da4aa88e12852faf417d5d9b21b9948bc924af11bd720",
)

_C = tuple(bytes.fromhex(h)[::-1] for h in C_HEX)


def _xor(a: bytes, b: bytes) -> bytes:
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(64, "little")


def _l(word: int) -> int:
    out = 0
    for i in range(64):
        if (word >> i) & 1:
            out ^= A[63 - i]
    return out


def _lps(block: bytes) -> bytes:
    s = bytes(PI[b] for b in block)
    p = bytes(s[TAU[i]] for i in range(64))
    return b"".join(_l(int.from_bytes(p[i:i + 8], "little")).to_bytes(8, "little")
                    for i in range(0, 64, 8))


def _g(n: bytes, h: bytes, m: bytes) -> bytes:
    k = _lps(_xor(h, n))
    t = m
    for i in range(12):
        t = _lps(_xor(k, t))
        k = _lps(_xor(k, _C[i]))
    return _xor(_xor(_xor(k, t), h), m)


def _add512(a: int, b: int) -> int:
    return (a + b) % (1 << 512)


def streebog(data: bytes, size: int = 256) -> bytes:
    """Хеш ГОСТ Р 34.11-2012, 256 или 512 бит, байты «младший первым»."""
    if size not in (256, 512):
        raise ValueError("размер хеша -- 256 или 512")
    h = (b"\x01" if size == 256 else b"\x00") * 64
    n = 0
    sigma = 0
    zero = bytes(64)
    full = len(data) // 64 * 64
    for i in range(0, full, 64):
        m = data[i:i + 64]
        h = _g(n.to_bytes(64, "little"), h, m)
        n = _add512(n, 512)
        sigma = _add512(sigma, int.from_bytes(m, "little"))
    rest = data[full:]
    m = rest + b"\x01" + bytes(63 - len(rest))
    h = _g(n.to_bytes(64, "little"), h, m)
    n = _add512(n, 8 * len(rest))
    sigma = _add512(sigma, int.from_bytes(m, "little"))
    h = _g(zero, h, n.to_bytes(64, "little"))
    h = _g(zero, h, sigma.to_bytes(64, "little"))
    return h[32:] if size == 256 else h


# -- ГОСТ Р 34.10-2012 ----------------------------------------------------------
@dataclass(frozen=True)
class Curve:
    name: str
    p: int
    a: int
    b: int
    q: int
    x: int
    y: int


#: RFC 7836, приложение A.2 (форма Вейерштрасса набора).
TC26_256_A = Curve(
    "id-tc26-gost-3410-2012-256-paramSetA",
    p=0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFD97,
    a=0xC2173F1513981673AF4892C23035A27CE25E2013BF95AA33B22C656F277E7335,
    b=0x295F9BAE7428ED9CCC20E7C359A9D41A22FCCD9108E17BF7BA9337A6F8AE9513,
    q=0x400000000000000000000000000000000FD8CDDFC87B6635C115AF556C360C67,
    x=0x91E38443A5E82C0D880923425712B2BB658B9196932E02C78B2582FE742DAA28,
    y=0x32879423AB1A0375895786C4BB46E9565FDE0B5344766740AF268ADB32322E5C,
)

CURVES = {TC26_256_A.name: TC26_256_A}


def _add(c: Curve, p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2:
        if (y1 + y2) % c.p == 0:
            return None
        lam = (3 * x1 * x1 + c.a) * pow(2 * y1, -1, c.p) % c.p
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, c.p) % c.p
    x3 = (lam * lam - x1 - x2) % c.p
    return x3, (lam * (x1 - x3) - y1) % c.p


def _mul(c: Curve, k: int, point):
    result = None
    while k:
        if k & 1:
            result = _add(c, result, point)
        point = _add(c, point, point)
        k >>= 1
    return result


def on_curve(c: Curve, point) -> bool:
    x, y = point
    return 0 <= x < c.p and 0 <= y < c.p and (y * y - x * x * x - c.a * x - c.b) % c.p == 0


def public_key(c: Curve, d: int):
    return _mul(c, d, (c.x, c.y))


def new_private_key(c: Curve) -> int:
    """Ключ из системного генератора (getrandom): повторить его можно только
    перебором, производного от пароля или времени здесь нет."""
    return secrets.randbelow(c.q - 1) + 1


def _e(c: Curve, digest: bytes) -> int:
    e = int.from_bytes(digest, "little") % c.q
    return e or 1


def sign_digest(c: Curve, d: int, digest: bytes, k: int | None = None) -> bytes:
    e = _e(c, digest)
    while True:
        kk = k if k is not None else secrets.randbelow(c.q - 1) + 1
        r = _mul(c, kk, (c.x, c.y))[0] % c.q
        s = (r * d + kk * e) % c.q
        if r and s:
            return s.to_bytes(32, "big") + r.to_bytes(32, "big")
        if k is not None:
            raise ValueError("k не годится")


def verify_digest(c: Curve, public, digest: bytes, signature: bytes) -> bool:
    if len(signature) != 64 or not on_curve(c, public):
        return False
    s = int.from_bytes(signature[:32], "big")
    r = int.from_bytes(signature[32:], "big")
    if not (0 < r < c.q and 0 < s < c.q):
        return False
    v = pow(_e(c, digest), -1, c.q)
    z1, z2 = s * v % c.q, -r * v % c.q
    point = _add(c, _mul(c, z1, (c.x, c.y)), _mul(c, z2, public))
    return point is not None and point[0] % c.q == r


def sign(c: Curve, d: int, data: bytes) -> bytes:
    return sign_digest(c, d, streebog(data, 256))


def verify(c: Curve, public, data: bytes, signature: bytes) -> bool:
    return verify_digest(c, public, streebog(data, 256), signature)


def encode_public(public) -> bytes:
    """64 байта: x||y, каждое big-endian."""
    return public[0].to_bytes(32, "big") + public[1].to_bytes(32, "big")


def decode_public(raw: bytes):
    if len(raw) != 64:
        raise ValueError("открытый ключ ГОСТ -- 64 байта")
    return int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
