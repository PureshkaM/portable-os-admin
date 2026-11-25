"""Ручное обновление носителя (LIVE-2.0, Э9): выпуск -- в неактивный слот.

То же, что носитель делает сам по сети (`poa_update.py`), только файлы берутся
из каталога дистрибутива на машине администратора. LUKS не открывается: образы,
ядра и `grubenv` лежат на незашифрованных разделах, секретов в них нет.

Порядок тот же, что у обновления по сети, и по той же причине -- носитель,
выдернутый на середине, должен загружаться в прежнюю систему:

1. выпуск проверен до записи: подпись (или, без корня организации, подпись
   дистрибутива этим контейнером), то же семейство, архитектура и разметка,
   номер новее работающего;
2. если неактивный слот уже предложен GRUB (`poa_try`), предложение снимается
   -- слот сейчас перестанет быть целым;
3. файлы пишутся с суффиксом `.part`, sha256 считается по ходу и сверяется с
   выпуском; на места встают только все вместе;
4. последним -- `poa_try=<слот>`, `poa_tries=3`. Не подтвердится новая система
   (`poa-boot-ok`) -- GRUB вернётся к прежней.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import Callable

from ..errors import PortableOSAdminError
from ..install import distribution, grubenv, overlay
from ..install.access.runtime import poa_trust
from ..install.backends.constants import BOOT_TRIES, GRUB_ENV_DIR

#: Файл дистрибутива -> (раздел, имя в слоте, ключ в release.json).
SLOT_FILES = (
    ("images", overlay.SLOT_IMAGE, "image"),
    ("images", overlay.SLOT_VERITY, "verity"),
    ("boot", "vmlinuz", "kernel"),
    ("boot", "initrd.img", "initrd"),
)
_CHUNK = 4 * 1024 * 1024


class MediaUpdateError(PortableOSAdminError):
    pass


def load_release(directory: str, anchors: list[dict], admin_secret: bytes) -> dict:
    """Выпуск дистрибутива после проверки -- payload `release.json`.

    С корнем организации выпуск обязан быть подписан: так же его проверит
    носитель после загрузки (Э5). Без корня подписывать выпуски нечем, и
    доверие замыкается на подписи дистрибутива этим же контейнером.
    """
    try:
        with open(os.path.join(directory, distribution.RELEASE_NAME), encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        raise MediaUpdateError(
            f"в каталоге нет выпуска ({distribution.RELEASE_NAME}): дистрибутив собран "
            f"до слотов A/B или каталог не тот. {exc}"
        ) from exc
    if anchors:
        try:
            return poa_trust.verify_document(anchors, document, "release")
        except poa_trust.TrustError as exc:
            raise MediaUpdateError(f"выпуск не прошёл проверку подписи: {exc}") from exc
    metadata = distribution.read_metadata(directory)
    verdict = distribution.verify_distribution(directory, admin_secret, expected=metadata)
    if verdict.status != distribution.SIGNED_BY_THIS_CONTAINER:
        raise MediaUpdateError("дистрибутив не подписан этим контейнером, а корня организации нет")
    payload = document.get("release") or {}
    if (payload.get("employee_profile") != metadata.employee_profile
            or payload.get("verity_root_hash") != metadata.verity_root_hash
            or (payload.get("image") or {}).get("sha256") != metadata.sha256):
        raise MediaUpdateError("выпуск не совпадает с подписанным описанием дистрибутива")
    return payload


def _read_release(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["release"]


def plan(esp: str, boot: str, payload: dict) -> dict:
    """Куда писать и почему можно: слот, текущий выпуск, переменные GRUB."""
    env_path = os.path.join(esp, GRUB_ENV_DIR, "grubenv")
    try:
        env = grubenv.read(env_path)
    except (OSError, grubenv.GrubEnvError) as exc:
        raise MediaUpdateError(
            f"на носителе нет переменных загрузчика ({exc}): он записан до слотов A/B "
            "и обновляется только перезаписью"
        ) from exc
    good = env.get("poa_good") or "a"
    if good not in ("a", "b"):
        raise MediaUpdateError(f"poa_good={good!r} -- не слот")
    if payload.get("employee_profile") != poa_trust.EMPLOYEE_PROFILE:
        raise MediaUpdateError("Нужен заново собранный графический выпуск без SSH.")
    target = {"a": "b", "b": "a"}[good]
    try:
        current = _read_release(os.path.join(boot, f"slot-{good}", "release.json"))
    except (OSError, ValueError, KeyError) as exc:
        raise MediaUpdateError(f"у рабочего слота {good} нет выпуска: {exc}") from exc
    for field in ("family", "arch", "layout"):
        if payload.get(field) != current.get(field):
            raise MediaUpdateError(
                f"выпуск несовместим с носителем: {field} {payload.get(field)!r}, "
                f"а на носителе {current.get(field)!r}"
            )
    if int(payload.get("serial", 0)) <= int(current.get("serial", 0)):
        raise MediaUpdateError(
            f"выпуск {payload.get('serial')} не новее того, что на носителе "
            f"({current.get('serial')}): откат носитель всё равно не загрузит"
        )
    return {"env": env_path, "good": good, "target": target, "current": current}


def _copy(source: str, dest: str, done: int, total: int, progress) -> str:
    digest = hashlib.sha256()
    with open(source, "rb") as src, open(dest, "wb") as out:
        for chunk in iter(lambda: src.read(_CHUNK), b""):
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            progress(done, total)
        out.flush()
        os.fsync(out.fileno())
    return digest.hexdigest()


def write_slot(directory: str, payload: dict, esp: str, boot: str, images: str,
               progress: Callable[[int, int], None] = lambda done, total: None) -> dict:
    """Записать проверенный выпуск в неактивный слот смонтированного носителя."""
    where = plan(esp, boot, payload)
    target = where["target"]
    dirs = {"boot": os.path.join(boot, f"slot-{target}"),
            "images": os.path.join(images, f"slot-{target}")}
    sources = {key: os.path.join(directory, payload[key]["file"]) for _p, _n, key in SLOT_FILES}
    total = sum(int(payload[key]["size"]) for _p, _n, key in SLOT_FILES)
    for key, path in sources.items():
        if not os.path.isfile(path) or os.path.getsize(path) != int(payload[key]["size"]):
            raise MediaUpdateError(f"{os.path.basename(path)}: нет файла или размер не тот, что в выпуске")

    # Место -- до того, как старый слот удалён: отказ не должен стоить запасной системы.
    need = sum(int(payload[key]["size"]) for part, _n, key in SLOT_FILES if part == "images")
    freed = sum(f.stat().st_size for f in os.scandir(dirs["images"]) if f.is_file()) \
        if os.path.isdir(dirs["images"]) else 0
    free = shutil.disk_usage(images).free + freed
    if free < need:
        raise MediaUpdateError(
            f"на разделе образов не хватает места: нужно {need // 2**20} МБ, "
            f"будет свободно {free // 2**20} МБ"
        )
    # Снять предложение до того, как слот перестанет быть целым.
    env = grubenv.read(where["env"])
    if env.get("poa_try") == target:
        grubenv.update(where["env"], unset=("poa_try", "poa_tries"))
    # Старое содержимое слота -- прочь: место нужно новому, а полуслот с
    # прежним release.json выглядел бы целым.
    for part in ("boot", "images"):
        shutil.rmtree(dirs[part], ignore_errors=True)
        os.makedirs(dirs[part])

    done = 0
    for part, name, key in SLOT_FILES:
        dest = os.path.join(dirs[part], name + ".part")
        if _copy(sources[key], dest, done, total, progress) != payload[key]["sha256"]:
            os.unlink(dest)
            raise MediaUpdateError(f"{name}: sha256 не совпал с выпуском -- файл дистрибутива испорчен")
        done += int(payload[key]["size"])
    for part, name, _key in SLOT_FILES:
        os.replace(os.path.join(dirs[part], name + ".part"), os.path.join(dirs[part], name))

    from ..install.installer import slot_cfg

    with open(os.path.join(dirs["boot"], "slot.cfg"), "w", encoding="ascii") as handle:
        handle.write(slot_cfg(payload["verity_root_hash"], int(payload["serial"])))
    shutil.copyfile(os.path.join(directory, distribution.RELEASE_NAME),
                    os.path.join(dirs["boot"], "release.json"))
    os.sync()
    grubenv.update(where["env"], {"poa_try": target, "poa_tries": str(BOOT_TRIES)})
    return {"slot": target, "serial": int(payload["serial"]), "name": payload.get("name", ""),
            "previous": int(where["current"].get("serial", 0))}
