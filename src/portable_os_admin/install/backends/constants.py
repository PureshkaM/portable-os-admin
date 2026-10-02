"""Пути и сроки, общие для обоих семейств.

Отдельным модулем, а не в `base.py`, по одной причине: генераторы текста
(`backends/text/*`) должны брать их, не импортируя класс бэкенда, иначе
получается круг -- класс зовёт генераторы, генераторы тянут класс. `base.py`
эти имена ре-экспортирует, потому что за них держатся установщик и тесты.
"""
from __future__ import annotations

# Where the two unencrypted service partitions end up inside the installed
# system. The log partition is mounted over /var/log on purpose: it is what
# makes the portable system's logs readable by the administrator later without
# the user's LUKS passphrase (see admin/logs.py).
LOG_MOUNTPOINT = "/var/log"
EXCHANGE_MOUNTPOINT = "/mnt/exchange"

#: Key file that opens the log and exchange volumes at boot. It lives on the
#: encrypted root, so it is only readable once the root has been unlocked with
#: the user's passphrase -- the volumes are not protected *from* that user,
#: they are protected from whoever finds the medium.
SERVICE_KEY_DIR = "etc/poa"
#: Сколько ждать появления тома службы, секунд.
#:
#: systemd по умолчанию ждёт устройство 90 секунд и снимает задание. Для тома
#: под LUKS это не запас, а ловушка: argon2id считается ровно столько, сколько
#: нужно машине, где носитель **загружают**, а стоимость записана в заголовок
#: на машине, где его **собирали**. Разница между рабочей станцией
#: администратора и слабым терминалом легко даёт десятикратное замедление.
#:
#: Наблюдалось на стенде: том журналов открылся на 164-й секунде, том обмена —
#: на 206-й, оба уже после того, как systemd перестал их ждать. /var/log не
#: смонтировался, auditd не запустился, и по симптомам это выглядело как
#: неверный ключ, хотя ключ был верный.
#:
#: 180 секунд ничего не стоят в обычном случае — ожидание кончается, как только
#: том открылся. Оплачивается это время только когда тома действительно нет.
SERVICE_DEVICE_TIMEOUT = 180

SERVICE_KEY_PATH = f"{SERVICE_KEY_DIR}/service.key"

#: Where the recovery module, its salt and its settings are placed inside the
#: installed system. They live on the encrypted root and are copied into the
#: initramfs when it is built, which is the only place they are needed: the
#: recovery flow runs before the root has been unlocked.
RECOVERY_DIR = "etc/poa/recovery"
#: Имя темы plymouth, которой носитель разговаривает с человеком до загрузки.
PLYMOUTH_THEME_NAME = "poa"
#: Where the same files end up inside the initramfs.
RECOVERY_INITRAMFS_DIR = "/poa"

#: Переменные выбора слота A/B на ESP (Э4): GRUB пишет только в FAT.
GRUB_ENV_DIR = "EFI/poa"
#: Сколько раз пробовать новый слот до возврата к проверенному.
BOOT_TRIES = 3
