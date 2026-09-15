"""Удалённый отзыв носителя и шлюз сеанса.

Задача, ради которой это существует: уволившийся сотрудник с невозвращённым
носителем и уже зарегистрированной машиной сохраняет доступ, и отменить это
нечем. Привязка к машинам закрывает только чужие машины, но не бывшего своего.

Механизм (протокол v2, `docs/RELEASE-1.1.md`). Носитель раз в пять минут и при
каждом подъёме сети спрашивает общедоступный сервер о своём состоянии. Запрос
подписан **ключом носителя** -- без подписи сервер отвечает 403 и ничего не
сообщает. Сервер решает и отвечает подписанной строкой «ok» или «revoked»;
носитель лишь проверяет подлинность ответа и исполняет его.

**Шлюз сеанса.** Пока сервер в этой загрузке не ответил «ok», разрешённые
приложения киоска не запускаются: каждый их запускатель идёт через
`poa-session-gate`, а тот смотрит на `/run/poa/authorized`, который пишет
только эта служба от root. Настройки сети шлюзом не закрыты -- без них до
сервера не дойти. После первого «ok» потеря связи ничего не закрывает
(решение 15.09.2026: обрыв Wi-Fi в дороге не должен отбирать VPN), а
пользователь получает системное уведомление.

Так закрыт обход, при котором домен сервера перекрывается на домашнем
роутере: раньше «ресурс недоступен» означало «работаем дальше», теперь -- «до
приложений не дойти». Не закрыт тот, кто открывает том сам, в обход системы
носителя: фраза плюс отпечаток своей машины дают ключ слота. Против него
отзыв на стороне носителя бессилен, и настоящая точка отзыва для него --
учётная запись и сертификат VPN в компании. Сказано в README, «Границы».

**Уничтожает себя, а не только заголовок.** Сперва здесь стоял один
`luksErase` системного тома. Живая проверка 14.09.2026 показала, чего это не
покрывает, и требование было поставлено прямо -- после отзыва носитель
непригоден безвозвратно:

* тома журналов и обмена -- отдельные контейнеры со своими ключами, их
  `luksErase` системного тома не касается вовсе;
* заголовок остаётся читаемым, а у того, кто носитель не вернул, могла заранее
  осесть его копия (`luksHeaderBackup` доступен всякому с правами root на своей
  же машине). Копия плюс известная пользователю фраза -- это восстановленный
  доступ. Отсюда затирание самих данных -- единственное, что копию обесценивает;
* носитель остаётся загрузочным, то есть выглядит «сломавшимся», а не
  отозванным.

Затирание сплошняком на USB идёт часами и прерывается выдёргиванием флешки,
поэтому проход двойной: сплошь по началу тома и дальше по мегабайту через
каждые 32. Порядок шагов -- строго от главного к второстепенному: прерывание
на любом из них уже не оставляет доступа к данным.

**Ответ подписан ключом сервера, а не доверием к удостоверяющим центрам.**
Ключ рождается на сервере при его установке -- сервер разворачивают раньше,
чем появляется приложение, -- приложение забирает открытую часть при
знакомстве и кладёт её на каждый носитель. Цепочка УЦ лежит на носителе, то
есть в руках того, у кого носитель, и доказательством служить не может.
Проверка идёт `openssl`, а не питоном: скрипт работает до входа пользователя и
не должен зависеть от интерпретатора.

**Запрос несёт случайное число, и оно же подписано в ответе.** Старый ответ
«носитель в порядке», снятый месяц назад, подсунуть нельзя -- в нём другое
число. Часов для этого не нужно, а именно им на переносной системе доверять
нельзя.
"""
from __future__ import annotations

from ..config.schema import InstallConfig

