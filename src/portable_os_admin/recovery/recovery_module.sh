#!/bin/sh
# recovery_module.sh -- reference implementation of the on-medium recovery flow
# (course-project UC-9 "Запрос кода восстановления доступа" and the second
# half of the activity diagram, Рисунок 3).
#
# This script is deployed to the boot partition at install time by
# install/backends/*.py and is meant to run from the custom boot environment
# (an initramfs/dracut or initramfs-tools hook) *instead of* the normal LUKS
# passphrase prompt, once the user-keyslot attempt limit has been exhausted.
#
# It intentionally never computes a scrypt-strength KDF itself (POSIX shell
# has no such primitive) -- it shells out to a small companion binary,
# `poa-recovery-kdf`, that must be built and bundled onto the boot partition
# alongside this script. That binary is NOT part of this repository yet; see
# docs/ARCHITECTURE.md "Known limitations" for why it is deliberately
# deferred and what its expected CLI contract is.
#
# Expected environment on entry:
#   $POA_MEDIA_ID       -- media_id written to the boot partition at install time
#   $POA_LUKS_DEVICE    -- e.g. /dev/sda2, the LUKS-formatted partition
#   $POA_RECOVERY_SLOT  -- keyslot number reserved for recovery at install time
#   $POA_USER_SLOT      -- keyslot number used for the day-to-day user password

set -eu

fail() {
    echo "Ошибка восстановления: $1" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "не найдена команда $1"
}

for cmd in cryptsetup sha256sum poa-recovery-kdf; do
    require_cmd "$cmd"
done

: "${POA_MEDIA_ID:?POA_MEDIA_ID не задан}"
: "${POA_LUKS_DEVICE:?POA_LUKS_DEVICE не задан}"
: "${POA_RECOVERY_SLOT:?POA_RECOVERY_SLOT не задан}"
: "${POA_USER_SLOT:?POA_USER_SLOT не задан}"

# "Recovery-модуль" / "Удалить пользовательский keyslot"
cryptsetup luksKillSlot "$POA_LUKS_DEVICE" "$POA_USER_SLOT" --batch-mode \
    || fail "не удалось отключить пользовательский keyslot"

# "Сформировать короткий запрос восстановления" -- mirrors
# recovery/request.py's RecoveryRequest.encode(): "media_id:slot:checksum"
checksum=$(printf '%s' "${POA_MEDIA_ID}:${POA_RECOVERY_SLOT}" | sha256sum | cut -c1-8)
request="${POA_MEDIA_ID}:${POA_RECOVERY_SLOT}:${checksum}"

echo "======================================================================"
echo " Система заблокирована. Передайте администратору запрос восстановления:"
echo
echo "   ${request}"
echo
echo "======================================================================"

printf 'Введите код восстановления, полученный от администратора: '
read -r recovery_code

# "Вычислить recovery-пароль через KDF" -- see docs/ARCHITECTURE.md for the
# poa-recovery-kdf CLI contract (code, salt-file on the boot partition, and
# the same scrypt parameters as crypto.recovery_kdf_params()).
recovery_salt_file="/boot/.poa/recovery_salt.bin"
[ -f "$recovery_salt_file" ] || fail "не найден файл соли recovery-пароля"
recovery_passphrase=$(poa-recovery-kdf "$recovery_code" "$recovery_salt_file" 64)

# "Разблокировать LUKS через recovery keyslot"
if ! printf '%s' "$recovery_passphrase" | cryptsetup luksOpen --test-passphrase \
        --key-slot "$POA_RECOVERY_SLOT" "$POA_LUKS_DEVICE"; then
    echo "Код восстановления неверен." >&2
    exit 2
fi

printf 'Задайте новый пользовательский пароль: '
stty -echo
read -r new_password
stty echo
echo

# "Добавить новый пользовательский keyslot"
printf '%s\n%s' "$recovery_passphrase" "$new_password" | \
    cryptsetup luksAddKey "$POA_LUKS_DEVICE" --key-slot "$POA_USER_SLOT" \
        --key-file - || fail "не удалось создать новый пользовательский keyslot"

# "Удалить использованный recovery keyslot" -- one-time use, per
# "Восстановление.RecoveryЗапись.Недоступна" on the admin side
cryptsetup luksKillSlot "$POA_LUKS_DEVICE" "$POA_RECOVERY_SLOT" --batch-mode \
    || fail "не удалось удалить использованный recovery keyslot"

echo "Доступ восстановлен. Перезагрузка..."
sleep 2
reboot
