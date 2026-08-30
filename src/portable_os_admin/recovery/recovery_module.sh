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
    # Носитель собран версией, где общего слоя ещё не было. Текст пойдёт в
    # консоль -- по-русски он там читается блоками, но путь наружу остаётся.
    poa_ui_active() { return 1; }
    poa_say() { echo "$1" > /dev/console 2>/dev/null || echo "$1"; }
    poa_ask() {
        printf '%s ' "$1" > /dev/console 2>/dev/null || printf '%s ' "$1"
        read -r poa_reply < /dev/console 2>/dev/null || read -r poa_reply
        printf '%s' "$poa_reply"
    }
    poa_ask_secret() {
        printf '%s ' "$1" > /dev/console 2>/dev/null || printf '%s ' "$1"
        stty -echo 2>/dev/null || true
        read -r poa_reply < /dev/console 2>/dev/null || read -r poa_reply
        stty echo 2>/dev/null || true
        printf '%s' "$poa_reply"
    }
fi
SALT_FILE="${POA_SALT_FILE:-$HERE/recovery_salt.bin}"

RECOVERY_KEY=/run/poa-recovery.key
NEW_KEY=/run/poa-new.key

cleanup() {
    rm -f "$RECOVERY_KEY" "$NEW_KEY"
}
trap cleanup EXIT INT TERM

fail() {
    # Через общий слой: stderr уходит в консоль, которую перекрыл plymouth, и
    # человек видел бы чёрный экран вместо причины отказа.
    poa_say "Ошибка восстановления: $1"
    sleep 10
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

# Одно сообщение, а не четыре. plymouth показывает их в одном месте, и каждое
# новое перекрывает предыдущее: на прогоне 29.08.2026 до человека дошло только
# последнее -- «администратор выдаст код», -- а сам код запроса, который надо
# продиктовать, он так и не увидел. Восстановление от этого становится
# бесполезным: звонить есть зачем, а сказать нечего.
poa_say "${trouble} ${promise}"

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
    # Ввод виден: код переписывают с бумажки, и человек должен видеть, что
    # набрал. Через общий слой -- консоль в этот момент перекрыта plymouth.
    # Код запроса -- в самом приглашении, а не отдельным сообщением: так он
    # остаётся на экране всё время, пока человек диктует его по телефону и
    # набирает ответ. Отдельное сообщение к этому моменту уже перекрыто.
    recovery_code=$(poa_ask "Продиктуйте администратору код ${request} и введите ответ ($attempt из 3):")
    ( umask 077; derive_passphrase "$recovery_code" > "$RECOVERY_KEY" )
    if cryptsetup luksOpen --test-passphrase --key-slot "$recovery_slot" \
            --key-file "$RECOVERY_KEY" "$device" 2>/dev/null; then
        accepted="yes"
        break
    fi
    poa_say "Код не подошёл."
    attempt=$((attempt + 1))
done

[ -n "$accepted" ] || fail "код восстановления не принят; обратитесь к администратору"

# Машинные слоты уничтожаются в обоих случаях, и по одной причине.
#
# Ключ машины выводится из фразы и отпечатка: `sha256(фраза ‖ отпечаток)`.
# Восстановление меняет фразу -- и все записанные машинные ключи разом
# перестают ей соответствовать. Слоты при этом остаются занятыми, но не
# узнаются больше никем: носитель на две машины после одного восстановления
# оказывался исчерпан, и та же самая машина, с которой человек только что
# работал, встречала его словами «этот компьютер незнаком, занять место 2 из
# 2». Проверено загрузкой 31.08.2026.
#
# Мёртвый ключ не защищает ничего и не открывает ничего -- он только занимает
# место, поэтому удаляется вместе с фразой, которая его породила.
#
# Текущая машина здесь НЕ регистрируется, хотя человек звонил администратору
# именно ради неё. Регистрация спрашивает разрешение, и задавать этот вопрос
# посреди восстановления значило бы получить «да» не глядя. После перезагрузки
# он ответит на него штатно, увидев, сколько слотов занимает.
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
[ "$killed" -gt 0 ] && poa_say "Записанные компьютеры забыты: $killed. Их нужно записать заново."

# Дальше оба случая идут одним путём, и это не упрощение ради краткости.
#
# `kind` существует только затем, чтобы объяснить человеку, почему он звонит
# администратору: забыл пароль или носитель занят другими компьютерами. Что
# делать дальше -- одно и то же: открыть том кодом, забыть записанные
# компьютеры, задать пароль заново. Для администратора эти обращения тоже
# ничем не отличаются: он выдаёт код и всё.
#
# Пароль спрашивается в обоих случаях, потому что регистрационный слот в
# обоих случаях создаётся заново -- в первом взамен забытого, во втором
# взамен уничтоженного при занятии последнего места.
while :; do
    new_password=$(poa_ask_secret "Задайте пароль для входа")
    repeat_password=$(poa_ask_secret "Повторите пароль")
    if [ -n "$new_password" ] && [ "$new_password" = "$repeat_password" ]; then
        break
    fi
    poa_say "Пароли не совпали или пусты, попробуйте ещё раз."
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
poa_say "Записываю новый пароль, это займёт около минуты…"

cryptsetup luksAddKey "$device" "$NEW_KEY" \
    --pbkdf-memory 262144 --pbkdf-parallel 2 --pbkdf-force-iterations 4 \
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
# Перезагрузка, а не продолжение загрузки: заголовок изменился под уже
# работающим initramfs, и регистрация этой машины должна пройти обычным путём
# -- с вопросом и со счётчиком мест, который она увидит заново.
if [ "$kind" = machines ]; then
    poa_say "Готово. Носитель снова принимает любые компьютеры, сейчас перезагрузка."
else
    poa_say "Пароль изменён. Сейчас перезагрузка."
fi
# Успеть прочитать: сразу за этим экран гаснет и машина уходит в перезагрузку.
sleep 8
reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger
