"""Data entities stored inside the encrypted admin container.

Maps to course-project § "Структура данных системы": "Запись носителя" and
"Recovery-запись" (table 6).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MediaRecord:
    media_id: str
    luks_uuid: str
    label: str
    config_name: str
    target_os: str
    created_at: str = field(default_factory=_now_iso)
    #: Base64 of the key that opens this medium's log and exchange volumes.
    #: It lives here, inside the encrypted administrator container, and
    #: nowhere else on the administrator's machine -- losing the container
    #: means those two volumes can no longer be read from outside the portable
    #: system, which is the intended property, not an accident.
    service_key_b64: str = ""
    #: LUKS container UUIDs of those two volumes, so they can be found on a
    #: medium without trusting device names, which reorder between ports.
    logs_luks_uuid: str = ""
    exchange_luks_uuid: str = ""

    @property
    def service_key(self) -> bytes:
        import base64

        return base64.b64decode(self.service_key_b64) if self.service_key_b64 else b""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MediaRecord":
        return cls(**data)


@dataclass
class RecoveryRecord:
    media_id: str
    slot: int
    code: str
    salt_b64: str
    status: str = "available"  # "available" | "used"
    created_at: str = field(default_factory=_now_iso)
    used_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RecoveryRecord":
        return cls(**data)


@dataclass
class AdminContainerData:
    media_records: list[MediaRecord] = field(default_factory=list)
    recovery_records: list[RecoveryRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "media_records": [m.to_dict() for m in self.media_records],
            "recovery_records": [r.to_dict() for r in self.recovery_records],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AdminContainerData":
        return cls(
            media_records=[MediaRecord.from_dict(m) for m in data.get("media_records", [])],
            recovery_records=[RecoveryRecord.from_dict(r) for r in data.get("recovery_records", [])],
        )