SCRIPT_PATH = "usr/local/sbin/poa-revocation"
SERVICE_PATH = "etc/systemd/system/poa-revocation.service"
SERVICE_NAME = "poa-revocation.service"
TIMER_PATH = "etc/systemd/system/poa-revocation.timer"
TIMER_NAME = "poa-revocation.timer"
#: Скрипт диспетчера NetworkManager: сеть поднялась -- спросить сразу, а не
#: через пять минут. Иначе Wi-Fi, подключённый уже в сеансе, держал бы
#: приложения закрытыми до следующего срабатывания таймера.
DISPATCHER_PATH = "etc/NetworkManager/dispatcher.d/90-poa-revocation"
#: Шлюз сеанса: через него идут запускатели разрешённых приложений киоска.
GATE_PATH = "usr/local/bin/poa-session-gate"

#: Открытый ключ **сервера** отзыва в PEM: им проверяется подпись ответа.
#: Кладётся при записи носителя; закрытая часть существует только на сервере.
PUBLIC_KEY_PATH = "etc/poa/revocation.pub"
#: Закрытый ключ **носителя**: им подписывается запрос. Рождается при записи,
#: лежит на зашифрованном корне, 0400 root -- пользователь киоска его не
#: прочитает. Открытая часть уходит в запись носителя в контейнере и на сервер.
DEVICE_KEY_PATH = "etc/poa/device.key"

#: Каталог состояния в памяти. `/run` -- tmpfs, поэтому авторизация сама
#: исчезает с перезагрузкой; `boot_id` в файле -- вторая страховка.
STATE_DIR = "/run/poa"
AUTHORIZED_FILE = f"{STATE_DIR}/authorized"

#: Как часто спрашивать сервер. Пять минут -- решение пользователя 15.09.2026.
POLL_INTERVAL = "5min"

#: Журнал -- на томе журналов, как и у автообновления: администратор читает его
#: своим ключом, не зная парольной фразы пользователя.
LOG_PATH = "/var/log/poa-revocation.log"

#: Чьим именем подписаны уведомления.
NOTIFY_APP = "Переносная система"


def is_enabled(config: InstallConfig) -> bool:
    settings = getattr(config, "revocation", None)
    return bool(settings and settings.enabled and settings.url.strip())


def gate_enabled(config: InstallConfig) -> bool:
    """Закрывать ли приложения шлюзом.

    Только в режиме киоска: в консольном режиме запускать через `.desktop`
    нечего, а закрывать консоль значило бы оставить носитель без входа.
    """
    from . import kiosk

    return is_enabled(config) and kiosk.is_enabled(config)


def _version() -> str:
    from .. import __version__

    return __version__


def revocation_script(
    config: InstallConfig,
    media_id: str,
    luks_uuid: str,
    logs_luks_uuid: str = "",
    exchange_luks_uuid: str = "",
    esp_uuid: str = "",
    boot_uuid: str = "",
) -> str:
    """Скрипт проверки. Идентификатор, адрес и UUID томов вписаны в текст.

    UUID служебных томов и загрузочных разделов приходят необязательными: в
    старых раскладках их может не быть, и отсутствие означает «этот шаг
    пропустить», а не «ничего не уничтожать». Системный том -- обязателен.

    То, что никогда не попадает в командную строку, нельзя закавычить
    неправильно -- тем же приёмом собран скрипт режима киоска.

    **Том ищется по UUID заголовка LUKS, а не по пути раздела.** До 14.09.2026
    в скрипт вписывался путь, каким раздел назывался на машине администратора
    -- `/dev/loop1p3`. На загруженном носителе такого устройства нет, и живая
    проверка отзыва остановилась на «Device /dev/loop1p3 does not exist». Хуже:
    на чужой машине путь `/dev/sdb3` может принадлежать постороннему диску, а
    `luksErase` необратим. Поэтому устройство ищется по UUID и перед
    уничтожением сверяется ещё раз.

    Порядок шагов уничтожения -- не оформительский: питание могут выдернуть на
    любом из них, поэтому сперва ключи системного тома и тома обмена, потом
    данные, и только затем загрузочная часть и разметка. Том журналов --
    предпоследним, потому что до этого в него пишется каждый шаг.
    """
    from .machine_binding import fingerprint_functions

    settings = config.revocation
    url = settings.url.strip().rstrip("/")
    timeout = max(1, int(settings.timeout_seconds or 10))

    return f"""#!/bin/sh
# generated by portable-os-admin
# Проверка отзыва носителя: раз в {POLL_INTERVAL} и при подъёме сети.
#
# Сервер решает, носитель исполняет. Но ответ принимается, только если его
# подпись сходится с ключом сервера и в нём наше случайное число: иначе
# уничтожить носитель мог бы всякий, кто подменил ответ по дороге.

LOG={LOG_PATH}
MEDIA_ID={media_id}
URL={url}
SERVER_KEY=/{PUBLIC_KEY_PATH}
DEVICE_KEY=/{DEVICE_KEY_PATH}
VERSION={_version()}
LUKS_UUID={luks_uuid}
LOGS_UUID={logs_luks_uuid}
EXCHANGE_UUID={exchange_luks_uuid}
ESP_UUID={esp_uuid}
BOOT_UUID={boot_uuid}
TIMEOUT={timeout}
STATE_DIR={STATE_DIR}
AUTHORIZED={AUTHORIZED_FILE}

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}}

say() {{
    echo "$1" > /dev/console 2>/dev/null || echo "$1"
}}

# Уведомление в графический сеанс. Служба работает от root, а уведомления
# живут на сессионной шине пользователя -- поэтому от его имени и в его шину.
# Нет сеанса (загрузка ещё идёт) -- уведомлять некого, и это не ошибка: о
# закрытом сеансе пользователю скажет сам шлюз при попытке запуска.
notify_user() {{
    command -v notify-send >/dev/null 2>&1 || {{
        log "нет notify-send, уведомление не показано: $2"
        return 0
    }}
    for _bus in /run/user/*/bus; do
        [ -S "$_bus" ] || continue
        _uid=${{_bus#/run/user/}}
        _uid=${{_uid%/bus}}
        [ "$_uid" -ge 1000 ] 2>/dev/null || continue
        _user=$(getent passwd "$_uid" | cut -d: -f1)
        [ -n "$_user" ] || continue
        runuser -u "$_user" -- env DBUS_SESSION_BUS_ADDRESS="unix:path=$_bus" \\
            notify-send -a "{NOTIFY_APP}" -u "$1" "$2" "$3" >/dev/null 2>&1 || true
    done
}}

# Сервер не ответил так, чтобы ответу можно было верить. Сеанс остаётся
# таким, каким был: закрытым до первого «ok», открытым после.
no_answer() {{
    log "$1"
    : > "$STATE_DIR/unreachable"
    notify_user critical "Нет связи с сервером авторизации" "$2"
    exit 0
}}

mkdir -p "$STATE_DIR" && chmod 0755 "$STATE_DIR"

# Один проход за раз: таймер и диспетчер сети могут сработать вместе.
if command -v flock >/dev/null 2>&1; then
    exec 9>"$STATE_DIR/revocation.lock"
    flock -n 9 || exit 0
fi
# Уничтожение уже идёт -- второй проход ему только помешает.
[ -e "$STATE_DIR/destroying" ] && exit 0

if [ ! -f "$SERVER_KEY" ] || [ ! -f "$DEVICE_KEY" ]; then
    no_answer "нет ключа сервера или ключа носителя, проверка невозможна" \\
        "Носитель не может обратиться к серверу авторизации. Обратитесь к администратору."
fi

# Инструменты названы поимённо: без curl или openssl проверка не сработает ни
# разу, и строка в журнале превращает это из «молча» в «сказано, чего нет».
for tool in curl openssl base64 od; do
    command -v "$tool" >/dev/null 2>&1 || \\
        no_answer "нет $tool -- проверка отзыва невозможна" \\
            "Носитель не может обратиться к серверу авторизации. Обратитесь к администратору."
done

if ! ip route show default 2>/dev/null | grep -q .; then
    no_answer "сети нет, сервер не спрошен" \\
        "Подключитесь к сети: приложения станут доступны, когда сервер подтвердит носитель."
fi

WORK=$(mktemp -d) || exit 0
trap 'rm -rf "$WORK"' EXIT INT TERM

{fingerprint_functions()}

# Случайное число запроса. Ответ подписывается вместе с ним, поэтому
# вчерашний ответ сегодня не подойдёт.
NONCE=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \\n')
[ -n "$NONCE" ] || no_answer "не удалось получить случайное число" \\
    "Носитель не может обратиться к серверу авторизации. Обратитесь к администратору."

# Что носитель сообщает о себе. Сервер хранит это как учёт парка: какая
# версия, на какой машине и когда носитель последний раз выходил на связь.
MACHINE=$(poa_fingerprint_id | cut -c1-8)
INFO="version=$VERSION;machine=$MACHINE"

# Запрос подписывается ключом носителя. Без подписи сервер отвечает 403 и
# ничего не сообщает -- ни человеку с браузером, ни тому, кто перебирает
# идентификаторы.
printf 'poa-ask %s %s %s' "$MEDIA_ID" "$NONCE" "$INFO" > "$WORK/ask"
if ! openssl pkeyutl -sign -inkey "$DEVICE_KEY" -rawin \\
        -in "$WORK/ask" -out "$WORK/ask.sig" 2>>"$LOG"; then
    no_answer "запрос подписать не удалось" \\
        "Носитель не может обратиться к серверу авторизации. Обратитесь к администратору."
fi
ASK_SIGNATURE=$(base64 < "$WORK/ask.sig" | tr -d '\\n')

# Всё в заголовках, а не в адресе: адрес оседает в журналах посредников.
CODE=$(curl --silent --show-error --max-time "$TIMEOUT" \\
        --output "$WORK/answer" --write-out '%{{http_code}}' \\
        -H "X-POA-Media: $MEDIA_ID" \\
        -H "X-POA-Nonce: $NONCE" \\
        -H "X-POA-Info: $INFO" \\
        -H "X-POA-Signature: $ASK_SIGNATURE" \\
        "$URL/v2/state" 2>>"$LOG") || \\
    no_answer "сервер недоступен" \\
        "Сервер авторизации не отвечает. Приложения, уже открытые в этом сеансе, продолжают работать."

case "$CODE" in
    200) ;;
    403)
        no_answer "сервер отказал (403): носитель не признан" \\
            "Сервер авторизации не признал носитель. Обратитесь к администратору."
        ;;
    *)
        no_answer "сервер ответил кодом $CODE" \\
            "Сервер авторизации отвечает с ошибкой. Приложения, уже открытые в этом сеансе, продолжают работать."
        ;;
esac

# Ответ: две строки. Первая -- сообщение, вторая -- подпись в base64.
MESSAGE=$(sed -n '1p' "$WORK/answer")
SIGNATURE=$(sed -n '2p' "$WORK/answer")
if [ -z "$MESSAGE" ] || [ -z "$SIGNATURE" ]; then
    no_answer "ответ сервера непонятен" \\
        "Сервер авторизации ответил непонятно. Обратитесь к администратору."
fi

printf '%s' "$MESSAGE" > "$WORK/message"
printf '%s' "$SIGNATURE" | base64 -d > "$WORK/signature" 2>/dev/null || \\
    no_answer "подпись ответа не разбирается, ответ отброшен" \\
        "Сервер авторизации ответил непонятно. Обратитесь к администратору."

if ! openssl pkeyutl -verify -pubin -inkey "$SERVER_KEY" -rawin \\
        -in "$WORK/message" -sigfile "$WORK/signature" >/dev/null 2>&1; then
    no_answer "ПОДПИСЬ НЕ СХОДИТСЯ, ответ отброшен" \\
        "Ответ сервера авторизации не прошёл проверку подлинности. Обратитесь к администратору."
fi

# Подпись сошлась -- теперь проверяется, что подписан ответ именно на наш
# запрос. Без этого подошёл бы подписанный ответ про другой носитель или
# снятый месяц назад.
case "$MESSAGE" in
    "poa-state $MEDIA_ID $NONCE "*) ;;
    *)
        no_answer "ответ подписан, но не на наш запрос, отброшен" \\
            "Ответ сервера авторизации не прошёл проверку подлинности. Обратитесь к администратору."
        ;;
esac

STATE=${{MESSAGE##* }}

if [ "$STATE" = "ok" ]; then
    FIRST=""
    [ -f "$AUTHORIZED" ] || FIRST=1
    cat /proc/sys/kernel/random/boot_id > "$AUTHORIZED.tmp" 2>/dev/null \\
        && chmod 0644 "$AUTHORIZED.tmp" && mv -f "$AUTHORIZED.tmp" "$AUTHORIZED"
    if [ -n "$FIRST" ]; then
        log "состояние: ok, сеанс авторизован"
        notify_user normal "Сеанс авторизован" "Сервер подтвердил носитель, приложения доступны."
    elif [ -e "$STATE_DIR/unreachable" ]; then
        log "состояние: ok, связь восстановлена"
        notify_user normal "Связь с сервером авторизации восстановлена" "Носитель подтверждён."
    fi
    rm -f "$STATE_DIR/unreachable"
    exit 0
fi

if [ "$STATE" != "revoked" ]; then
    no_answer "неизвестное состояние в подписанном ответе: $STATE" \\
        "Сервер авторизации ответил непонятно. Обратитесь к администратору."
fi

# --- Дальше необратимое ----------------------------------------------------
#
# Порядок строго от главного к второстепенному: питание могут выдернуть на
# любом шаге, и на каждом промежуточном срезе доступа к данным уже не должно
# быть. Сперва ключи томов, затем сами данные, затем загрузочная часть, и лишь
# в самом конце -- разметка.

: > "$STATE_DIR/destroying"
rm -f "$AUTHORIZED"
log "состояние: revoked"
FAILED=0

find_volume() {{
    _uuid=$1
    [ -n "$_uuid" ] || return 1
    if [ -e "/dev/disk/by-uuid/$_uuid" ]; then
        readlink -f "/dev/disk/by-uuid/$_uuid"
        return 0
    fi
    if command -v blkid >/dev/null 2>&1; then
        _found=$(blkid -t UUID="$_uuid" -o device 2>/dev/null | head -1)
        if [ -n "$_found" ]; then
            echo "$_found"
            return 0
        fi
    fi
    return 1
}}

# Затирание начала устройства. Заголовок LUKS2 -- это первые 16 МиБ (две копии
# заголовка и слоты ключей); берём с запасом.
wipe_head() {{
    dd if=/dev/zero of="$1" bs=1M count="$2" conv=fsync >/dev/null 2>&1
}}

# Результат кладётся в VOLUME_DEV, а не печатается: подстановка $(...) увела бы
# функцию в подоболочку, и счёт ошибок в FAILED пропал бы вместе с ней.
destroy_luks() {{
    VOLUME_DEV=""
    _uuid=$1
    _name=$2
    if [ -z "$_uuid" ]; then
        log "$_name: UUID не записан в скрипт, шаг пропущен"
        return 1
    fi
    _dev=$(find_volume "$_uuid")
    if [ -z "$_dev" ]; then
        log "ОШИБКА: $_name -- том с UUID $_uuid не найден"
        FAILED=1
        return 1
    fi
    _found=$(cryptsetup luksUUID "$_dev" 2>/dev/null)
    if [ "$_found" != "$_uuid" ]; then
        log "ОШИБКА: $_name -- $_dev не тот том (UUID $_found), не трогаю"
        FAILED=1
        return 1
    fi
    if cryptsetup luksErase "$_dev" --batch-mode >>"$LOG" 2>&1; then
        log "$_name: слоты ключей уничтожены ($_dev)"
    else
        log "ОШИБКА: $_name -- luksErase не отработал ($_dev)"
        FAILED=1
    fi
    # Поверх -- нули: luksErase оставляет заголовок читаемым, а он рассказывает,
    # что на носителе было и чем зашифровано.
    if wipe_head "$_dev" 32; then
        log "$_name: заголовок затёрт ($_dev)"
    else
        log "ОШИБКА: $_name -- заголовок затереть не удалось ($_dev)"
        FAILED=1
    fi
    VOLUME_DEV=$_dev
    return 0
}}

# Затирание самих данных -- против заранее снятой копии заголовка: копия лежит
# вне носителя, отобрать её нельзя, обесценить можно только то, что она
# открывает.
wipe_data() {{
    _dev=$1
    _total=0
    if command -v blockdev >/dev/null 2>&1; then
        _bytes=$(blockdev --getsize64 "$_dev" 2>/dev/null)
        [ -n "$_bytes" ] && _total=$(( _bytes / 1048576 ))
    fi
    log "затираю первый гигабайт тома $_dev"
    dd if=/dev/zero of="$_dev" bs=1M count=1024 conv=fsync >/dev/null 2>&1
    if [ "$_total" -le 1024 ]; then
        log "том затёрт целиком"
        return 0
    fi
    # Дальше -- по мегабайту через каждые 32: ни файловая система, ни образ с
    # дырами такого шага не собираются, а времени уходит минуты, не часы.
    log "затираю остальное с шагом 32 МиБ (всего $_total МиБ)"
    _at=1024
    while [ "$_at" -lt "$_total" ]; do
        dd if=/dev/zero of="$_dev" bs=1M count=1 seek="$_at" conv=fsync >/dev/null 2>&1 || break
        _at=$(( _at + 32 ))
    done
    log "данные затёрты до отметки $_at МиБ"
}}

wipe_boot() {{
    _uuid=$1
    _name=$2
    if [ -z "$_uuid" ]; then
        log "$_name: UUID не записан в скрипт, шаг пропущен"
        return 0
    fi
    _dev=$(find_volume "$_uuid")
    if [ -z "$_dev" ]; then
        log "$_name: раздел не найден, шаг пропущен"
        return 0
    fi
    umount "$_dev" >/dev/null 2>&1
    if wipe_head "$_dev" 16; then
        log "$_name: загрузчик затёрт ($_dev)"
    else
        log "ОШИБКА: $_name -- затереть не удалось ($_dev)"
        FAILED=1
    fi
}}

log "НОСИТЕЛЬ ОТОЗВАН, уничтожаю"
notify_user critical "Носитель отозван администратором" \\
    "Доступ прекращён. Идёт уничтожение носителя, не выключайте компьютер."
say ""
say "Носитель отозван администратором. Доступ к данным прекращён."
say "Идёт уничтожение носителя, не выключайте машину."

# 1. Системный том -- главное. Уничтожается заголовок раздела, а не отображения:
#    luksErase работает с диском, и открытый сейчас том этому не мешает.
destroy_luks "$LUKS_UUID" "системный том"
SYSTEM_DEV=$VOLUME_DEV

# 2. Том обмена -- отдельный контейнер со своим ключом.
destroy_luks "$EXCHANGE_UUID" "том обмена"

# 3. Данные системного тома.
if [ -n "$SYSTEM_DEV" ]; then
    wipe_data "$SYSTEM_DEV"
fi

# 4. Загрузочная часть: отозванный носитель не должен грузиться вообще.
wipe_boot "$BOOT_UUID" "раздел /boot"
wipe_boot "$ESP_UUID" "раздел ESP"

# 5. Том журналов -- последним из томов: до этой строки каждый шаг писался
#    именно в него. Дальше журнал переезжает в память.
if [ -n "$LOGS_UUID" ]; then
    log "том журналов уничтожается следующим, дальше строки только в памяти"
    sync
    LOG=/run/poa-revocation.log
    destroy_luks "$LOGS_UUID" "том журналов"
fi

# 6. Разметка -- в самом конце и только от уже проверенного раздела. Затирается
#    и начало, и хвост: в хвосте лежит запасная копия GPT.
if [ -n "$SYSTEM_DEV" ] && command -v lsblk >/dev/null 2>&1; then
    PARENT=$(lsblk -no pkname "$SYSTEM_DEV" 2>/dev/null | head -1)
    if [ -n "$PARENT" ] && [ -b "/dev/$PARENT" ]; then
        dd if=/dev/zero of="/dev/$PARENT" bs=1M count=4 conv=fsync >/dev/null 2>&1
        DISK_MB=0
        if command -v blockdev >/dev/null 2>&1; then
            DISK_BYTES=$(blockdev --getsize64 "/dev/$PARENT" 2>/dev/null)
            [ -n "$DISK_BYTES" ] && DISK_MB=$(( DISK_BYTES / 1048576 ))
        fi
        if [ "$DISK_MB" -gt 8 ]; then
            dd if=/dev/zero of="/dev/$PARENT" bs=1M count=4 seek=$(( DISK_MB - 4 )) \\
                conv=fsync >/dev/null 2>&1
        fi
        log "разметка затёрта (/dev/$PARENT)"
    else
        log "родительский диск не определён, разметка оставлена как есть"
    fi
fi

sync

if [ "$FAILED" -eq 0 ]; then
    log "=== носитель уничтожен ==="
    say ""
    say "Носитель уничтожен. Для восстановления обратитесь к администратору."
else
    log "=== уничтожение прошло не полностью, см. строки ОШИБКА выше ==="
    say ""
    say "Отзыв доведён не до конца. Обратитесь к администратору."
fi

sleep 5
poweroff -f 2>/dev/null || echo o > /proc/sysrq-trigger
exit 0
"""


