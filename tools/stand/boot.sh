#!/bin/sh
# Стенд загрузки носителя: запустить, посмотреть, понажимать, остановить.
#
# Хост arm64, поэтому это полная эмуляция x86-64 без KVM — всё медленно.
# По умолчанию snapshot=on: носитель не меняется, прогон можно повторять,
# и падение не испортит то, что собиралось час. Мышь — usb-tablet, чтобы
# координаты были абсолютными: с относительной по ярлыку не попасть.
#
# Третий аргумент start — какой машиной притвориться. Привязку носителя
# нельзя проверить на одной машине: нужна вторая, которую носитель обязан
# не узнать. Отпечаток считается из DMI платы и модели процессора, и DMI
# здесь задаётся ключами -smbios. Значения фиксированные, а не случайные:
# отпечаток обязан совпадать от загрузки к загрузке, иначе одна и та же
# машина занимала бы новый слот при каждом включении.
#
# Использование:
#   boot.sh start [устройство] [rw] [m1|m2|m3]   rw — писать по-настоящему
#   boot.sh shot имя
#   boot.sh key имя-клавиши...
#   boot.sh type текст
#   boot.sh pass фраза          ввод в последовательный порт
#   boot.sh click X Y
#   boot.sh watch [сколько-кадров] [пауза]
#   boot.sh stop
set -u
MON=/tmp/poa-boot-mon.sock
QMP=/tmp/poa-boot-qmp.sock
SER=/tmp/poa-boot-serial.sock
SERIN=/tmp/poa-boot-serial.in
OUT=/root/gui/boot-shots
VARS=/tmp/poa-boot-vars.fd
# Какой машиной запускались в последний раз — чтобы снимок и журнал можно
# было отнести к машине, не вспоминая, чем стартовали.
WHICH=/tmp/poa-boot-machine

mon() { printf "%s\n" "$*" | socat - "UNIX-CONNECT:$MON" >/dev/null 2>&1; }

# DMI одной машины. Возвращает готовые ключи -smbios.
smbios_for() {
    case "$1" in
        m1) v="POA-Stand"; n="Machine-One";   s="M1-0000001"; u="11111111-1111-1111-1111-111111111111" ;;
        m2) v="POA-Stand"; n="Machine-Two";   s="M2-0000002"; u="22222222-2222-2222-2222-222222222222" ;;
        m3) v="POA-Stand"; n="Machine-Three"; s="M3-0000003"; u="33333333-3333-3333-3333-333333333333" ;;
        *)  echo "неизвестная машина: $1 (m1|m2|m3)" >&2; return 1 ;;
    esac
    # type=1 несёт product_uuid, type=2 — поля платы. Отпечаток читает и то,
    # и другое, поэтому задаются обе структуры.
    printf -- "-smbios type=1,manufacturer=%s,product=%s,uuid=%s -smbios type=2,manufacturer=%s,product=%s,serial=%s" \
        "$v" "$n" "$u" "$v" "$n" "$s"
}

case "${1:-}" in
start)
    DEV=${2:-/dev/sda}
    MODE=${3:-ro}
    MACHINE=${4:-m1}
    # Пятый параметр net -- дать гостю сеть (user-mode, хост виден как 10.0.2.2).
    NETDEV=""
    [ "${5:-}" = "net" ] && NETDEV="-netdev user,id=net0 -device e1000,netdev=net0"
    pid=$(pgrep -f "[q]emu-system-x86_64 .*poa-boot-mon" || true)
    [ -n "$pid" ] && { echo "уже запущено (pid $pid)"; exit 0; }
    SMBIOS=$(smbios_for "$MACHINE") || exit 2
    mkdir -p "$OUT"; rm -f "$MON" "$QMP"
    cp /usr/share/OVMF/OVMF_VARS_4M.fd "$VARS"
    SNAP="snapshot=on"
    [ "$MODE" = "rw" ] && SNAP="snapshot=off"
    printf "%s\n" "$MACHINE" > "$WHICH"
    # shellcheck disable=SC2086 -- $SMBIOS обязан разбиться на отдельные слова
    qemu-system-x86_64 -machine q35 -m 3072 -smp 2 \
        -drive if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.fd \
        -drive if=pflash,format=raw,file="$VARS" \
        -drive file="$DEV",format=raw,if=none,id=stick,$SNAP \
        -device usb-ehci,id=ehci -device usb-storage,bus=ehci.0,drive=stick \
        -device usb-tablet \
        $NETDEV \
        $SMBIOS \
        -vga std -display none -vnc 127.0.0.1:2,password=on \
        -monitor "unix:$MON,server,nowait" \
        -qmp "unix:$QMP,server=on,wait=off" \
        -serial "unix:$SER,server=on,wait=off" \
        > "$OUT/qemu.log" 2>&1 &
    echo "qemu pid $! ($DEV, $SNAP, машина $MACHINE)"
    sleep 2
    # Пароль VNC задаётся сразу: без него подключиться нельзя, а password=on
    # оставляет его пустым до первой команды монитора.
    mon "set_password vnc poa2026"
    rm -f "$SERIN"; mkfifo "$SERIN"
    # Канал держится открытым фоновым писателем: иначе socat завершится
    # на первом же EOF и порт замолчит.
    ( sleep 100000 > "$SERIN" & )
    ( socat "UNIX-CONNECT:$SER" - < "$SERIN" > "$OUT/serial.log" 2>/dev/null & )
    ;;
