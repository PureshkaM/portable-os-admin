"""Прослойка между экранами и сервером доступа v3 (Э7).

Как `revocation_publish` для отзыва: экранам не нужно знать ни про ключ
полномочий, ни про номера команд. Сервер доступа -- тот же сервер, с которым
контейнер познакомился для отзыва.
"""
from __future__ import annotations

from . import access_client


def is_connected(container) -> bool:
    return bool(container.data.revocation_server_url.strip())


def client(container) -> access_client.AccessClient:
    return access_client.AccessClient(container.data.revocation_server_url,
                                      container.admin_authority_secret)


def issue_activation(container, media_id: str) -> str:
    """Новый код активации. Показывается администратору один раз."""
    code = client(container).issue_activation(media_id)
    container.record_event("access_activation", media_id, "выдан код активации")
    return code


def set_targets(container, media_id: str, targets: list[dict]) -> int:
    count = client(container).set_targets(media_id, targets)
    container.record_event("access_targets", media_id, f"рабочих мест: {count}")
    return count


def get_targets(container, media_id: str) -> list[dict]:
    return client(container).get_targets(media_id)


def unblock(container, media_id: str) -> None:
    client(container).unblock(media_id)
    container.record_event("access_unblock", media_id, "носитель разблокирован на сервере")


def media_events(container, media_id: str) -> list[str]:
    """Строки журнала сервера об этом носителе, новые сверху."""
    log = client(container).log()
    lines = [f"{e.get('t') or e['received']}: {e['text']}" for e in log.get("device", [])
             if e.get("media_id") == media_id]
    lines += [f"{e['t']}: {media_id}: {e['kind']}" + (f" {e['detail']}" if e.get("detail") else "")
              for e in log.get("server", []) if e.get("media_id") == media_id]
    return sorted(lines, reverse=True)


def publish_descriptor(container, endpoints: list[str] | None = None,
                       fallback: list[str] = ()) -> int:
    """Подписывает рабочим ключом и публикует описатель сервера (Э18).

    По умолчанию -- текущий адрес сервера, его отпечаток TLS (снятый с обычной
    проверкой УЦ) и ключ подписи сервера из контейнера: то же, что носитель
    получает при записи. Смена адреса или ключей -- передать новые.
    """
    import base64

    from cryptography.hazmat.primitives import serialization

    from . import org_root

    endpoints = list(endpoints or [container.data.revocation_server_url.rstrip("/")])
    pins = [access_client.fetch_tls_spki(url) for url in endpoints if url.startswith("https://")]
    key = serialization.load_pem_public_key(container.data.revocation_public_key_pem.encode("ascii"))
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    document = org_root.descriptor(container, endpoints, sorted(set(pins)),
                                   [base64.b64encode(raw).decode("ascii")], list(fallback))
    serial = client(container).publish_descriptor(document)
    container.record_event("access_descriptor", "", f"опубликован описатель {serial}")
    return serial


# -- выпуски и группы (Э7) ------------------------------------------------------------
#: Файлы выпуска, которые носитель скачивает (`poa_update.FILES` + сам выпуск).
RELEASE_FILES = ("release.json", "rootfs.squashfs", "rootfs.verity", "vmlinuz", "initrd.img")


class AccessActionError(Exception):
    pass


def overview(container) -> dict:
    return client(container).overview()


def publish_distribution(container, directory: str, dist_id: str, progress=None) -> int:
    """Собранный дистрибутив -- выпуском на сервер. Возвращает номер выпуска.

    Неподписанный выпуск носитель не примет (`poa_update.verify`), поэтому он
    и не публикуется: ошибка здесь дешевле, чем парк, который молча не
    обновляется. Имя дистрибутива на сервере -- линейка выпусков: у неё одно
    семейство, архитектура и разметка, и чужой выпуск в неё не кладётся.
    """
    import json
    import os

    with open(os.path.join(directory, "release.json"), encoding="utf-8") as handle:
        document = json.load(handle)
    if "signer" not in document:
        raise AccessActionError(
            "выпуск не подписан: рабочий ключ контейнера не заверен корнем организации "
            "(Обслуживание → Корень), а неподписанный выпуск носитель не примет")
    release = document["release"]
    api = client(container)
    known = {d["id"]: d for d in api.overview()["distributions"]}
    line = known.get(dist_id)
    if line and (line["family"], line["arch"], line["layout"]) != \
            (release["family"], release["arch"], release["layout"]):
        raise AccessActionError(
            f"{dist_id} -- это {line['family']}/{line['arch']}, разметка {line['layout']}; "
            f"выпуск {release['family']}/{release['arch']} сюда не подходит")
    if line and line["releases"] and release["serial"] <= line["releases"][0]:
        raise AccessActionError(
            f"выпуск {release['serial']} не новее опубликованного {line['releases'][0]}")
    api.distribution(dist_id, release["family"], release["arch"], release["layout"],
                     min_serial=line["min_serial"] if line else 0)
    api.publish_release(dist_id, release["serial"],
                        [os.path.join(directory, name) for name in RELEASE_FILES], progress)
    container.record_event("access_release", "", f"{dist_id}: выпуск {release['serial']}")
    return int(release["serial"])


def set_min_serial(container, dist_id: str, serial: int) -> None:
    """Ниже этого номера сервер отвечает 426: носитель обновляется до входа."""
    api = client(container)
    line = {d["id"]: d for d in api.overview()["distributions"]}.get(dist_id)
    if line is None:
        raise AccessActionError(f"дистрибутива {dist_id} на сервере нет")
    api.distribution(dist_id, line["family"], line["arch"], line["layout"], min_serial=serial)
    container.record_event("access_min_serial", "", f"{dist_id}: не ниже {serial}")


def create_group(container, group_id: str, parent: str) -> None:
    client(container).group(group_id, parent)
    container.record_event("access_group", "", f"группа {group_id} в {parent}")


def assign(container, group_id: str, dist_id: str | None) -> None:
    client(container).assign(group_id, dist_id)
    container.record_event("access_assign", "",
                           f"{group_id}: {dist_id or 'по наследству'}")


def set_vm_policy(container, group_id: str, allow: bool | None) -> None:
    client(container).allow_vm(group_id, allow)
    word = {True: "разрешены", False: "запрещены", None: "по наследству"}[allow]
    container.record_event("access_policy", "", f"{group_id}: ВМ {word}")


def move_media(container, media_id: str, group_id: str) -> None:
    client(container).move_media(media_id, group_id)
    container.record_event("access_group", media_id, f"в группу {group_id}")
