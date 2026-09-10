"""Система на носителе: сжатый образ только для чтения плюс слой изменений.

Зачем
-----

Раньше развёртывание раскладывало на носитель распакованное дерево — 4,32 ГБ
миллионом мелких файлов. Замер на тестовой флешке: она держит 2,9 МБ/с
последовательной записи, а такая раскладка шла 0,49–0,88 МБ/с, то есть втрое
хуже потолка, и прогон занимал полтора часа.

Здесь на носитель кладётся **один файл** — тот самый squashfs, который собран
на шаге сборки дистрибутива, 1,56 ГиБ. Это и втрое меньше байт, и запись
последовательная. Записи системы уходят в overlay поверх образа.

Что это даёт кроме скорости
---------------------------

База становится доступной только для чтения. Подменить системный файл так,
чтобы подмена пережила сброс верхнего слоя, нельзя: squashfs неизменяем.
Для киоска это свойство по существу — систему можно вернуть в заведомо
известное состояние, очистив один каталог.

Честная граница: overlay **не** мешает подменить файл в текущем сеансе — то,
что записано в верхний слой, перекрывает нижний. Неизменяемость здесь про
постоянство, а не про невозможность записи. И LUKS шифрует, но не
аутентифицирует: от порчи шифротекста защищает не он, а dm-verity, которого
здесь пока нет.

Устройство
----------

Внутри LUKS-тома лежит обычная ext4 — контейнер, а в нём::

    /system.squashfs      эталон, только чтение
    /overlay/upper/       изменения системы
    /overlay/work/        служебный каталог overlayfs

При загрузке initramfs открывает LUKS, монтирует контейнер, подключает образ
петлевым устройством и собирает overlay, который и становится корнем.

Верхний слой лежит на том же шифрованном томе — решение пользователя от
26.08.2026. Значит, изменения переживают перезагрузку: работает
автообновление, копятся журналы, сохраняются настройки сети. Вариант с
верхним слоем в оперативной памяти (каждая загрузка — чистая система)
обсуждался и отклонён именно поэтому.
"""
from __future__ import annotations

#: Имя образа системы внутри контейнера.
SYSTEM_IMAGE = "system.squashfs"
#: Каталоги overlayfs внутри контейнера.
UPPER_DIR = "overlay/upper"
WORK_DIR = "overlay/work"

#: Чем корень назван в командной строке ядра, когда систему собирает наш
#: хук, а не сам initramfs. Значение вида `poa:/dev/mapper/poa-xxxx`.
#:
#: Префикс нужен там, где корень монтирует systemd внутри initramfs (dracut).
#: Он монтирует то, что назвал `root=`, а следом dracut отдаёт корень своему
#: модулю `rootfs-block`, и тот **размонтирует** чужое монтирование и
#: монтирует сам, проверяя результат на пригодность. Контейнер проверку не
#: проходит: в нём нет ни /proc, ни /sbin/init -- один файл образа и слой
#: изменений. Дальше загрузка уходит в аварийную оболочку с сообщением
#: «Can't mount root filesystem», по которому причину не угадать (проверено
#: загрузкой РЕД ОС + KDE 10.09.2026).
#:
#: С префиксом обе проверки отключаются разом: `systemd-fstab-generator`
#: пропускает корень, который не является путём к устройству, а `rootfs-block`
#: занимается только значениями вида `block:`. Корень остаётся целиком за
#: нашим хуком. Тем же приёмом собираются live-образы (`root=live:...`).
ROOT_PREFIX = "poa"

#: Где initramfs собирает конструкцию. Всё в /run: он существует до того, как
#: появится корень, и не остаётся в собранной системе.
CONTAINER_MOUNT = "/run/poa/container"
LOWER_MOUNT = "/run/poa/lower"