machine)
    cat "$WHICH" 2>/dev/null || echo "не запускалось"
    ;;
shot)
    name=${2:-shot}
    rm -f "$OUT/$name.ppm"
    mon "screendump $OUT/$name.ppm"
    sleep 2
    if [ -f "$OUT/$name.ppm" ]; then
        convert "$OUT/$name.ppm" "$OUT/$name.png" && rm -f "$OUT/$name.ppm"
        echo "$OUT/$name.png"
    else
        echo "снять не удалось — машина запущена?"
        exit 1
    fi
    ;;
key)
    shift
    for k in "$@"; do mon "sendkey $k"; sleep 0.15; done
    echo "нажато: $*"
    ;;
type)
    text=${2:-}
    i=1; len=$(printf "%s" "$text" | wc -c)
    while [ "$i" -le "$len" ]; do
        ch=$(printf "%s" "$text" | cut -c"$i")
        case "$ch" in
            [a-z]|[0-9]) key=$ch ;;
            [A-Z]) key="shift-$(printf "%s" "$ch" | tr "A-Z" "a-z")" ;;
            "-") key="minus" ;; ".") key="dot" ;; "_") key="shift-minus" ;;
            " ") key="spc" ;; "/") key="slash" ;; ":") key="shift-semicolon" ;;
            "=") key="equal" ;; ",") key="comma" ;; ";") key="semicolon" ;;
            "|") key="shift-backslash" ;; "\\") key="backslash" ;;
            '"') key="shift-apostrophe" ;;
            "'") key="apostrophe" ;;
            ">") key="shift-dot" ;; "<") key="shift-comma" ;; "?") key="shift-slash" ;;
            "!") key="shift-1" ;; "@") key="shift-2" ;; "#") key="shift-3" ;;
            "$") key="shift-4" ;; "%") key="shift-5" ;; "^") key="shift-6" ;;
            "&") key="shift-7" ;; "*") key="shift-8" ;; "(") key="shift-9" ;;
            ")") key="shift-0" ;; "+") key="shift-equal" ;; "~") key="shift-grave_accent" ;;
            "[") key="bracket_left" ;; "]") key="bracket_right" ;;
            "{") key="shift-bracket_left" ;; "}") key="shift-bracket_right" ;;
            *) key="" ;;
        esac
        [ -n "$key" ] && mon "sendkey $key"
        i=$((i + 1)); sleep 0.12
    done
    echo "введено ($len симв.)"
    ;;
enter) mon "sendkey ret"; echo "ввод" ;;
click)
    # Через QMP, а не через монитор: HMP mouse_move шлёт относительные
    # события, и курсор упирается в угол экрана вместо заданной точки.
    # Из-за этого клики по ярлыкам не проверялись вовсе.
    x=${2:-0}; y=${3:-0}; w=${4:-1280}; h=${5:-800}; n=${6:-1}; e=${7:-}
    python3 /root/gui/qmp-click.py "$x" "$y" "$w" "$h" "$n" $e
    ;;
watch)
    # Снимать подряд, а не по расписанию: сообщения GRUB и ядра мелькают и
    # между двумя запланированными снимками теряются целиком. Пользователь
    # поймал так ошибку, которой у меня в снимках не было.
    count=${2:-60}
    every=${3:-1}
    mkdir -p "$OUT"
    i=1
    while [ "$i" -le "$count" ]; do
        name=$(printf "w%03d" "$i")
        rm -f "$OUT/$name.ppm"
        mon "screendump $OUT/$name.ppm"
        sleep 1
        if [ -f "$OUT/$name.ppm" ]; then
            convert "$OUT/$name.ppm" "$OUT/$name.png" 2>/dev/null && rm -f "$OUT/$name.ppm"
        fi
        i=$((i + 1))
        sleep "$every"
    done
    echo "снято кадров: $count"
    ;;
pass)
    # Ввод идёт в порт, а не клавишами: пароль спрашивают именно там.
    printf "%s\n" "${2:-}" > "$SERIN"
    echo "отправлено в порт"
    ;;
stop)
    mon "quit"; sleep 2
    pkill -f "[q]emu-system-x86_64 .*poa-boot-mon" 2>/dev/null
    echo "остановлено"
    ;;
*)
    echo "boot.sh start|machine|shot|key|type|pass|enter|click|watch|stop"; exit 2 ;;
esac
