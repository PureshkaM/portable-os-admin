#!/bin/sh
# Разворачивает ресурс отзыва одной командой.
#
#   sh install.sh --domain revoke.example.org
#
# Приложения на этот момент ещё нет, и это определяет весь порядок: сервер
# ставится первым и в одиночку. Он сам создаёт ключ отзыва, отдаёт его открытую
# часть по /pubkey и печатает одноразовый код знакомства. Дальше администратор
# в приложении вводит адрес сервера и этот код -- приложение забирает открытый
# ключ и отправляет свой. Больше никаких файлов между машинами носить не надо.
#
# Что делает: ставит зависимости, кладёт службу, заводит каталоги и ключ,
# включает TLS, поднимает всё это. Повторный запуск безопасен -- каждый шаг
# проверяет, сделан ли он уже, и существующий ключ не трогает.
#
# Чего не делает намеренно:
#
# * **Не поднимает ничего лишнего.** Одна служба на стандартной библиотеке
#   Python, sqlite вместо базы данных, nginx как приём TLS. Репозиторий пакетов
#   при желании живёт на этой же машине статическим каталогом -- см. --repo-root.
# * **Не заводит администраторов молча.** Первый приходит по коду знакомства,
#   код сгорает после него. Следующим код выписывает владелец сервера -- одной
#   строкой, она напечатана в конце установки.
set -eu

DOMAIN=""
KEY=""
REPO_ROOT=""
EMAIL=""
ORG=""
KIT_NAME=""
KIT_DIR=""

usage() {
    cat >&2 <<'USAGE'
Использование:
  install.sh --domain <имя> [параметры]

Обязательные:
  --domain <имя>            публичное имя, на которое берётся сертификат

Необязательные:
  --revocation-key <файл>   взять готовый закрытый ключ отзыва вместо нового
                            (нужно только при переносе сервера: носители носят
                            в себе открытую часть старого ключа)
  --repo-root <каталог>     раздавать этот каталог как /repo (репозиторий пакетов)
  --email <адрес>           адрес для Let's Encrypt (по умолчанию без адреса)
  --org <название>          название организации в сертификате CA для VPN
                            (по умолчанию -- имя домена)
  --gateway-kit <имя> <каталог>
                            только выпустить комплект для OpenVPN-шлюза с этим
                            именем (сертификат шлюза, CA, ключ tls-crypt-v2) и
                            выйти; сервер должен быть уже установлен
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN=$2; shift 2 ;;
        --revocation-key) KEY=$2; shift 2 ;;
        --repo-root) REPO_ROOT=$2; shift 2 ;;
        --email) EMAIL=$2; shift 2 ;;
        --org) ORG=$2; shift 2 ;;
        --gateway-kit) KIT_NAME=$2; KIT_DIR=$3; shift 3 ;;
        -h|--help) usage ;;
        *) echo "неизвестный параметр: $1" >&2; usage ;;
    esac
done

LIB=/usr/local/lib/poa-access
KEYS=/etc/poa-revocation

if [ -n "$KIT_NAME" ]; then
    [ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }
    [ -d "$LIB/poa_access" ] || { echo "сервер не установлен" >&2; exit 1; }
    [ ! -e "$KIT_DIR" ] || { echo "каталог $KIT_DIR уже существует" >&2; exit 1; }
    cd "$LIB" && python3 -m poa_access.tool gateway-kit "$KEYS" "$KIT_NAME" "$KIT_DIR"
    echo "Перенесите каталог на шлюз, запустите там gateway-install.sh и удалите его."
    exit 0
fi

[ -n "$DOMAIN" ] || usage
[ -n "$ORG" ] || ORG=$DOMAIN
[ -z "$KEY" ] || [ -f "$KEY" ] || { echo "нет файла ключа: $KEY" >&2; exit 1; }
[ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)

echo "== зависимости"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -q --no-install-recommends \
        python3 python3-cryptography openssl openvpn nginx certbot python3-certbot-nginx >/dev/null
    # openvpn нужен только как генератор ключей tls-crypt-v2, сама служба не включается.
    systemctl disable --now openvpn.service >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-cryptography openssl openvpn nginx certbot python3-certbot-nginx
else
    echo "не знаю менеджера пакетов этой системы; поставьте python3-cryptography, nginx и certbot сами" >&2
fi

