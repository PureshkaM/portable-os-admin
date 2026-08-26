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

HERE=$(dirname "$0")
[ -f "$HERE/env" ] && . "$HERE/env"
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

# The request the user reads out to the administrator. Same shape as
# recovery/request.py RecoveryRequest.encode(): media_id:slot:checksum.
checksum=$(printf '%s' "${POA_MEDIA_ID}:${POA_RECOVERY_SLOT}" | sha256sum | cut -c1-8)

cat <<EOF

======================================================================
 Доступ к системе заблокирован: пароль введён неверно слишком много раз.

 Сообщите администратору этот запрос на восстановление:

     ${POA_MEDIA_ID}:${POA_RECOVERY_SLOT}:${checksum}

 Администратор выдаст одноразовый код восстановления.
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
    if cryptsetup luksOpen --test-passphrase --key-slot "$POA_RECOVERY_SLOT" \
            --key-file "$RECOVERY_KEY" "$device" 2>/dev/null; then
        accepted="yes"
        break
    fi
    echo "Код не подошёл." >&2
    attempt=$((attempt + 1))
done

[ -n "$accepted" ] || fail "код восстановления не принят; обратитесь к администратору"

while :; do
    printf 'Задайте новый пароль для входа: '
    stty -echo 2>/dev/null || true
    read -r new_password
    stty echo 2>/dev/null || true
    echo
    printf 'Повторите новый пароль: '
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
cryptsetup luksKillSlot "$device" "$POA_RECOVERY_SLOT" --batch-mode \
    --key-file "$NEW_KEY" \
    || fail "не удалось удалить использованный recovery keyslot"

cleanup
echo
echo "Доступ восстановлен. Система будет перезагружена."
sleep 3
reboot -f 2>/dev/null || echo b > /proc/sysrq-trigger
