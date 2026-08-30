#!/bin/sh
# recovery_module.sh -- the on-medium recovery flow (UC-9), run from the boot
# environment once the user has exhausted their password attempts.
#
# It is not started by hand. The initramfs integration installed alongside it
# runs this instead of leaving the user staring at "cryptsetup failed, bad
# password?": on initramfs-tools as scripts/local-top/poa-recovery, after
# cryptroot has given up; on dracut as a pre-mount hook.
#
# The recovery passphrase is derived from the recovery code with scrypt, using
# the same parameters as crypto.recovery_kdf_params(). Earlier versions shelled
# out to a companion binary `poa-recovery-kdf` that was never written, which is
# why this whole feature stayed deferred. It turned out not to be needed:
# `openssl kdf ... SCRYPT` computes the same value -- checked against
# crypto.derive_recovery_passphrase byte for byte -- and openssl exists on both
# families and can be copied into the initramfs.
#
# Environment comes from the `env` file the installer writes next to this
# script: POA_MEDIA_ID, POA_LUKS_UUID, POA_RECOVERY_SLOT, POA_USER_SLOT and the
# scrypt cost parameters the medium was built with.

set -eu

# Подстановка оболочки, а не dirname: в initramfs его нет. При set -eu это
# роняло модуль на первой же строке, беззвучно для всех, кроме консоли, --
# то есть путь наружу при забытом пароле не работал ни разу за всё время его
# существования. Найдено прогоном 28.08.2026 по строке
# «recovery_module.sh: line 24: dirname: command not found».
# Заодно снимается зависимость от того, какие утилиты собрал dracut.
HERE=${0%/*}
[ "$HERE" = "$0" ] && HERE=.
[ -f "$HERE/env" ] && . "$HERE/env"

# Общий слой вывода -- тот же, которым пользуются разблокировка и регистрация
# машины. Экран у носителя один; отличается только то, кто его вызывает.
# Без файла остаётся консоль: русский текст пойдёт блоками, но путь наружу
# сохранится.
if [ -f "$HERE/ui.sh" ]; then
    . "$HERE/ui.sh"
else
    poa_ui_active() { return 1; }
    poa_say() { echo "$1" > /dev/console 2>/dev/null || echo "$1"; }
fi
SALT_FILE="${POA_SALT_FILE:-$HERE/recovery_salt.bin}"

RECOVERY_KEY=/run/poa-recovery.key
NEW_KEY=/run/poa-new.key

cleanup() {
    rm -f "$RECOVERY_KEY" "$NEW_KEY"
}
trap cleanup EXIT INT TERM

fail() {
    echo "Ошибка восстановления: $1" >&2
    exit 1
}

for cmd in cryptsetup openssl sha256sum od; do
    command -v "$cmd" >/dev/null 2>&1 || fail "в загрузочной среде нет команды $cmd"
done

: "${POA_MEDIA_ID:?не задан POA_MEDIA_ID}"
: "${POA_LUKS_UUID:?не задан POA_LUKS_UUID}"
: "${POA_RECOVERY_SLOT:?не задан POA_RECOVERY_SLOT}"
: "${POA_USER_SLOT:?не задан POA_USER_SLOT}"
POA_SCRYPT_N="${POA_SCRYPT_N:-16384}"
POA_SCRYPT_R="${POA_SCRYPT_R:-8}"
POA_SCRYPT_P="${POA_SCRYPT_P:-1}"

device="/dev/disk/by-uuid/${POA_LUKS_UUID}"
[ -b "$device" ] || fail "не найден раздел с UUID ${POA_LUKS_UUID}"
[ -f "$SALT_FILE" ] || fail "не найден файл соли recovery-пароля"

slot_taken() {  # $1 -- номер слота
    cryptsetup luksDump "$device" 2>/dev/null | grep -qE "^[[:space:]]+$1: luks2"
}

# Что именно случилось, носитель определяет сам, а не спрашивает у человека.
#
# Регистрационный слот жив -- значит фраза открыла бы том на любой машине, и
# раз не открыла, дело в самой фразе: пользователь её забыл. Слот уничтожен --
# значит носитель занял все машинные слоты, и верная фраза здесь бессильна по
# устройству механизма: ключа для этой машины в заголовке нет.
#
# Для пользователя это два разных несчастья, и спрашивать «что у вас?» значило
# бы требовать от него понимания keyslot'ов LUKS.
if slot_taken "$POA_USER_SLOT"; then
    kind=password
    trouble="Пароль введён неверно слишком много раз."
    promise="Администратор выдаст одноразовый код восстановления."
else
    kind=machines
    trouble="Носитель уже занят другими компьютерами и на этом работать не будет."
    promise="Администратор выдаст код, чтобы очистить список компьютеров."
fi

# Первый ещё живой recovery-слот: коды расходуются по одному, и второй раз
# подряд один и тот же слот не подойдёт.
POA_RECOVERY_FIRST="${POA_RECOVERY_FIRST:-$POA_RECOVERY_SLOT}"
POA_RECOVERY_LAST="${POA_RECOVERY_LAST:-$POA_RECOVERY_SLOT}"
recovery_slot=""
slot="$POA_RECOVERY_FIRST"
while [ "$slot" -le "$POA_RECOVERY_LAST" ]; do
    if slot_taken "$slot"; then
        recovery_slot="$slot"
        break
    fi
    slot=$((slot + 1))
done
[ -n "$recovery_slot" ] || fail "коды восстановления исчерпаны; носитель придётся собрать заново"

# Строка запроса берётся готовой из таблицы, а не считается здесь. Кодирование
# живёт в recovery/request.py в единственном экземпляре: вторая реализация
# base32 и sha256 разошлась бы с первой при первой же правке, и молча --
# пользователь диктовал бы строку, которую администратор не может разобрать.
request=$(awk -v s="$recovery_slot" -v k="$kind" '$1 == s && $2 == k {print $3}' "$HERE/requests" 2>/dev/null)
[ -n "$request" ] || fail "на носителе нет строки запроса для слота $recovery_slot"

cat <<EOF

======================================================================
 Вход в систему невозможен. ${trouble}

 Сообщите администратору этот код:

     ${request}

 ${promise}
======================================================================

EOF

derive_passphrase() {
    # -binary so the result is the raw key rather than a hex listing: these are
    # the exact bytes crypto.derive_recovery_passphrase() produces.
    openssl kdf -keylen 64 \
        -kdfopt "pass:$1" \
        -kdfopt "hexsalt:$(od -An -v -tx1 "$SALT_FILE" | tr -d ' \n')" \
        -kdfopt "n:${POA_SCRYPT_N}" \
        -kdfopt "r:${POA_SCRYPT_R}" \
        -kdfopt "p:${POA_SCRYPT_P}" \
        -binary SCRYPT
}

# Three attempts, and nothing on the medium is touched until one of them
# succeeds. An earlier version of this script killed the user's keyslot on
# entry, which destroyed the password of anyone who had merely mistyped it and
# left them worse off than before recovery started.
accepted=""
attempt=1
while [ "$attempt" -le 3 ]; do
    printf 'Введите код восстановления (попытка %s из 3): ' "$attempt"
    read -r recovery_code
    ( umask 077; derive_passphrase "$recovery_code" > "$RECOVERY_KEY" )
    if cryptsetup luksOpen --test-passphrase --key-slot "$recovery_slot" \
            --key-file "$RECOVERY_KEY" "$device" 2>/dev/null; then
        accepted="yes"
        break
    fi
    echo "Код не подошёл." >&2
    attempt=$((attempt + 1))
done

[ -n "$accepted" ] || fail "код восстановления не принят; обратитесь к администратору"

# Сброс списка машин: уничтожаются все машинные слоты, и носитель возвращается
# в исходное состояние -- фраза снова открывает его где угодно, а машины он
# набирает заново по мере работы.
#
# Текущая машина здесь НЕ регистрируется, хотя человек звонил администратору
# именно ради неё. Регистрация спрашивает разрешение, и задавать этот вопрос
# посреди восстановления значило бы получить «да» не глядя. После перезагрузки
# он ответит на него штатно, увидев, сколько слотов занимает.
if [ "$kind" = machines ]; then
    POA_MACHINE_FIRST="${POA_MACHINE_FIRST:-1}"
    POA_MACHINE_LAST="${POA_MACHINE_LAST:--1}"
    killed=0
    slot="$POA_MACHINE_FIRST"
    while [ "$slot" -le "$POA_MACHINE_LAST" ]; do
        if slot_taken "$slot"; then
            cryptsetup luksKillSlot "$device" "$slot" --batch-mode \
                --key-file "$RECOVERY_KEY" 2>/dev/null \
                && killed=$((killed + 1))
        fi
        slot=$((slot + 1))
    done
    echo
    echo "Список компьютеров очищен (забыто: $killed)."
fi

while :; do
    if [ "$kind" = machines ]; then
        # Пароль тот же, что и был: пользователь его помнит -- он ведь и
        # позвонил потому, что пароль подходит, а машина не принимается.
        # Спросить его нужно затем, что регистрационный слот уничтожен и его
        # надо создать заново, а из чего -- знает только человек.
        printf 'Введите пароль от системы: '
    else
        printf 'Задайте новый пароль для входа: '
    fi
    stty -echo 2>/dev/null || true
    read -r new_password
    stty echo 2>/dev/null || true
    echo
    printf 'Повторите пароль: '
    stty -echo 2>/dev/null || true
    read -r repeat_password
    stty echo 2>/dev/null || true
    echo
    if [ -n "$new_password" ] && [ "$new_password" = "$repeat_password" ]; then
        break
    fi
    echo "Пароли не совпали или пусты, попробуйте ещё раз." >&2
done

( umask 077; printf '%s' "$new_password" > "$NEW_KEY" )

# Order matters: remove the old password first, then add the new one into the
# slot it freed, then drop the recovery slot. Doing it the other way round
# would leave the medium with two working passwords if the last step failed.
#
# При сбросе списка машин этого слота уже нет -- он уничтожается в тот момент,
# когда занимается последняя машина, и именно поэтому носитель сюда и попал.
# Отказ здесь ожидаем и не должен ронять восстановление.
cryptsetup luksKillSlot "$device" "$POA_USER_SLOT" --batch-mode \
    --key-file "$RECOVERY_KEY" 2>/dev/null || true

# Тот же потолок памяти, что и при создании тома (install/luks.py): слот,
# созданный здесь без ограничения, получил бы стоимость по этой машине — а
# носитель переносной, и следующая машина может оказаться слабее.
cryptsetup luksAddKey "$device" "$NEW_KEY" \
    --pbkdf-memory 262144 \
    --key-slot "$POA_USER_SLOT" --key-file "$RECOVERY_KEY" \
    || fail "не удалось создать новый пользовательский keyslot"

# One-time use: the recovery keyslot goes as soon as it has done its job, which
# is the medium-side half of "Восстановление.RecoveryЗапись.Недоступна".
#
# Authorised with the *new* password, not with the recovery key: cryptsetup
# refuses to destroy a keyslot using the very key that slot holds ("Key is not
# available with this passphrase"), which is why this step failed the first
# time it was run against a real container.
cryptsetup luksKillSlot "$device" "$recovery_slot" --batch-mode \
    --key-file "$NEW_KEY" \
    || fail "не удалось удалить использованный recovery keyslot"

cleanup
echo
if [ "$kind" = machines ]; then
    # Перезагрузка, а не продолжение загрузки: заголовок изменился под уже
    # работающим initramfs, и регистрация этой машины должна пройти обычным
    # путём -- с вопросом и со счётчиком занятых слотов, который она увидит
    # заново.
    echo "Носитель снова принимает любые компьютеры. Система будет перезагружена,"
    echo "и при следующем запуске спросит разрешение записать этот компьютер."
else
    echo "Доступ восстановлен. Система будет перезагружена."
fi
sleep 5
reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger
