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


def normalize_pin(kind: str, text: str) -> str:
    """Отпечаток цели в том виде, в каком его ждёт носитель, или ошибка.

    * RDP -- `sha256:<64 hex>`; принимается и с двоеточиями, и заглавными;
    * SSH -- `тип ключ` без комментария (строка из `ssh_host_*_key.pub`);
    * VNC -- сертификат CA в PEM. Поле в диалоге однострочное, и вставленный
      PEM теряет переводы строк, поэтому тело собирается заново и проверяется
      разбором сертификата: битый PEM здесь лучше, чем отказ VNC у
      пользователя без объяснения.
    """
    import base64
    import binascii
    import re

    text = text.strip()
    if kind == "rdp":
        digest = text[len("sha256:"):] if text.lower().startswith("sha256:") else text
        digest = digest.replace(":", "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise AccessActionError("RDP: нужен отпечаток sha256 сертификата — 64 шестнадцатеричных знака")
        return f"sha256:{digest}"
    if kind == "ssh":
        parts = text.split()
        if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-")):
            raise AccessActionError("SSH: нужен открытый ключ хоста вида «ssh-ed25519 AAAA…»")
        try:
            base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise AccessActionError("SSH: ключ хоста не похож на base64") from exc
        return f"{parts[0]} {parts[1]}"
    if kind == "vnc":
        from cryptography import x509

        match = re.search(r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----", text, re.S)
        if not match:
            raise AccessActionError("VNC: нужен сертификат CA в PEM (-----BEGIN CERTIFICATE-----…)")
        body = re.sub(r"\s+", "", match.group(1))
        try:
            x509.load_der_x509_certificate(base64.b64decode(body, validate=True))
        except (binascii.Error, ValueError) as exc:
            raise AccessActionError(f"VNC: сертификат не разбирается: {exc}") from exc
        lines = [body[i:i + 64] for i in range(0, len(body), 64)]
        return "-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n"
    raise AccessActionError(f"неизвестный тип цели: {kind}")


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