def service_unit() -> str:
    """Один проход проверки. Запускают его таймер и диспетчер сети.

    Предела времени нет намеренно: сетевые шаги ограничены своим
    `--max-time`, а уничтожение отозванного носителя длится столько, сколько
    длится затирание тома на USB, -- предел означал бы `SIGTERM` посреди него.
    """
    return f"""# generated by portable-os-admin
[Unit]
Description=Проверка отзыва переносного носителя
After=network-online.target
Wants=network-online.target
# Журнал службы лежит в /var/log, а это отдельный зашифрованный том носителя.
# Без этой строки служба успевает написать раньше, чем том смонтирован, и
# строки ложатся на корень под точку монтирования (13.09.2026).
RequiresMountsFor=/var/log

[Service]
Type=oneshot
ExecStart=/{SCRIPT_PATH}
TimeoutStartSec=infinity
"""


def timer_unit() -> str:
    """Опрос по расписанию.

    Первый проход вскоре после загрузки, дальше -- через `POLL_INTERVAL` после
    начала предыдущего. Таймер, а не цикл в службе: служба с циклом живёт
    вечно, и её падение означало бы, что проверок больше нет, а заметить это
    некому.
    """
    return f"""# generated by portable-os-admin
[Unit]
Description=Периодическая проверка отзыва переносного носителя

[Timer]
OnBootSec=20s
OnUnitActiveSec={POLL_INTERVAL}
AccuracySec=15s

[Install]
WantedBy=timers.target
"""


