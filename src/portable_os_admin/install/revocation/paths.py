"""Где что лежит на носителе и как часто спрашивать сервер.

Один модуль на все имена файлов и параметры: их называют и генератор скрипта,
и юниты, и шлюз, и сборка набора файлов. Разъехавшись по этим модулям, они
разъехались бы и по значениям -- служба писала бы в один путь, а шлюз читал
другой, и увидеть это можно было бы только живой загрузкой.
"""
from __future__ import annotations

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
REVOKED_RUNTIME_FILE = f"{STATE_DIR}/media-revoked"
REVOKED_FILE = "/var/lib/poa/media-revoked"

#: Как часто спрашивать сервер. Пять минут -- решение пользователя 15.09.2026.
POLL_INTERVAL = "5min"

#: Журнал -- на томе журналов, как и у автообновления: администратор читает его
#: своим ключом, не зная парольной фразы пользователя.
LOG_PATH = "/var/log/poa-revocation.log"

#: Чьим именем подписаны уведомления.
NOTIFY_APP = "Переносная система"
