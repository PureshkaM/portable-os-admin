#!/usr/bin/env python3
"""Обновление образа в неактивный слот (LIVE-2.0, Э9).

Сервер при входе называет назначенный выпуск (`release`: распределение, номер,
файлы). Если он новее работающего -- файлы скачиваются в **неактивный** слот:

1. `release.json` -- и сразу проверка: подписан рабочим ключом с сертификатом
   корня организации (`poa_trust`), то же семейство, архитектура и разметка,
   номер больше текущего;
2. образ, дерево verity, ядро и initramfs -- рядом, с суффиксом `.part`, с
   докачкой; sha256 каждого сверяется с подписанным `release.json`;
3. только когда всё сошлось -- файлы встают на места, пишется `slot.cfg`, и
   GRUB получает «попробуй новый слот» (`poa_try`, три попытки).

До шага 3 загрузчик о новом слоте не знает: прерванная загрузка оставляет
носитель с прежней системой. После -- если новая не подтвердится
(`poa-boot-ok`), GRUB сам вернётся к прежней.

Прогресс -- в `/run/poa/update.json`: окно «Подключение» показывает «не
выключайте компьютер». Запускается службой `poa-image-update` под
`systemd-inhibit`: выключение и сон ждут конца записи.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import poa_client  # noqa: E402
import poa_trust  # noqa: E402

IMAGES = "/run/poa/images"
BOOT = "/boot"
STATE = "/run/poa/update.json"
TRIES = 3
#: Попыток докачать файл, прежде чем сдаться до следующего входа.
ATTEMPTS = 30
#: Имя файла выпуска -> (где лежит в слоте, ключ sha256 в release.json).
FILES = (
    ("rootfs.squashfs", "images", "image"),
    ("rootfs.verity", "images", "verity"),
    ("vmlinuz", "boot", "kernel"),
    ("initrd.img", "boot", "initrd"),
)


class UpdateError(Exception):
    pass


def cmdline_value(name: str, cmdline: str | None = None) -> str:
    if cmdline is None:
        with open("/proc/cmdline", encoding="ascii") as handle:
            cmdline = handle.read()
    for word in cmdline.split():
        if word.startswith(name + "="):
            return word.split("=", 1)[1]
    return ""


def other_slot(slot: str) -> str:
    return {"a": "b", "b": "a"}[slot]


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Updater:
    def __init__(self, endpoint: poa_client.Endpoint, anchors: list[dict], editenv: str,
                 run=subprocess.run, images: str = IMAGES, boot: str = BOOT,
                 state: str = STATE, cmdline: str | None = None) -> None:
        self.endpoint = endpoint
        self.anchors = anchors
        self.editenv = editenv
        self.run = run
        self.images, self.boot, self.state = images, boot, state
        self.sleep = time.sleep
        self.slot = cmdline_value("poa.slot", cmdline)
        self.serial = int(cmdline_value("poa.serial", cmdline) or 0)

    # -- состояние для окна ------------------------------------------------
    def report(self, **fields) -> None:
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        poa_client.atomic_write(self.state, json.dumps(fields, ensure_ascii=False).encode(), 0o644)

    def current_release(self) -> dict:
        path = os.path.join(self.boot, f"slot-{self.slot}", "release.json")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)["release"]

    def needed(self, info: dict) -> bool:
        return bool(info) and bool(self.slot) and int(info.get("serial", 0)) > self.serial

    # -- обновление --------------------------------------------------------
    def apply(self, info: dict) -> str:
        if not self.needed(info):
            return ""
        target = other_slot(self.slot)
        base = f"/v3/release/{info['distribution']}/{int(info['serial'])}"
        boot_dir = os.path.join(self.boot, f"slot-{target}")
        images_dir = os.path.join(self.images, f"slot-{target}")
        self.report(state="checking", serial=info["serial"])
        self.run(["mount", "-o", "remount,rw", self.images], check=False)
        try:
            os.makedirs(boot_dir, exist_ok=True)
            os.makedirs(images_dir, exist_ok=True)
            release_part = os.path.join(boot_dir, "release.json.part")
            if os.path.exists(release_part):
                os.unlink(release_part)
            self.endpoint.download(f"{base}/release.json", release_part)
            with open(release_part, encoding="utf-8") as handle:
                document = json.load(handle)
            payload = self.verify(document, info)
            total = sum(int(payload[key].get("size", 0)) for _f, _w, key in FILES)
            done = 0
            for name, where, key in FILES:
                directory = images_dir if where == "images" else boot_dir
                part = os.path.join(directory, name + ".part")
                expected = int(payload[key]["size"])
                self.fetch(f"{base}/{name}", part, expected,
                           lambda size, done=done, name=name: self.report(
                               state="downloading", serial=info["serial"], file=name,
                               done=done + size, total=total))
                if sha256_file(part) != payload[key]["sha256"]:
                    os.unlink(part)
                    raise UpdateError(f"{name}: sha256 не совпал с подписанным выпуском")
                done += expected
            # Всё сошлось -- на места. До этого момента слот загрузчику не виден.
            for name, where, _key in FILES:
                directory = images_dir if where == "images" else boot_dir
                os.replace(os.path.join(directory, name + ".part"), os.path.join(directory, name))
            poa_client.atomic_write(os.path.join(boot_dir, "slot.cfg"),
                                    (f"set poa_roothash={payload['verity_root_hash']}\n"
                                     f"set poa_serial={int(payload['serial'])}\n").encode(), 0o644)
            os.replace(release_part, os.path.join(boot_dir, "release.json"))
            self.run(["sync"], check=False)
            env = os.path.join(self.boot, "efi", "EFI", "poa", "grubenv")
            self.run([self.editenv, env, "set", f"poa_try={target}", f"poa_tries={TRIES}"], check=True)
            self.report(state="ready", serial=info["serial"], slot=target)
            return target
        except Exception as exc:  # noqa: BLE001 - окно должно узнать, а носитель -- остаться прежним
            self.report(state="failed", serial=info.get("serial"), error=str(exc)[:300])
            raise
        finally:
            self.run(["mount", "-o", "remount,ro", self.images], check=False)

    def fetch(self, path: str, part: str, expected: int, progress) -> None:
        """Файл целиком, с докачкой. Обрывы -- норма: медленная флешка не
        успевает за сетью, и сервер закрывает соединение по своему таймауту
        (живая проверка 02.10.2026: образ обрывался на 313 МБ из 952), связь
        у ноутбука пропадает. Каждая попытка продолжает с места обрыва."""


        last = [0.0]

        def throttled(size: int) -> None:
            now = time.monotonic()
            if now - last[0] >= 2 or size >= expected:
                last[0] = now
                progress(size)

        # Считаются только попытки без продвижения: обрыв, после которого файл
        # вырос, -- это работа, а не отказ (живая проверка 02.10.2026: образ
        # шёл кусками по 6 МБ, и счётчик всех обрывов сдался бы на 30-м).
        stalled = 0
        size = os.path.getsize(part) if os.path.exists(part) else 0
        while stalled < ATTEMPTS:
            before = size
            try:
                self.endpoint.download(path, part, throttled)
            except poa_client.AccessError:
                pass
            size = os.path.getsize(part) if os.path.exists(part) else 0
            if size == expected:
                return
            if size > expected:
                os.unlink(part)      # мусор от прежней попытки -- заново
                size = 0
            stalled = 0 if size > before else stalled + 1
            if stalled:
                self.sleep(min(30, 2 * stalled))
        raise UpdateError(f"{os.path.basename(part)[:-5]}: не скачался за {ATTEMPTS} попыток")

    def verify(self, document: dict, info: dict) -> dict:
        """Подпись, номер и совместимость -- до того, как скачан хоть байт образа."""
        try:
            payload = poa_trust.verify_document(self.anchors, document, "release")
        except poa_trust.TrustError as exc:
            raise UpdateError(f"выпуск не прошёл проверку подписи: {exc}") from exc
        if int(payload.get("serial", 0)) != int(info["serial"]) or int(payload["serial"]) <= self.serial:
            raise UpdateError("номер выпуска не тот, что назначен, или не новее текущего")
        current = self.current_release()
        for field in ("family", "arch", "layout"):
            if payload.get(field) != current.get(field):
                raise UpdateError(f"выпуск несовместим с носителем: {field} {payload.get(field)!r}")
        return payload


def main(argv: list[str]) -> int:
    """poa_update.py <access.json> <release-info.json> -- запускает служба."""
    with open(argv[1], encoding="utf-8") as handle:
        config = json.load(handle)
    with open(argv[2], encoding="utf-8") as handle:
        info = json.load(handle)
    import poa_accessd

    service = poa_accessd.Service(config)
    server = service.server()
    endpoint = poa_client.Endpoint(server["endpoint"], tuple(server.get("tls_spki") or ()))
    updater = Updater(endpoint, service.anchors(), config.get("grub_editenv", "grub-editenv"))
    updater.apply(info)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
