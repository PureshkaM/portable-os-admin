"""Administrator-side recovery flow (UC-7 "Выдача кода восстановления доступа").

The counterpart that runs on the portable medium itself is the reference
shell script in recovery/recovery_module.sh (see README, раздел «Архитектура» for how
the two sides fit together).
"""
from __future__ import annotations

from ..container import AdminContainer, RecoveryRecord
from ..errors import MediaNotRegisteredError, RecoveryDeniedError
from ..session import AdminSession
from .request import RecoveryRequest


class RecoveryService:
    def __init__(self, session: AdminSession):
        self.session = session

    @property
    def container(self) -> AdminContainer:
        return self.session.container

    def issue_code(self, request_text: str) -> str:
        """Implements the normal + exception flows of UC-7:

        - request_text fails to parse/checksum -> RecoveryRequestInvalidError
          (raised by RecoveryRequest.parse before we ever touch the container)
        - no matching media record -> MediaNotRegisteredError
          ("1.0.И.2 Сведения о переносной системе не найдены")
        - no matching / already-used recovery record -> RecoveryDeniedError
          ("1.0.И.3 Recovery-запись недоступна или уже использована")

        On success the record is immediately marked "used"
        ("Восстановление.Код.Фиксация") and the code is returned for the
        administrator to relay to the user."""
        self.session.require_open()
        request = RecoveryRequest.parse(request_text)

        media = self.container.find_media(request.media_id)
        if media is None:
            raise MediaNotRegisteredError(
                f"сведения о переносной системе не найдены (media_id={request.media_id})"
            )

        record = self.container.find_recovery(request.media_id, request.slot)
        if record is None or record.status != "available":
            raise RecoveryDeniedError("recovery-запись недоступна или уже использована")

        record.status = "used"
        from datetime import datetime, timezone

        record.used_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return record.code

    def register_recovery_record(self, record: RecoveryRecord) -> None:
        """Used by the installer right after a successful install
        ("Установка.Восстановление.Данные" / "Установка.Контейнер.Сохранение")."""
        self.session.require_open()
        self.container.add_recovery_record(record)