def dispatcher_script() -> str:
    """Сеть поднялась -- спросить сервер сразу.

    NetworkManager исполняет файлы из `dispatcher.d`, только если они
    принадлежат root и не доступны на запись никому больше; режим задаёт
    установщик. `--no-block`: диспетчер ждёт свои скрипты, а проверка может
    длиться до таймаута curl.
    """
    return f"""#!/bin/sh
# generated by portable-os-admin
# Подъём сети -- повод спросить сервер авторизации, не дожидаясь таймера.
case "$2" in
    up|connectivity-change|dhcp4-change|dhcp6-change)
        systemctl start --no-block {SERVICE_NAME} >/dev/null 2>&1 || true
        ;;
esac
exit 0
"""


def gate_script() -> str:
    """Шлюз, через который запускаются разрешённые приложения киоска.

    Проверяет одно: есть ли `/run/poa/authorized` с `boot_id` этой загрузки.
    Файл пишет служба отзыва от root в каталог, куда пользователь писать не
    может, -- подделать его изнутри сеанса нечем.

    `--wait` -- для автозапуска: он срабатывает при входе, раньше, чем сеть
    поднята и сервер спрошен. Отказать ему значило бы, что приложение из
    автозапуска не открывается никогда; ждать -- что открывается, как только
    сервер подтвердит носитель.
    """
    return f"""#!/bin/sh
# generated by portable-os-admin
# Шлюз сеанса: приложение запускается, только если сервер авторизации в этой
# загрузке подтвердил носитель.

AUTHORIZED={AUTHORIZED_FILE}

authorized() {{
    [ -f "$AUTHORIZED" ] || return 1
    [ "$(cat "$AUTHORIZED" 2>/dev/null)" = "$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)" ]
}}

if [ "${{1:-}}" = "--wait" ]; then
    shift
    while ! authorized; do
        sleep 5
    done
    exec "$@"
fi

if authorized; then
    exec "$@"
fi

if command -v notify-send >/dev/null 2>&1; then
    notify-send -a "{NOTIFY_APP}" -u critical "Сеанс не авторизован" \\
        "Приложения станут доступны, когда сервер авторизации подтвердит носитель. Проверьте подключение к сети." \\
        >/dev/null 2>&1 || true
fi
exit 1
"""


