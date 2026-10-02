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
    /medium/              слой носителя: всё, что записал установщик
    /state/               постоянное состояние: сети Wi-Fi, /var/lib/poa
    /overlay/work/        служебный каталог overlayfs при записи носителя

**LIVE-2.0 (01.10.2026, этап Э3): корень в оперативной памяти.** При загрузке
initramfs открывает LUKS, монтирует контейнер и собирает overlay из трёх
слоёв: сверху tmpfs (всё, что изменилось за сеанс, исчезает с выключением),
посередине `medium` только для чтения (fstab, crypttab, ключи, скрипты
привязки и отзыва -- то, что установщик записал этому носителю), внизу образ.
Постоянно только явно перечисленное в `STATE_BINDS`: оно монтируется из
`state` поверх соответствующих каталогов.

Прежнее решение 26.08.2026 (верхний слой на носителе ради автообновления,
журналов и сетей) пересмотрено: автообновления пакетов больше нет, журналы
живут на своём томе, сети -- в `state`.
"""
from __future__ import annotations

#: Имя образа системы внутри контейнера.
SYSTEM_IMAGE = "system.squashfs"

#: Контекст SELinux, который обязан быть у корня системы. Значение общее для
#: всей RHEL-линейки и берётся из её же политики; там, где мандатного контроля
#: нет, метка лежит неиспользованной и ничему не мешает.
ROOT_CONTEXT = "system_u:object_r:root_t:s0"
#: Слой носителя внутри контейнера. При записи носителя это верхний слой
#: overlay (туда ложится всё, что пишет установщик), при загрузке -- средний,
#: только для чтения.
MEDIUM_DIR = "medium"
#: Прежнее имя того же каталога -- для носителей, записанных до LIVE-2.0.
UPPER_DIR = MEDIUM_DIR
#: Служебный каталог overlayfs при записи носителя.
WORK_DIR = "overlay/work"
#: Постоянное состояние внутри контейнера.
STATE_DIR = "state"
#: Что переживает перезагрузку: (каталог в state, путь в системе). Всё прочее,
#: изменённое за сеанс, живёт в tmpfs и исчезает.
STATE_BINDS = (
    ("nm", "etc/NetworkManager/system-connections"),   # сохранённые сети Wi-Fi
    ("poa", "var/lib/poa"),                             # звено, описатель (Э8)
)
#: Где initramfs монтирует tmpfs верхнего слоя.
RW_MOUNT = "/run/poa/rw"
#: Предел tmpfs. Без него переполнение верхнего слоя съело бы всю память.
RW_SIZE = "50%"

#: Как носитель говорит своему initramfs, что корень собирается из образа.
#:
#: Это **не** `root=`. В семействе, где корень монтирует systemd внутри
#: initramfs (dracut), любое значение `root=` кончается попыткой смонтировать
#: его до всякого нашего хука, и обойти это не вышло ни одним значением
#: (проверено тремя загрузками РЕД ОС 10.09.2026):
#:
#: * `root=/dev/mapper/poa-…` -- systemd монтирует контейнер, следом штатный
#:   модуль dracut `rootfs-block` размонтирует чужое монтирование, монтирует
#:   сам, проверяет корень на пригодность и бракует: в контейнере нет ни
#:   /proc, ни /sbin/init, один файл образа и слой изменений. Аварийная
#:   оболочка с «Can't mount root filesystem»;
#: * `root=poa:/dev/mapper/poa-…` (как `root=live:` у live-образов) --
#:   «mount: /sysroot: unknown filesystem type 'nfs'»: `mount` из util-linux
#:   разбирает `что-то:путь` как сетевой ресурс;
#: * `root=poa` -- «mount: /sysroot: /poa is not a block device»: генератор
#:   systemd достроил слово до пути и всё равно попытался смонтировать.
#:
#: Поэтому `root=` в командной строке нет вовсе — генератору нечего
#: монтировать, — а вместо него стоит `rd.poa.overlay=1`. Корень объявляет
#: сам модуль: его хук `cmdline` пишет `root=poa` в `/etc/cmdline.d`, откуда
#: значение видят только последующие шаги dracut, но не systemd. Имя тома в
#: командной строке не нужно: оно вшито в хук при записи носителя.
ROOT_FLAG = "rd.poa.overlay"
#: Как корень называется внутри dracut, после объявления модулем.
ROOT_VALUE = "poa"

#: Где initramfs собирает конструкцию. Всё в /run: он существует до того, как
#: появится корень, и не остаётся в собранной системе.
CONTAINER_MOUNT = "/run/poa/container"
LOWER_MOUNT = "/run/poa/lower"
#: Раздел образов слотов (разметка v3) -- монтируется initramfs только для
#: чтения и переезжает в новый корень: на нём держится dm-verity корня.
IMAGES_MOUNT = "/run/poa/images"
#: Имя устройства dm-verity корня.
VERITY_MAPPER = "poa-root"
#: Файлы слота на разделе образов.
SLOT_IMAGE = "rootfs.squashfs"
SLOT_VERITY = "rootfs.verity"


def container_paths(container_root: str) -> tuple[str, str, str]:
    """(образ, верхний слой, рабочий каталог) внутри смонтированного контейнера."""
    root = container_root.rstrip("/")
    return f"{root}/{SYSTEM_IMAGE}", f"{root}/{UPPER_DIR}", f"{root}/{WORK_DIR}"


def prepare_commands(container_root: str) -> list[list[str]]:
    """Каталоги, которые overlayfs требует до первого монтирования.

    `workdir` обязан лежать на той же файловой системе, что и `upperdir`, и
    быть пустым — иначе монтирование откажет, причём с сообщением, по
    которому это не очевидно.
    """
    _, upper, work = container_paths(container_root)
    root = container_root.rstrip("/")
    return [
        ["mkdir", "-p", upper],
        ["mkdir", "-p", work],
        *(["mkdir", "-p", f"{root}/{STATE_DIR}/{name}"] for name, _ in STATE_BINDS),
        # Метка мандатного контроля на самом верхнем слое.
        #
        # Корень собранного overlay берёт инод от `upperdir`, а этот каталог
        # создаётся здесь, на свежем ext4, и метки не имеет. На РЕД ОС с
        # SELinux в enforcing это означает `/` с контекстом `unlabeled_t` -- и
        # отказ `search` на корне для каждой службы, которая его коснётся.
        #
        # Найдено прогоном 12.09.2026, и найдено дорого: носитель проходил весь
        # путь до graphical.target, после чего journald, logind, sysctl и sddm
        # падали один за другим. В журнале -- сорок восемь отказов с
        # `tcontext=system_u:object_r:unlabeled_t:s0` на `dev="overlay" ino=2`.
        # До squashfs этого не было: метки лежали на самом ext4-корне, куда их
        # ставил `setfiles` при сборке эталона, а верхнего слоя не существовало.
        #
        # `setfattr`, а не `chcon`: команда выполняется на машине
        # администратора, где SELinux может быть не собран вовсе, а xattr
        # пишется в любом случае. Там, где мандатного контроля нет (Debian),
        # метка просто не используется и ничего не стоит.
        ["setfattr", "-n", "security.selinux", "-v", ROOT_CONTEXT, upper],
    ]


def state_label_commands(container_root: str, mount_root: str) -> list[list[str]]:
    """Метки SELinux каталогов состояния -- как у каталогов, поверх которых их
    смонтируют.

    Каталог в `state` создан на свежем ext4 без метки; смонтированный поверх
    /etc/NetworkManager/system-connections, он дал бы NetworkManager отказ на
    `unlabeled_t` -- тот же класс поломки, что 12.09.2026 с корнем. Метка
    берётся с собранной системы, а не пишется здесь: она своя у каждой политики.
    Где SELinux нет, getfattr ничего не вернёт, и команда ничего не сделает.
    """
    root = container_root.rstrip("/")
    commands = []
    for name, path in STATE_BINDS:
        commands.append([
            "sh", "-c",
            f'v=$(getfattr --only-values -n security.selinux "{mount_root}/{path}" 2>/dev/null) '
            f'&& [ -n "$v" ] && setfattr -n security.selinux -v "$v" "{root}/{STATE_DIR}/{name}"; true',
        ])
    return commands


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
    selinux: bool = False,
) -> str:
    """Тело загрузочного скрипта, одинаковое для обоих семейств.

    Отличается только обвязка: у initramfs-tools это `scripts/local-bottom`,
    у dracut — модуль с хуком `pre-pivot`. Само действие одно и то же, поэтому
    текст здесь один: две копии разошлись бы при первой же правке.

    Различие одно и оно в том, откуда берётся контейнер. У initramfs-tools
    корень к этому моменту уже смонтирован, и его достаточно перенести
    (`mount_container=False`). У dracut корень не монтируется вовсе: там
    носитель не называет корня вовсе, чтобы никто, кроме этого хука, за
    корень не брался, — и контейнер хук монтирует сам
    (`mount_container=True`). Почему так, объяснено у `ROOT_FLAG`.

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
        container_mount = f"""# Корень не смонтирован никем: в командной строке его нет,
# и монтирование целиком на этом хуке.
mount /dev/mapper/{mapper_name} "$container" || fail "не смонтировать контейнер"

# Перенос смонтированного из initramfs в собранный корень возможен только для
# частного (private) поддерева: systemd делает корень initramfs общим (shared),
# а `mount --move` на общем поддереве отказывает.
mount --make-private "$container" 2>/dev/null || true"""
    else:
        container_mount = f"""# Образ проверяется ДО переноса корня, и это не педантизм.
#
# Найдено прогоном 13.09.2026: носитель, собранный обычным деревом, получил
# этот скрипт по недосмотру. Скрипт переносил корень из "$__ROOTMNT__", не
# находил образа и выходил -- оставляя "$__ROOTMNT__" пустым каталогом. Дальше
# init-bottom не мог смонтировать /dev, run-init отказывался переходить в
# «не точку монтирования», и initramfs уходил в петлю. Проверка на этом месте
# делает такой носитель просто загружающимся: скрипт молча уступает.
# С разметкой v3 образ не в контейнере, а на разделе образов; признак
# «корень наш» -- слой носителя в контейнере.
[ -d "$__ROOTMNT__/{MEDIUM_DIR}" ] || {{
    echo "poa-overlay: слоя носителя нет, корень оставлен как есть" >&2
    exit 0
}}

# Контейнер уже смонтирован как корень: переносим его, не размонтируя, чтобы
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
medium="$container/{MEDIUM_DIR}"
state="$container/{STATE_DIR}"
rw={RW_MOUNT}
upper="$rw/upper"
work="$rw/work"

[ -b /dev/mapper/{mapper_name} ] || fail "зашифрованный том не открыт"

mkdir -p "$container" "$lower" || fail "не создать точки монтирования"

{container_mount}

modprobe loop 2>/dev/null || true
modprobe squashfs 2>/dev/null || true
modprobe overlay 2>/dev/null || true
modprobe dm-verity 2>/dev/null || true

# Образ слота -- на разделе образов, под dm-verity (Э3b). Слот и корневой хеш
# -- из командной строки ядра: с Э4 их передаёт проверяющая ступень после
# проверки подписи выпуска. Без хеша корень не собирается вовсе: образ без
# проверки целостности -- это ровно то, от чего verity защищает.
slot=$(sed -n 's/.*poa\.slot=\([ab]\).*/\1/p' /proc/cmdline)
roothash=$(sed -n 's/.*poa\.roothash=\([0-9a-f]\{{64\}}\).*/\1/p' /proc/cmdline)
[ -n "$slot" ] || fail "в командной строке ядра не назван слот (poa.slot)"
[ -n "$roothash" ] || fail "в командной строке ядра нет корневого хеша образа (poa.roothash)"
[ -n "${{POA_IMAGES_UUID:-}}" ] || fail "носитель не назвал раздел образов"
images={IMAGES_MOUNT}
mkdir -p "$images" || fail "не создать точку монтирования образов"
poa_wait=0
while [ ! -b "/dev/disk/by-uuid/$POA_IMAGES_UUID" ] && [ "$poa_wait" -lt 30 ]; do
    sleep 1; poa_wait=$((poa_wait + 1))
done
mount -o ro "/dev/disk/by-uuid/$POA_IMAGES_UUID" "$images" || fail "не смонтировать раздел образов"
veritysetup open "$images/slot-$slot/{SLOT_IMAGE}" {VERITY_MAPPER} \
    "$images/slot-$slot/{SLOT_VERITY}" "$roothash" \
    || fail "образ слота $slot не прошёл проверку целостности"
mount -t squashfs -o ro /dev/mapper/{VERITY_MAPPER} "$lower" || fail "не подключить образ системы"

[ -d "$medium" ] || fail "в контейнере нет слоя носителя $medium"

# Верхний слой -- в оперативной памяти (LIVE-2.0): всё, что изменилось за
# сеанс, исчезает с выключением. Предел -- чтобы переполнение не съело память.
mkdir -p "$rw" || fail "не создать точку монтирования tmpfs"
mount -t tmpfs -o "mode=0755,size={RW_SIZE}" poa-rw "$rw" || fail "не смонтировать tmpfs"
mkdir -p "$upper" "$work" || fail "не создать каталоги слоя изменений"
__LABEL__
mount -t overlay poa-overlay \\
    -o "lowerdir=$medium:$lower,upperdir=$upper,workdir=$work" "$__ROOTMNT__" \\
    || fail "не собрать overlay"

# Постоянное состояние -- поверх своих каталогов. Если каталог в state пуст,
# в него сперва переносится то, что лежит в системе (например, сети,
# записанные при выпуске носителя), -- иначе монтирование спрятало бы их.
__BINDS__
# Нижний слой обязан уехать в новый корень вместе с остальным.
#
# Найдено прогоном 11.09.2026, и найдено дорого. Носитель загружался целиком:
# фраза, регистрация машины, switch_root, systemd доходил до graphical.target,
# -- а потом всё до единой службы падало с кодом 127, включая journald, и сеанс
# не поднимался. Код 127 -- это ENOENT, "файла нет", хотя в собранном корне все
# они на месте: проверено пересборкой того же overlay вручную.
#
# Причина: overlay держится на смонтированном squashfs, а он оставался в
# /run/poa/lower внутри initramfs. Контейнер переносился (строки ниже), нижний
# слой -- нет. Поэтому записи в верхний слой проходили, уже открытые файлы
# работали (оттого и жил PID 1, запущенный до перехода), а каждый новый exec
# из /usr получал ENOENT.
#
# Порядок важен: сперва нижний слой, потом контейнер. Оба лежат в /run/poa, и
# перенос родителя раньше ребёнка оставил бы ребёнка за бортом.
mkdir -p "$__ROOTMNT__{LOWER_MOUNT}"
mount --make-private "$lower" 2>/dev/null || true
mount -o move "$lower" "$__ROOTMNT__{LOWER_MOUNT}" 2>/dev/null || \\
    mount --move "$lower" "$__ROOTMNT__{LOWER_MOUNT}" 2>/dev/null || \\
    fail "не перенести образ системы в новый корень"

# Раздел образов -- тоже: на файле образа держится dm-verity нижнего слоя.
mkdir -p "$__ROOTMNT__{IMAGES_MOUNT}"
mount --make-private "$images" 2>/dev/null || true
mount -o move "$images" "$__ROOTMNT__{IMAGES_MOUNT}" 2>/dev/null || \
    mount --move "$images" "$__ROOTMNT__{IMAGES_MOUNT}" 2>/dev/null || \
    fail "не перенести раздел образов в новый корень"

# То же для tmpfs верхнего слоя: overlay держится и на нём.
mkdir -p "$__ROOTMNT__{RW_MOUNT}"
mount --make-private "$rw" 2>/dev/null || true
mount -o move "$rw" "$__ROOTMNT__{RW_MOUNT}" 2>/dev/null || \\
    mount --move "$rw" "$__ROOTMNT__{RW_MOUNT}" 2>/dev/null || \\
    fail "не перенести слой изменений в новый корень"

# Контейнер должен остаться доступным изнутри системы: на нём лежит и образ, и
# слой носителя, и состояние, и размонтировать его нельзя, пока система работает.
mkdir -p "$__ROOTMNT__{CONTAINER_MOUNT}"
mount -o move "$container" "$__ROOTMNT__{CONTAINER_MOUNT}" 2>/dev/null || \\
    mount --move "$container" "$__ROOTMNT__{CONTAINER_MOUNT}" 2>/dev/null || true
"""
    if selinux:
        # Метка корня: корень overlay берёт инод от upperdir, а он создан здесь,
        # в initramfs, без метки -- иначе `/` получит unlabeled_t (найдено
        # прогоном 12.09.2026 для прежнего верхнего слоя на ext4).
        label = (f'setfattr -n security.selinux -v "{ROOT_CONTEXT}" "$upper" 2>/dev/null || '
                 'echo "poa-overlay: метка корня не поставлена" >&2')
    else:
        label = ""
    binds = []
    for name, path in STATE_BINDS:
        binds.append(
            f'mkdir -p "$state/{name}" "$__ROOTMNT__/{path}" && {{ '
            f'[ -n "$(ls -A "$state/{name}" 2>/dev/null)" ] || '
            f'cp -a "$__ROOTMNT__/{path}/." "$state/{name}/" 2>/dev/null || true; '
            f'mount -o bind "$state/{name}" "$__ROOTMNT__/{path}" || '
            f'echo "poa-overlay: состояние {name} не подключено" >&2; }}'
        )
    script = script.replace("__LABEL__", label).replace("__BINDS__", "\n".join(binds))
    return script.replace("__ROOTMNT__", rootmnt_var)


def fstab_rows() -> list[tuple[str, str, str, str, str, str]]:
    """Строки fstab, которые нужны системе поверх overlay.

    Корень в fstab не описывается: он собран initramfs до того, как fstab
    прочитан, и запись о нём заставила бы systemd пытаться перемонтировать
    то, чего в его понимании не существует.
    """
    return []
