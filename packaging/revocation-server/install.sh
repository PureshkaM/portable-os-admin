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
USAGE
    exit 2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --domain) DOMAIN=$2; shift 2 ;;
        --revocation-key) KEY=$2; shift 2 ;;
        --repo-root) REPO_ROOT=$2; shift 2 ;;
        --email) EMAIL=$2; shift 2 ;;
        -h|--help) usage ;;
        *) echo "неизвестный параметр: $1" >&2; usage ;;
    esac
done

[ -n "$DOMAIN" ] || usage
[ -z "$KEY" ] || [ -f "$KEY" ] || { echo "нет файла ключа: $KEY" >&2; exit 1; }
[ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)

echo "== зависимости"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -q --no-install-recommends \
        python3 python3-cryptography openssl nginx certbot python3-certbot-nginx >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-cryptography openssl nginx certbot python3-certbot-nginx
else
    echo "не знаю менеджера пакетов этой системы; поставьте python3-cryptography, nginx и certbot сами" >&2
fi

echo "== служба"
install -m 755 "$HERE/poa-revocation-server.py" /usr/local/sbin/poa-revocation-server.py
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

echo "== приём и TLS"
SITE=/etc/nginx/sites-available/poa-revocation
{
    echo "server {"
    echo "    listen 80;"
    echo "    server_name $DOMAIN;"
    echo "    # Носители спрашивают только /state, приложение -- только эти три."
    echo "    location /state { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /pubkey { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/enroll { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /admin/media/ { proxy_pass http://127.0.0.1:8081; }"
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
