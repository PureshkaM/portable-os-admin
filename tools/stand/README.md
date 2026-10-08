# Стенд LIVE-2.0 (ВМ `posvm`)

Копии вспомогательных скриптов со стенда — чтобы новая сессия знала, чем и как
там проверяется работа. Рабочие экземпляры лежат на самой ВМ (пути ниже); здесь
они для чтения и восстановления. Проверка функций — **через приложение** (GUI под
Xvfb), скрипты сборки и записи — диагностика и подготовка стенда, не доказательство.

## Что где

| На стенде | Здесь | Назначение |
|---|---|---|
| `/root/poa-live2/` | — | рабочая копия репозитория (`rsync -a src tests packaging posvm:/root/poa-live2/`); тесты и GUI идут **отсюда** (`PYTHONPATH=src`), а не из установленного пакета |
| `/root/poa-live2/e9b.py` | `e9b.py` | `setup` / `build <debian\|redos> <каталог>` / `deploy <debian\|redos> <каталог>` — сборка дистрибутива и запись носителя-файла `/root/e8-<семейство>.img` (14 ГБ, разреженный) |
| `/root/poa-live2/e9root.py` | `e9root.py` | корень организации (резервный, `/root/e9-run/reserve.poaroot`) и сертификат контейнеру — для подписанных выпусков |
| `/root/poa-live2/e9pub.py` | `e9pub.py` | опубликовать дистрибутив выпуском на сервер (то же есть в GUI) |
| `/root/gui/run-live2.sh` | `run-live2.sh` | GUI из `/root/poa-live2/src` под Xvfb `:99` |
| `/root/gui/fill.sh X Y "текст"` | `fill.sh` | ввод в поле Tk (очистка End+BackSpace: Ctrl+A в Tk — «в начало строки») |
| `/root/gui/shot.sh имя` | `shot.sh` | снимок Xvfb в `/root/gui/shots/` |
| `/root/gui/boot.sh` | `boot.sh` | QEMU-загрузка носителя: `start <img> rw m1 net`, `type`, `enter`, `click X Y` (экран 1280×800), `shot`, `stop`; монитор `/tmp/poa-boot-mon.sock` (`system_powerdown`) |
| `/root/e8-totp.py <media>` | `e8-totp.py` | текущий код TOTP носителя (из базы стенда) |
| `/root/gui/stand-up.sh` + `poa-stand.service` | то же | после перезагрузки ВМ: `10.0.2.2/32` на `lo`, цель `poa-tgt` 10.99.0.1, сервер доступа `e6srv` |
| `/etc/nginx/sites-enabled/poa-stand` | `nginx-poa-stand.conf` | сервер на 8443 (TLS) |
| `/etc/nginx/sites-enabled/poa-stand80` | `nginx-poa-stand80.conf` | порт 80 как в `install.sh` + режим «гостиничного портала» по флагу `/run/poa-stand-portal` |
| `/root/gui/fake-portal.py` | `fake-portal.py` | страница портала на 127.0.0.1:8088 (запускать вручную) |
| `/root/rutoken/` | `rutoken-probe.py` | Рутокен ЭЦП (проброшен в ВМ): библиотека Актива `lib/librtpkcs11ecp.so` (aarch64, в систему не ставилась), заголовки `include/`, зонд — `rutoken-probe.py info` (без PIN) / `check` (временные ключи на чипе, сверка с `poa_gost`); итоги — `docs/RUTOKEN.md` |

Прочее на стенде: контейнеры администратора `/root/e8-run/{debian,redos}.container`
(пароль `AdminContainer123`), фраза носителей `KioskTest12345`, пользователь целей
`rdpuser` / `RdpTest123`; цели — xrdp (3389), sshd (22), TigerVNC X509Plain
(`Xvnc :5 -rfbport 5905 -interface 10.99.0.1 …`, сертификат `/root/vnc/cert.pem`,
запускать вручную); OpenVPN-шлюз (`openvpn-server@poa-{udp,tcp}`, таблица nft
`inet poa_gateway`); PostgreSQL 17 с ролью `poa_test` (пароль `poa_test`, CREATEDB) —
для `POA_TEST_PG=postgresql://poa_test:poa_test@127.0.0.1/postgres`.

## Как проверять

- Тесты — порциями, на стенде под Xvfb:
  `cd /root/poa-live2 && PYTHONPATH=src xvfb-run -a python3 -m pytest -q -p no:cacheprovider tests/<файлы>`.
  Серверные — ещё и на PostgreSQL: тот же запуск с `POA_TEST_PG=…`.
- Живая загрузка: `e9b.py build` (Debian ≈20 мин, РЕД ОС ≈20–40) → `e9b.py deploy`
  или «Обновление» в окне носителя (запись выпуска в свободный слот — секунды) →
  `boot.sh start … rw m1 net` → фраза → (РЕД ОС: вопрос о привязке — латинское `y`)
  → окно «Подключение» → TOTP из `e8-totp.py`.
- Журнал носителя: вкладка «Журналы» в окне носителя (или `MediaActions.read_logs`
  для разбора уже найденного дефекта).
- grubenv носителя: `losetup -fP --show -r /root/e8-<…>.img`, `mount -o ro <loop>p1`,
  `grub-editenv …/EFI/poa/grubenv list`.

## Осторожно (каждое — из реального случая)

- **Тесты на стенде идут от root.** Тест, исполняющий shell-код носителя, глушит
  все разрушительные ветки: 03.10.2026 заглушка `poweroff … return 1` довела
  `halt_now` до `echo o > /proc/sysrq-trigger` и выключила стенд.
- **Не синхронизировать `src` во время сборки** — поздно подгружаемые модули
  смешают версии в одном дистрибутиве.
- **`pkill -f <имя>` в той же ssh-команде убивает саму команду**, если имя есть в
  её тексте: использовать шаблон вида `"[g]ui.app import run"`.
- **Не вкладывать heredoc с `EOF` в heredoc с `EOF`** — хвост выполнится как
  команды на маке.
- **`rm -rf` только по каталогам без монтирований** (проверка `/proc/mounts`);
  следы прерванной сборки убирать через «Обслуживание → Следы установок».
- **Место на диске** (59 ГБ): дистрибутив ≈1,1–2 ГБ, носитель-файл до 4 ГБ,
  сборке нужно ≈5 ГБ временно, контейнер nspawn с PostgreSQL ≈1,3 ГБ. 03.10 место
  кончилось посреди проверки кластера.
- **Рутокен: неверный PIN тратит одну из 10 попыток.** `rutoken-probe.py check` без
  `POA_TOKEN_PIN` пробует только заводской PIN и только если токен сам говорит, что он
  заводской; после смены PIN — задавать явно.
- Кириллица через `boot.sh type` (QEMU sendkey) не набирается — в загрузчике
  отвечать латиницей.