echo "== служба"
# Скрипт и пакет v3 -- вместе в одном каталоге: скрипт ищет пакет рядом с собой.
mkdir -p "$LIB/poa_access"
install -m 755 "$HERE/poa-revocation-server.py" "$LIB/poa-revocation-server.py"
install -m 644 "$HERE"/poa_access/*.py "$LIB/poa_access/"
ln -sf "$LIB/poa-revocation-server.py" /usr/local/sbin/poa-revocation-server.py
install -m 644 "$HERE/poa-revocation.service" /etc/systemd/system/poa-revocation.service

echo "== ключ отзыва"
mkdir -p /etc/poa-revocation
if [ -n "$KEY" ]; then
    install -m 600 "$KEY" /etc/poa-revocation/revocation-key.pem
    echo "взят готовый ключ"
elif [ -f /etc/poa-revocation/revocation-key.pem ]; then
    # Ключ уже есть -- перевыпуск сделал бы бесполезными все выпущенные
    # носители: они проверяют подпись прежним открытым ключом.
    echo "ключ уже есть, оставляю как есть"
else
    umask 077
    openssl genpkey -algorithm ed25519 -out /etc/poa-revocation/revocation-key.pem
    chmod 600 /etc/poa-revocation/revocation-key.pem
    echo "создан новый ключ"
fi

echo "== ключи сервера доступа v3"
# Только недостающие: новый CA лишил бы шлюз доверия, новый ключ данных сделал
# бы нечитаемыми семена TOTP в базе.
(cd "$LIB" && umask 077 && python3 -m poa_access.tool init-keys "$KEYS" "$ORG")

echo "== приём и TLS"
SITE=/etc/nginx/sites-available/poa-revocation
{
    echo "server {"
    echo "    listen 80;"
    echo "    server_name $DOMAIN;"
    echo "    # Носители спрашивают только /v2/state, приложение -- только эти три."
    echo "    # /state протокола 1.0.0 служба отклоняет сама (403)."
    echo "    location = /v2/state { proxy_pass http://127.0.0.1:8081; }"
    echo "    location = /state { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /pubkey { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/enroll { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/media/ { proxy_pass http://127.0.0.1:8081; }"
    echo "    # Протокол v3: носители, шлюз (CRL), выпуски образа, команды администратора."
    echo "    location /v3/ { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/v3/ { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/v3/upload/ {"
    echo "        client_max_body_size 8g;"
    echo "        proxy_request_buffering off;"
    echo "        proxy_pass http://127.0.0.1:8081;"
    echo "    }"
    if [ -n "$REPO_ROOT" ]; then
        echo "    # Репозиторий пакетов на той же машине: просто статика."
        echo "    location /repo/ { alias $REPO_ROOT/; autoindex on; }"
    fi
    echo "    location / { return 404; }"
    echo "}"
} > "$SITE"
mkdir -p /etc/nginx/sites-enabled
ln -sf "$SITE" /etc/nginx/sites-enabled/poa-revocation
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null
systemctl reload nginx

# Блок с `listen 443` в файл сайта дописывает certbot, а файл выше переписан
# заново -- поэтому смотреть надо на сам файл, а не на наличие сертификата.
# Иначе повторный запуск установки оставляет сервер без TLS: сертификат на
# месте, слушателя на 443 нет, и носители молча перестают проверяться.
if [ -n "$EMAIL" ]; then
    set -- --agree-tos -m "$EMAIL"
else
    set -- --agree-tos --register-unsafely-without-email
fi
# Ключ TLS сохраняется при продлении: носители закрепляют его отпечаток (SPKI)
# в описателе сервера, и новый ключ каждые 90 дней оборвал бы им связь.
set -- "$@" --reuse-key
if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    # Сертификат есть: только вписать его в конфигурацию, не выпуская заново
    # (у Let's Encrypt на выпуск есть ограничения по частоте).
    certbot --nginx -d "$DOMAIN" --reinstall --redirect --non-interactive "$@"
else
    certbot --nginx -d "$DOMAIN" --redirect --non-interactive "$@"
fi
grep -q "listen 443" "$SITE" || { echo "TLS не настроен: в $SITE нет 443" >&2; exit 1; }

echo "== запуск"
systemctl daemon-reload
systemctl enable --now poa-revocation.service >/dev/null
systemctl restart poa-revocation.service
sleep 2
systemctl is-active poa-revocation.service

# Код знакомства служба создаёт сама при первом старте: каталог состояния
# принадлежит ей, а не root, и писать туда снаружи -- лишняя возня с правами.
CODE_FILE=/var/lib/poa-revocation/enrollment-code
CODE=""
[ -f "$CODE_FILE" ] && CODE=$(cat "$CODE_FILE")

echo
echo "Готово."
echo "Адрес для приложения:  https://$DOMAIN"
if [ -n "$CODE" ]; then
    echo "Код знакомства:        $CODE"
    echo
    echo "Введите их в приложении при первом запуске. Код одноразовый: приложение"
    echo "по нему отправит свой открытый ключ, и сервер его сожжёт."
else
    echo
    echo "Администраторы уже заведены, новый код не создавался."
fi
echo
echo "Код для следующего администратора выписывается одной строкой:"
echo "  openssl rand -hex 16 | tee $CODE_FILE"
