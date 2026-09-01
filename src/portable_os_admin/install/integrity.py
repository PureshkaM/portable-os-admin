"""Целостность загрузчика и initramfs (задача 13).

Что защищается и почему именно это
----------------------------------

Корень носителя зашифрован, а `/boot` и ESP -- нет, и иначе быть не может:
прошивке нужно прочитать загрузчик, а загрузчику -- ядро и initramfs, и оба
работают до того, как появится хоть какой-то ключ. Значит всё, что лежит на
этих двух разделах, доступно на запись всякому, у кого носитель побывал в
руках: shim, grub, `grub.cfg`, ядро, initramfs и модуль восстановления.

Подменённый initramfs -- это программа, которая спросит у человека пароль
ровно так же, как настоящая, и запомнит его. До 01.09.2026 такая подмена не
обнаруживалась ничем.

Чего этот механизм НЕ делает
----------------------------

Он не мешает подмене, он её обнаруживает. Помешать может только цепочка,
проверяющая подпись до исполнения, -- Secure Boot с собственным ключом в
прошивке или TPM, -- а этого у переносного носителя, который несут на чужую
машину, нет по условию задачи.

Отсюда честная граница: **если носитель подменили и человек загрузился с
него, пароль уже украден.** Проверка ловит подмену либо до загрузки (когда
носитель принесли администратору), либо после неё (на самой системе), но не
вместо неё. Написано это здесь, а не в README, потому что читать будут код.

Как устроено
------------

Манифест -- список путей и sha256 каждого файла на ESP и `/boot`. Он
подписывается HMAC-SHA256 на ключе, выведенном из секрета администратора: без
контейнера манифест не подделать, а сам секрет на носитель не попадает.

Копий манифеста две, и обе нужны:

* **в контейнере администратора** -- эталон, по которому носитель проверяют,
  когда его принесли. Он лежит там же, где ключи служебных томов, и на
  носителе его нет вовсе;
* **на зашифрованном корне носителя** -- по нему система проверяет себя сама
  после разблокировки. Подменить эту копию, не зная пароля, нельзя, а
  подменивший `/boot` о ней и не узнает.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

#: Где на зашифрованном корне лежит копия манифеста и его подпись.
MANIFEST_PATH = "var/lib/portable-os-admin/boot-integrity.json"

#: Что считается неизменным. Всё, что исполняется до расшифровки корня.
GUARDED_AREAS = ("esp", "boot")


def _hash_file(path: str) -> str:
    """sha256 одного файла, читая его кусками.

    Кусками, а не целиком: на `/boot` лежит initramfs, и он на этом проекте
    доходил до 138 МБ. Читать такое в память ради одной суммы незачем.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(roots: "dict[str, str]") -> "dict[str, str]":
    """Пути и суммы всех файлов в названных каталогах.

    Ключ пути -- область и путь внутри неё (`esp:EFI/BOOT/BOOTX64.EFI`), а не
    путь на машине администратора: тот зависит от того, куда смонтировали, и
    сравнивать по нему было бы нельзя.

    Симлинки не разыменовываются и в манифест не попадают: их цель уже
    посчитана как файл, а сама ссылка -- это имя, а не содержимое.
    """
    manifest: dict[str, str] = {}
    # ESP монтируется внутрь /boot, то есть один корень лежит в другом.
    # Обходя внешний, надо не заходить во вложенный: иначе те же файлы
    # попадут в опись дважды -- как `esp:EFI/...` и как `boot:efi/EFI/...`, --
    # и любая правка ESP покажется двумя изменениями вместо одного.
    nested = {
        os.path.realpath(root): area
        for area, root in roots.items()
        if root and os.path.isdir(root)
    }
    for area, root in sorted(roots.items()):
        if not root or not os.path.isdir(root):
            continue
        real_root = os.path.realpath(root)
        for directory, dirs, files in os.walk(root):
            dirs[:] = [
                name for name in dirs
                if os.path.realpath(os.path.join(directory, name)) not in nested
                or os.path.realpath(os.path.join(directory, name)) == real_root
            ]
            for name in sorted(files):
                full = os.path.join(directory, name)
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                relative = os.path.relpath(full, root)
                manifest[f"{area}:{relative}"] = _hash_file(full)
    return manifest


def sign(manifest: "dict[str, str]", admin_secret: bytes) -> str:
    """Подпись манифеста ключом, выведенным из секрета администратора.

    HMAC, а не хеш от конкатенации: у HMAC ключ и данные разделены
    конструкцией, и это та же причина, по которой он выбран для кодов
    восстановления.

    Подписывается каноническая форма -- отсортированный JSON без пробелов, --
    чтобы подпись не зависела от того, в каком порядке словарь оказался в
    памяти.
    """
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(admin_secret, b"poa-boot-integrity\\x00" + payload, hashlib.sha256).hexdigest()


def verify(manifest: "dict[str, str]", signature: str, admin_secret: bytes) -> bool:
    """Та ли это опись, которую составил этот администратор.

    Сравнение постоянного времени: подпись сверяется на машине, где может
    оказаться кто угодно, и разница во времени ответа -- тоже ответ.
    """
    return hmac.compare_digest(sign(manifest, admin_secret), signature)