def container_paths(container_root: str) -> tuple[str, str, str]:
    """(образ, верхний слой, рабочий каталог) внутри смонтированного контейнера."""
    root = container_root.rstrip("/")
    return f"{root}/{SYSTEM_IMAGE}", f"{root}/{UPPER_DIR}", f"{root}/{WORK_DIR}"


def root_parameter(mapper_path: str) -> str:
    """Значение `root=` для носителя, чей корень собирает наш хук."""
    return f"{ROOT_PREFIX}:{mapper_path}"


def prepare_commands(container_root: str) -> list[list[str]]:
    """Каталоги, которые overlayfs требует до первого монтирования.

    `workdir` обязан лежать на той же файловой системе, что и `upperdir`, и
    быть пустым — иначе монтирование откажет, причём с сообщением, по
    которому это не очевидно.
    """
    _, upper, work = container_paths(container_root)
    return [["mkdir", "-p", upper], ["mkdir", "-p", work]]


def copy_image_command(archive: str, container_root: str) -> list[str]:
    """Кладёт образ в контейнер одним последовательным потоком.

    `cp` без ухищрений: это один файл, и вся выгода именно в том, что он один.
    """
    image, _, _ = container_paths(container_root)
    return ["cp", "--no-preserve=mode", archive, image]


def mount_command(container_root: str, target: str) -> list[str]:
    """Собирает overlay поверх образа для работы на машине администратора.

    Нужно, чтобы дальнейшие шаги развёртывания — запись fstab, chroot,
    пересборка initramfs, установка загрузчика — видели систему целиком, а не
    один файл. Нижний слой подключается отдельной командой (`lower_mount_command`).
    """
    _, upper, work = container_paths(container_root)
    return [
        "mount", "-t", "overlay", "poa-overlay",
        "-o", f"lowerdir={LOWER_MOUNT},upperdir={upper},workdir={work}",
        target,
    ]


def lower_mount_command(container_root: str) -> list[str]:
    """Подключает образ только для чтения в LOWER_MOUNT."""
    image, _, _ = container_paths(container_root)
    return ["mount", "-t", "squashfs", "-o", "loop,ro", image, LOWER_MOUNT]


#: Как называется переменная с точкой монтирования будущего корня. У
#: initramfs-tools это `$rootmnt`, у dracut — `$NEWROOT`. Разница ровно в
#: имени, поэтому тело скрипта одно на оба семейства.
ROOTMNT_INITRAMFS_TOOLS = "rootmnt"
ROOTMNT_DRACUT = "NEWROOT"