def config_files(
    config: InstallConfig,
    media_id: str = "",
    luks_uuid: str = "",
    logs_luks_uuid: str = "",
    exchange_luks_uuid: str = "",
    esp_uuid: str = "",
    boot_uuid: str = "",
) -> dict[str, tuple[str, int | None]]:
    """Файлы носителя. Пусто, пока отзыв не включён или носитель неизвестен.

    `media_id` и UUID приходят от установщика: они разные у каждого носителя,
    поэтому эти файлы пишутся при записи, а не попадают в образ дистрибутива --
    иначе все носители спрашивали бы сервер об одном и том же идентификаторе.
    Шлюз одинаков у всех, но пишется вместе с остальным: без службы, которая
    открывает его, он лишь запер бы приложения навсегда.
    """
    if not is_enabled(config) or not media_id or not luks_uuid:
        return {}
    script = revocation_script(
        config,
        media_id,
        luks_uuid,
        logs_luks_uuid=logs_luks_uuid,
        exchange_luks_uuid=exchange_luks_uuid,
        esp_uuid=esp_uuid,
        boot_uuid=boot_uuid,
    )
    files: dict[str, tuple[str, int | None]] = {
        SCRIPT_PATH: (script, 0o755),
        SERVICE_PATH: (service_unit(), 0o644),
        TIMER_PATH: (timer_unit(), 0o644),
        DISPATCHER_PATH: (dispatcher_script(), 0o755),
    }
    if gate_enabled(config):
        files[GATE_PATH] = (gate_script(), 0o755)
    return files


def new_device_key() -> tuple[str, str]:
    """Ключ носителя: (закрытая часть в PEM, открытая часть -- base64 сырых байт).

    Открытая часть -- в том виде, в каком её ждёт команда серверу
    (`poa-media <id> <номер> <состояние> <ключ>`): 44 знака, без переводов строк.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return private_pem, base64.b64encode(public_raw).decode("ascii")


# Службу здесь не включают. Это делает установщик ссылкой в
# `timers.target.wants`, рядом с записью самих файлов -- см.
# `OSInstaller._write_revocation_key`.
#
# Так вышло не сразу. Сперва включение жило в командах chroot и шло
# безусловно: сборка дистрибутива падала на «unit poa-revocation.service does
# not exist», потому что файлов там нет по замыслу. Признак исправили на
# «известен носитель» -- и служба перестала включаться на пути развёртывания
# готового дистрибутива, где команды chroot не выполняются вовсе: носитель
# РЕД ОС 14.09.2026 уехал со скриптом, ключом и без автозапуска.
