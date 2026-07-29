"""Recovery-request encoding/parsing.

Per "Интерфейсы передачи информации-3", the request and code travel between
user and administrator as plain text values (read aloud, typed, messaged --
out of band, outside the system). The wire format here is deliberately a
short, easy-to-read-aloud string rather than a packed binary blob:

    "<media_id>:<recovery_slot>:<checksum>"

`checksum` guards against transcription mistakes (a wrong digit produces a
mismatch that is caught before the container is even searched, corresponding
to "1.0.И.1 Запрос восстановления некорректен").
"""
from __future__ import annotations

from dataclasses import dataclass

from .. import crypto
from ..errors import RecoveryRequestInvalidError


@dataclass(frozen=True)
class RecoveryRequest:
    media_id: str
    slot: int

    def checksum(self) -> str:
        return crypto.short_checksum(self.media_id, str(self.slot))

    def encode(self) -> str:
        return f"{self.media_id}:{self.slot}:{self.checksum()}"

    @classmethod
    def parse(cls, text: str) -> "RecoveryRequest":
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