def boot_script(
    mapper_name: str,
    rootmnt_var: str = ROOTMNT_INITRAMFS_TOOLS,
    *,
    mount_container: bool = False,
) -> str:
    """Тело загрузочного скрипта, одинаковое для обоих семейств.

    Отличается только обвязка: у initramfs-tools это `scripts/local-bottom`,
    у dracut — модуль с хуком `pre-pivot`. Само действие одно и то же, поэтому
    текст здесь один: две копии разошлись бы при первой же правке.

    Различие одно и оно в том, откуда берётся контейнер. У initramfs-tools
    корень к этому моменту уже смонтирован, и его достаточно перенести
    (`mount_container=False`). У dracut корень не монтируется вовсе: там
    носитель называет себя `root=poa:...`, чтобы никто, кроме этого хука, за
    корень не брался, — и контейнер хук монтирует сам
    (`mount_container=True`). Почему так, объяснено у `ROOT_PREFIX`.

    Если что-то не получилось — скрипт **не** оставляет контейнер корнем:
    система из одного файла system.squashfs не загрузится, а диагностировать
    это по симптомам почти невозможно. Лучше внятная строка на консоли.
    """
    # `set -u` только там, где скрипт запускается сам по себе. dracut
    # исполняет хуки через `.`, то есть в своей же оболочке: строгий режим
    # остался бы включённым и после нашего скрипта и уронил бы dracut на
    # первой же необъявленной переменной — в чужом коде, без всякой связи с
    # нами.
    strict = "" if mount_container else "set -u\n"

    if mount_container:
        container_mount = f"""# Корень не смонтирован никем: носитель назвался `root={ROOT_PREFIX}:...`,
# и монтирование целиком на этом хуке.
mount /dev/mapper/{mapper_name} "$container" || fail "не смонтировать контейнер"

# Перенос смонтированного из initramfs в собранный корень возможен только для
# частного (private) поддерева: systemd делает корень initramfs общим (shared),
# а `mount --move` на общем поддереве отказывает.
mount --make-private "$container" 2>/dev/null || true"""
    else:
        container_mount = f"""# Контейнер уже смонтирован как корень: переносим его, не размонтируя, чтобы
# не потерять открытые файлы и не зависеть от порядка.
#
# `-o move`, а не `--move`: в initramfs команду выполняет BusyBox, а он
# длинных опций не знает. Проверено загрузкой с флешки 10.09.2026 -- перенос
# молча не срабатывал (вывод отправлен в /dev/null), а следом попытка
# смонтировать том второй раз падала с «Device or resource busy», потому что
# он уже был корнем.
mount -o move "$__ROOTMNT__" "$container" 2>/dev/null || \\
    mount --move "$__ROOTMNT__" "$container" 2>/dev/null || \\
    mount /dev/mapper/{mapper_name} "$container" || fail "не смонтировать контейнер"

# Контейнер нужен на запись: слой изменений лежит на нём. Ядро монтирует
# корень только для чтения (`ro` в командной строке -- так принято, чтобы
# systemd перемонтировал его сам после проверки), и overlay с таким upperdir
# просто не соберётся.
mount -o remount,rw "$container" 2>/dev/null || true"""

    script = f"""#!/bin/sh
# generated by portable-os-admin
#
# Корень носителя — не файловая система, а сжатый образ плюс слой изменений.
# Здесь смонтированный контейнер подменяется собранным overlay.
{strict}
fail() {{
    echo "poa-overlay: $1" >&2
    echo "poa-overlay: система не собрана, загрузка прервана" >&2
    exit 1
}}

container={CONTAINER_MOUNT}
lower={LOWER_MOUNT}
image="$container/{SYSTEM_IMAGE}"
upper="$container/{UPPER_DIR}"
work="$container/{WORK_DIR}"

[ -b /dev/mapper/{mapper_name} ] || fail "зашифрованный том не открыт"

mkdir -p "$container" "$lower" || fail "не создать точки монтирования"

{container_mount}

[ -f "$image" ] || fail "в контейнере нет $image"

modprobe loop 2>/dev/null || true
modprobe squashfs 2>/dev/null || true
modprobe overlay 2>/dev/null || true

mount -t squashfs -o loop,ro "$image" "$lower" || fail "не подключить образ системы"

mkdir -p "$upper" "$work" || fail "не создать каталоги слоя изменений"

mount -t overlay poa-overlay \\
    -o "lowerdir=$lower,upperdir=$upper,workdir=$work" "$__ROOTMNT__" \\
    || fail "не собрать overlay"

# Контейнер должен остаться доступным изнутри системы: на нём лежит и образ, и
# слой изменений, и размонтировать его нельзя, пока система работает.
mkdir -p "$__ROOTMNT__{CONTAINER_MOUNT}"
mount -o move "$container" "$__ROOTMNT__{CONTAINER_MOUNT}" 2>/dev/null || \\
    mount --move "$container" "$__ROOTMNT__{CONTAINER_MOUNT}" 2>/dev/null || true
"""
    return script.replace("__ROOTMNT__", rootmnt_var)


def fstab_rows() -> list[tuple[str, str, str, str, str, str]]:
    """Строки fstab, которые нужны системе поверх overlay.

    Корень в fstab не описывается: он собран initramfs до того, как fstab
    прочитан, и запись о нём заставила бы systemd пытаться перемонтировать
    то, чего в его понимании не существует.
    """
    return []
