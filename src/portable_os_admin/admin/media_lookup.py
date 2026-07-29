"""Matches a connected medium to a MediaRecord in the admin container.

Implements "Журналы.Носитель.Сопоставление" / "Обмен.Носитель.Сопоставление":
log and exchange access are only granted for media the container already
knows about. The boot/ESP partition carries a small `poa/media_id.txt`
metadata file written at install time (see install/installer.py,
`_install_boot_environment`) that lets us identify a medium without touching
its LUKS-encrypted system partition at all.
"""
from __future__ import annotations

from pathlib import Path

from ..container import AdminContainer, MediaRecord
from ..errors import MediaNotRegisteredError

MEDIA_ID_RELATIVE_PATH = "poa/media_id.txt"


def read_media_id(boot_mount_point: str) -> str:
    media_id_path = Path(boot_mount_point) / MEDIA_ID_RELATIVE_PATH
    if not media_id_path.exists():
        raise MediaNotRegisteredError(
            f"на носителе отсутствуют метаданные ({media_id_path}); это не переносная система, "
            "созданная этим приложением"
        )
    return media_id_path.read_text(encoding="utf-8").strip()

def find_media_by_boot_metadata(container: AdminContainer, boot_mount_point: str) -> MediaRecord:
    media_id = read_media_id(boot_mount_point)
    record = container.find_media(media_id)
    if record is None:
        raise MediaNotRegisteredError(f"сведения о носителе не найдены в контейнере (media_id={media_id})")
    return record
