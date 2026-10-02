"""QR-код без зависимостей: побайтовый режим, коррекция M, версии 1--10.

Нужен окну активации: семя TOTP показывается один раз QR-кодом на экране
самого носителя. `qrencode` есть только в образе РЕД ОС, а модуль одинаков для
обоих семейств, поэтому кодировщик свой. Устройство -- по стандарту ISO/IEC
18004 в изложении генератора Nayuki (код Рида -- Соломона над GF(256),
чередование блоков, восемь масок со штрафами). Проверяется декодером zbar в
тестах (`tests/test_qr.py`).

Строка otpauth с семенем в 32 знака -- около 130 байт, это версия 7--8.
"""
from __future__ import annotations

#: (всего кодовых слов, слов коррекции на блок, [(число блоков, слов данных)])
#: для уровня M, версии 1--10 (ISO/IEC 18004, таблица 9).
_BLOCKS = {
    1: (26, 10, [(1, 16)]), 2: (44, 16, [(1, 28)]), 3: (70, 26, [(1, 44)]),
    4: (100, 18, [(2, 32)]), 5: (134, 24, [(2, 43)]), 6: (172, 16, [(4, 27)]),
    7: (196, 18, [(4, 31)]), 8: (242, 22, [(2, 38), (2, 39)]),
    9: (292, 22, [(3, 36), (2, 37)]), 10: (346, 26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
_ECC_M_BITS = 0  # биты уровня M в формате -- 00


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z & 0xFF


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _data_capacity(version: int) -> int:
    return sum(n * k for n, k in _BLOCKS[version][2])


def _choose_version(length: int) -> int:
    for version in range(1, 11):
        header = 4 + (8 if version < 10 else 16)
        if header + 8 * length <= 8 * _data_capacity(version):
            return version
    raise ValueError("слишком длинные данные для QR версии 10")


def _codewords(data: bytes, version: int) -> list[int]:
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)                                   # побайтовый режим
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    capacity = 8 * _data_capacity(version)
    put(0, min(4, capacity - len(bits)))             # терминатор
    put(0, (-len(bits)) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    _, ec_len, groups = _BLOCKS[version]
    divisor = _rs_divisor(ec_len)
    blocks, offset = [], 0
    for count, size in groups:
        for _ in range(count):
            chunk = words[offset:offset + size]
            offset += size
            blocks.append((chunk, _rs_remainder(chunk, divisor)))
    result = []
    for i in range(max(len(b[0]) for b in blocks)):
        result.extend(b[0][i] for b in blocks if i < len(b[0]))
    for i in range(ec_len):
        result.extend(b[1][i] for b in blocks)
    return result


class _Matrix:
    def __init__(self, version: int) -> None:
        self.version = version
        self.size = 4 * version + 17
        self.modules = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]

    def set(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self.function[y][x] = True

    def draw_functions(self) -> None:
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self.set(x, y, dist not in (2, 4))
        positions = _ALIGN[self.version]
        last = len(positions) - 1
        for i, ax in enumerate(positions):
            for j, ay in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        self.draw_version()

    def draw_format(self, mask: int) -> None:
        data = (_ECC_M_BITS << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        size = self.size
        for i in range(6):
            self.set(8, i, (bits >> i) & 1 == 1)
        self.set(8, 7, (bits >> 6) & 1 == 1)
        self.set(8, 8, (bits >> 7) & 1 == 1)
        self.set(7, 8, (bits >> 8) & 1 == 1)
        for i in range(9, 15):
            self.set(14 - i, 8, (bits >> i) & 1 == 1)
        for i in range(8):
            self.set(size - 1 - i, 8, (bits >> i) & 1 == 1)
        for i in range(8, 15):
            self.set(8, size - 15 + i, (bits >> i) & 1 == 1)
        self.set(8, size - 8, True)                  # тёмный модуль

    def draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.version << 12) | rem
        for i in range(18):
            dark = (bits >> i) & 1 == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.set(a, b, dark)
            self.set(b, a, dark)

    def draw_codewords(self, data: list[int]) -> None:
        i = 0
        size = self.size
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.function[y][x] and i < len(data) * 8:
                        self.modules[y][x] = (data[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.function[y][x]:
                    continue
                invert = (
                    (x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                    (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
                    (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0,
                )[mask]
                if invert:
                    self.modules[y][x] = not self.modules[y][x]

    def penalty(self) -> int:
        m, size, score = self.modules, self.size, 0
        lines = [m[y] for y in range(size)] + [[m[y][x] for y in range(size)] for x in range(size)]
        for line in lines:
            run, prev = 0, None
            for cell in line:
                if cell == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, cell
            if run >= 5:
                score += run - 2
            text = "".join("1" if c else "0" for c in line)
            for pattern in ("10111010000", "00001011101"):
                score += 40 * text.count(pattern)
        for y in range(size - 1):
            for x in range(size - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in m)
        total = size * size
        score += (abs(dark * 20 - total * 10) + total - 1) // total * 10
        return score


def encode(text: str | bytes) -> list[list[bool]]:
    """Матрица QR (True -- тёмный модуль), без тихой зоны."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = _choose_version(len(data))
    words = _codewords(data, version)
    best, best_score = None, None
    for mask in range(8):
        matrix = _Matrix(version)
        matrix.draw_functions()
        matrix.draw_codewords(words)
        matrix.apply_mask(mask)
        matrix.draw_format(mask)
        score = matrix.penalty()
        if best_score is None or score < best_score:
            best, best_score = matrix, score
    return best.modules


def to_pbm(modules: list[list[bool]], scale: int = 8, border: int = 4) -> bytes:
    """Изображение PBM (P1) -- для тестов и отладки."""
    size = len(modules) + 2 * border
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            inside = border <= x < size - border and border <= y < size - border
            dark = inside and modules[y - border][x - border]
            row.extend(["1" if dark else "0"] * scale)
        rows.extend([" ".join(row)] * scale)
    return f"P1\n{size * scale} {size * scale}\n".encode() + "\n".join(rows).encode() + b"\n"
