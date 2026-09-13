#!/bin/sh
# Разворачивает ресурс отзыва одной командой.
#
#   sh install.sh --domain revoke.example.org --revocation-key ./revocation-key.pem
#
# Что делает: ставит зависимости, кладёт службу, заводит каталоги, включает
# TLS и поднимает всё это. Повторный запуск безопасен -- каждый шаг проверяет,
# сделан ли он уже.
#
# Чего не делает намеренно:
#
# * **Не создаёт ключ отзыва.** Он приходит из контейнера администратора
#   («Обслуживание» -> «Выгрузить для сервера…»). Носители носят в себе именно
#   его открытую часть; ключ, рождённый на сервере, они не признают, и отзыв
#   молча перестал бы работать -- худший из возможных отказов для такой вещи.
# * **Не заводит администраторов.** Их открытые ключи кладутся в
#   /etc/poa-revocation/admins по мере появления; сервер знает только их.
# * **Не поднимает ничего лишнего.** Одна служба на стандартной библиотеке
#   Python, sqlite вместо базы данных, nginx как приём TLS. Репозиторий пакетов
#   при желании живёт на этой же машине статическим каталогом -- см. --repo-root.
set -eu

DOMAIN=""
KEY=""
ADMIN_PUBKEY=""
REPO_ROOT=""
EMAIL=""

usage() {
    cat >&2 <<'USAGE'
Использование:
  install.sh --domain <имя> --revocation-key <файл.pem> [параметры]

Обязательные:
  --domain <имя>            публичное имя, на которое берётся сертификат
  --revocation-key <файл>   закрытый ключ отзыва, выгруженный из приложения

Необязательные:
  --admin-pubkey <файл>     открытый ключ первого администратора
  --repo-root <каталог>     раздавать этот каталог как /repo (репозиторий пакетов)
  --email <адрес>           адрес для Let's Encrypt (по умолчанию без адреса)
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN=$2; shift 2 ;;
        --revocation-key) KEY=$2; shift 2 ;;
        --admin-pubkey) ADMIN_PUBKEY=$2; shift 2 ;;
        --repo-root) REPO_ROOT=$2; shift 2 ;;
        --email) EMAIL=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "неизвестный параметр: $1" >&2; usage ;;
    esac
done

[ -n "$DOMAIN" ] || usage
[ -n "$KEY" ] || usage
[ -f "$KEY" ] || { echo "нет файла ключа: $KEY" >&2; exit 1; }
[ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)

echo "== зависимости"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -q --no-install-recommends \
        python3 python3-cryptography nginx certbot python3-certbot-nginx >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-cryptography nginx certbot python3-certbot-nginx
else
    echo "не знаю менеджера пакетов этой системы; поставьте python3-cryptography, nginx и certbot сами" >&2
fi

echo "== служба"
install -m 755 "$HERE/poa-revocation-server.py" /usr/local/sbin/poa-revocation-server.py
install -m 644 "$HERE/poa-revocation.service" /etc/systemd/system/poa-revocation.service

echo "== ключи и каталоги"
mkdir -p /etc/poa-revocation/admins
install -m 600 "$KEY" /etc/poa-revocation/revocation-key.pem
if [ -n "$ADMIN_PUBKEY" ]; then
    install -m 644 "$ADMIN_PUBKEY" /etc/poa-revocation/admins/admin.pem
fi

echo "== приём и TLS"
SITE=/etc/nginx/sites-available/poa-revocation
{
    echo "server {"
    echo "    listen 80;"
    echo "    server_name $DOMAIN;"
    echo "    # Носители спрашивают только это, администраторы -- только это."
    echo "    location /state { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/media/ { proxy_pass http://127.0.0.1:8081; }"
    if [ -n "$REPO_ROOT" ]; then
        echo "    # Репозиторий пакетов на той же машине: просто статика."
        echo "    location /repo/ { alias $REPO_ROOT/; autoindex on; }"
    fi
    echo "    location / { return 404; }"
    echo "}"
} > "$SITE"
ln -sf "$SITE" /etc/nginx/sites-enabled/poa-revocation
rm -f /etc/nginx/sites-enabled/default
nginx -t >/dev/null
systemctl reload nginx

if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo "сертификат для $DOMAIN уже есть"
else
    if [ -n "$EMAIL" ]; then
        certbot --nginx -d "$DOMAIN" --agree-tos -m "$EMAIL" --non-interactive --redirect
    else
        certbot --nginx -d "$DOMAIN" --agree-tos --register-unsafely-without-email \
            --non-interactive --redirect
    fi
fi

echo "== запуск"
systemctl daemon-reload
systemctl enable --now poa-revocation.service >/dev/null
sleep 2
systemctl is-active poa-revocation.service

echo
echo "Готово. Адрес для приложения: https://$DOMAIN"
if [ -z "$ADMIN_PUBKEY" ]; then
    echo "Осталось одно: положить открытый ключ администратора в"
    echo "/etc/poa-revocation/admins/<имя>.pem -- без этого сервер не примет от него"
    echo "ни одной команды. Ключ администратор берёт в своём приложении."
fi