def compare(expected: "dict[str, str]", actual: "dict[str, str]") -> "dict[str, list[str]]":
    """Чем нынешнее содержимое отличается от описанного.

    Три вида различий, и они не равнозначны. Изменённый файл -- это подмена
    того, что исполняется при загрузке. Лишний -- принесённое со стороны.
    Пропавший чаще всего значит, что носитель обновляли не через эту
    программу, но выглядит так же, как изъятие, поэтому называется отдельно.
    """
    changed = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )
    return {
        "changed": changed,
        "added": sorted(actual.keys() - expected.keys()),
        "removed": sorted(expected.keys() - actual.keys()),
    }


def is_intact(difference: "dict[str, list[str]]") -> bool:
    return not any(difference.values())


def describe(difference: "dict[str, list[str]]") -> str:
    """Человеческое объяснение того, что нашлось.

    Отдельной строкой на вид различия, а не общим числом: администратору,
    которому принесли носитель, важно не «десять отличий», а то, что среди
    них подменённый initramfs.
    """
    if is_intact(difference):
        return "Загрузчик и initramfs не изменялись с момента записи."
    parts = []
    if difference["changed"]:
        parts.append(f"изменено файлов: {len(difference['changed'])}")
    if difference["added"]:
        parts.append(f"появилось лишних: {len(difference['added'])}")
    if difference["removed"]:
        parts.append(f"пропало: {len(difference['removed'])}")
    return "Незашифрованная часть носителя изменена — " + ", ".join(parts) + "."


#: Куда носитель кладёт свою копию описи и чем её проверяет.
SELF_CHECK_SCRIPT = "usr/local/sbin/poa-boot-integrity"
SELF_CHECK_UNIT = "etc/systemd/system/poa-boot-integrity.service"


def self_check_files(manifest: "dict[str, str]") -> "dict[str, str]":
    """Копия описи на зашифрованном корне и служба, которая её сверяет.

    Зачем вторая копия, когда эталон и так лежит в контейнере: контейнер
    остаётся у администратора, а носитель уезжает с человеком. Между
    проверками у администратора проходят недели, и всё это время подмену не
    заметил бы никто. Копия на зашифрованном корне позволяет носителю сказать
    о ней самому -- при первой же загрузке после подмены.

    Подделать эту копию, не зная пароля, нельзя: она лежит за LUKS. Тот, кто
    подменил `/boot`, о ней даже не узнает -- корень для него шифротекст.

    Граница та же и здесь: проверка идёт **после** разблокировки, то есть
    после того, как подменённый initramfs уже получил бы пароль. Это
    обнаружение, а не защита, и служба говорит ровно это.

    Отчёт идёт в журнал, а не на экран: журнал носителя лежит на отдельном
    томе, который администратор открывает своим ключом (см. admin/logs.py), и
    прочитать его сможет именно он. Показывать пользователю киоска «ваш
    загрузчик подменён» бессмысленно -- сделать с этим он ничего не может.
    """
    import json

    listing = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    script = f"""#!/bin/sh
# generated by portable-os-admin
# Сверка незашифрованной части носителя с описью, снятой при записи.
#
# Запускается после монтирования /boot и ESP, то есть уже в системе. Найденное
# расхождение означает, что загрузчик, ядро или initramfs подменили — и что
# пароль, введённый при этой загрузке, мог быть перехвачен.
set -eu

MANIFEST=/{MANIFEST_PATH}
[ -f "$MANIFEST" ] || exit 0

changed=0
# Пути в описи записаны как «область:путь»: esp -- то, что смонтировано в
# /boot/efi, boot -- сам /boot. Разделять их приходится здесь же, потому что
# на носителе нет ни python, ни jq.
while IFS= read -r line; do
    path=${{line%%|*}}
    want=${{line#*|}}
    case "$path" in
        esp:*) file="/boot/efi/${{path#esp:}}" ;;
        boot:*) file="/boot/${{path#boot:}}" ;;
        *) continue ;;
    esac
    if [ ! -f "$file" ]; then
        logger -t poa-integrity "пропал файл: $path"
        changed=$((changed + 1))
        continue
    fi
    have=$(sha256sum "$file" | cut -d' ' -f1)
    if [ "$have" != "$want" ]; then
        logger -t poa-integrity "изменён файл: $path"
        changed=$((changed + 1))
    fi
done <<'POA_LIST'
{chr(10).join(f"{path}|{digest}" for path, digest in sorted(manifest.items()))}
POA_LIST

if [ "$changed" -gt 0 ]; then
    logger -t poa-integrity "ВНИМАНИЕ: незашифрованная часть носителя изменена ($changed). Пароль этой загрузки считать скомпрометированным."
else
    logger -t poa-integrity "загрузчик и initramfs совпадают с описью"
fi
exit 0
"""
    unit = """# generated by portable-os-admin
[Unit]
Description=Сверка загрузчика и initramfs с описью
# После монтирования: сверять нечего, пока /boot и ESP не на месте.
After=local-fs.target
Wants=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/poa-boot-integrity
# Отказ проверки не должен мешать загрузке: носитель с подменённым
# загрузчиком обязан дойти до системы и сказать об этом в журнал, а не
# встать посреди дороги, оставив человека без объяснения.
SuccessExitStatus=0 1

[Install]
WantedBy=multi-user.target
"""
    return {
        MANIFEST_PATH: listing + "\n",
        SELF_CHECK_SCRIPT: script,
        SELF_CHECK_UNIT: unit,
    }
