"""Запрос, который пользователь диктует администратору.

Запрос и код идут между людьми: их читают вслух по телефону, пишут в
мессенджер, переписывают с экрана на бумажку. Отсюда всё устройство этого
модуля.

Формат -- одна строка из трёх групп по четыре знака:

    3F7K-9QAB-M2XZ

Внутри упакованы `media_id`, вид случившегося (забыт пароль или носитель
исчерпал машины) и номер recovery-слота, которым носитель предлагает
воспользоваться. Пользователю знать про слоты незачем -- он диктует строку и
получает строку, а разбирается в этом программа с обеих сторон.

Почему не прежние `media_id:слот:контрольная_сумма`
--------------------------------------------------

Прежний формат был вдвое длиннее (`66640908:3:a1b2c3d4`) и показывал человеку
кухню, которая ему не нужна. Хуже того, в нём не было места для вида
операции: сброс машин и забытый пароль -- разные события, а строка выглядела
одинаково, и администратор не мог сказать, что у него просят.

Разбор прежнего формата сохранён: носители с ним уже собраны, и они будут
приходить за восстановлением ещё долго.

Алфавит
-------

Crockford base32. Он придуман ровно для строк, которые люди диктуют и
переписывают, и решает проблему похожих знаков не выкидыванием букв, а
разбором: `O` принимается как ноль, `I` и `L` -- как единица. Написавший на
бумажке «O» вместо «0» получит то же значение, а не отказ.

Взять алфавит кодов восстановления не вышло: он содержит `L` и `U`, хотя
комментарий рядом с ним утверждает обратное, -- то есть от `1` его `L` ничем
не отличается. Здесь это стоило бы отказа на верном коде.

Контрольная сумма
-----------------

Двадцать два бита из двадцати четырёх, отданных под неё в шестидесяти битах
строки. Ловит опечатку до того, как программа полезет в контейнер, -- это
«1.0.И.1 Запрос восстановления некорректен».
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .. import crypto
from ..errors import RecoveryRequestInvalidError

#: Забыт пароль -- обычное восстановление.
KIND_PASSWORD = "password"
#: Носитель исчерпал машины, и его надо вернуть в исходное состояние.
KIND_MACHINES = "machines"

#: Сколько бит на что уходит в упакованном значении.
_MEDIA_ID_BITS = 32
_KIND_BITS = 1
_SLOT_BITS = 5
_CHECKSUM_BITS = 22
_TOTAL_BITS = _MEDIA_ID_BITS + _KIND_BITS + _SLOT_BITS + _CHECKSUM_BITS  # 60

#: Crockford base32: цифры и латиница без `I`, `L`, `O`, `U`. Последняя убрана
#: не ради читаемости, а чтобы случайное слово не оказалось бранным.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: Что человек мог написать вместо чего. Читается «принимаем и так»: буква,
#: которой в алфавите нет, но которую от цифры на глаз не отличить.
_LOOKALIKES = {"O": "0", "I": "1", "L": "1"}
_GROUP_SIZE = 4
#: 60 бит по пять на знак -- двенадцать знаков, ровно три группы.
_LENGTH = _TOTAL_BITS // 5


def _encode_base32(value: int) -> str:
    """Число в строку алфавита, старшими разрядами вперёд."""
    chars = []
    for shift in range(_TOTAL_BITS - 5, -1, -5):
        chars.append(_ALPHABET[(value >> shift) & 0x1F])
    return "-".join(
        "".join(chars[i:i + _GROUP_SIZE]) for i in range(0, len(chars), _GROUP_SIZE)
    )


def _decode_base32(text: str) -> int:
    """Строка обратно в число. Дефисы, пробелы и регистр значения не имеют.

    Человек, переписывающий код с экрана, ставит пробелы где придётся и не
    следит за регистром; отвергать за это было бы придиркой, а не проверкой.
    """
    cleaned = text.strip().upper().replace("-", "").replace(" ", "")
    if len(cleaned) != _LENGTH:
        raise RecoveryRequestInvalidError(
            f"запрос должен состоять из {_LENGTH} знаков (три группы по четыре)"
        )
    value = 0
    for char in cleaned:
        char = _LOOKALIKES.get(char, char)
        index = _ALPHABET.find(char)
        if index < 0:
            raise RecoveryRequestInvalidError(f"недопустимый знак в запросе: «{char}»")
        value = (value << 5) | index
    return value


def _checksum_of(payload: int) -> int:
    digest = hashlib.sha256(payload.to_bytes(8, "big")).digest()
    return int.from_bytes(digest[:3], "big") >> (24 - _CHECKSUM_BITS)


@dataclass(frozen=True)
class RecoveryRequest:
    media_id: str
    slot: int
    #: Что случилось: забыт пароль или носитель исчерпал машины. Вид входит в
    #: сам запрос, иначе администратор не знает, что у него просят, а носитель
    #: не знает, что делать после того, как код принят.
    kind: str = KIND_PASSWORD

    def __post_init__(self) -> None:
        if self.kind not in (KIND_PASSWORD, KIND_MACHINES):
            raise RecoveryRequestInvalidError(f"неизвестный вид запроса: {self.kind}")

    def checksum(self) -> str:
        """Контрольная сумма прежнего формата -- только для него и осталась."""
        return crypto.short_checksum(self.media_id, str(self.slot))

    def _payload(self) -> int:
        # Границы проверяются здесь, а не при разборе: строка фиксированной
        # длины вмещает ровно восемь шестнадцатеричных знаков media_id и слот
        # от 0 до 31 -- столько их и есть в заголовке LUKS2. Молча обрезать
        # значение значило бы выдать пользователю код от чужого носителя.
        try:
            media = int(self.media_id, 16)
        except ValueError:
            raise RecoveryRequestInvalidError(
                f"идентификатор носителя должен быть шестнадцатеричным: «{self.media_id}»"
            ) from None
        if media >> _MEDIA_ID_BITS:
            raise RecoveryRequestInvalidError(
                f"идентификатор носителя длиннее восьми знаков: «{self.media_id}»"
            )
        if not 0 <= self.slot < (1 << _SLOT_BITS):
            raise RecoveryRequestInvalidError(
                f"номер слота вне заголовка LUKS2 (0..31): {self.slot}"
            )
        kind_bit = 1 if self.kind == KIND_MACHINES else 0
        return (media << (_KIND_BITS + _SLOT_BITS)) | (kind_bit << _SLOT_BITS) | self.slot

    def encode(self) -> str:
        """Строка, которую пользователь читает администратору."""
        payload = self._payload()
        return _encode_base32((payload << _CHECKSUM_BITS) | _checksum_of(payload))

    @classmethod
    def parse(cls, text: str) -> "RecoveryRequest":
        # Прежний формат узнаётся по двоеточиям и разбирается как раньше:
        # носители с ним уже в работе.
        if ":" in text:
            return cls._parse_legacy(text)

        value = _decode_base32(text)
        payload = value >> _CHECKSUM_BITS
        if _checksum_of(payload) != (value & ((1 << _CHECKSUM_BITS) - 1)):
            raise RecoveryRequestInvalidError(
                "контрольная сумма запроса не совпадает — проверьте, верно ли записан код"
            )
        slot = payload & ((1 << _SLOT_BITS) - 1)
        kind_bit = (payload >> _SLOT_BITS) & 1
        media = payload >> (_KIND_BITS + _SLOT_BITS)
        return cls(
            media_id=f"{media:08x}",
            slot=slot,
            kind=KIND_MACHINES if kind_bit else KIND_PASSWORD,
        )

    @classmethod
    def table(cls, media_id: str, slots) -> str:
        """Готовые запросы на все recovery-слоты, обоих видов.

        Кладётся на носитель при развёртывании, чтобы загрузочная среда ничего
        не вычисляла. Считать base32 и sha256 в shell пришлось бы во второй
        раз -- первый уже здесь, -- и две реализации одного кодирования
        разошлись бы при первой же правке, причём молча: пользователь диктовал
        бы строку, которую администратор не может разобрать, и выяснилось бы
        это на живой загрузке у человека, оставшегося без системы.

        Формат строки: `<слот> <вид> <запрос>`. Таблица на 29 слотов занимает
        около килобайта -- дешевле любой отладки.
        """
        lines = ["# generated by portable-os-admin"]
        for slot in slots:
            for kind in (KIND_PASSWORD, KIND_MACHINES):
                lines.append(f"{slot} {kind} {cls(media_id, slot, kind).encode()}")
        return "\n".join(lines) + "\n"

    @classmethod
    def _parse_legacy(cls, text: str) -> "RecoveryRequest":
        parts = text.strip().split(":")
        if len(parts) != 3:
            raise RecoveryRequestInvalidError(
                "неверный формат запроса восстановления (ожидается media_id:слот:контрольная_сумма)"
            )
        media_id, slot_str, checksum = parts
        if not slot_str.isdigit():
            raise RecoveryRequestInvalidError("номер recovery-слота должен быть числом")
        request = cls(media_id=media_id, slot=int(slot_str))
        if request.checksum() != checksum:
            raise RecoveryRequestInvalidError("контрольная сумма запроса восстановления не совпадает")
        return request
