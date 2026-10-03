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
DB=postgresql
CLUSTER_NEW=""
CLUSTER_KIT=""
NODE=""
TLS_CERT=""
TLS_KEY=""

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
  --tls-cert <файл> --tls-key <файл>
                            свой сертификат TLS (цепочка в PEM) вместо Let's
                            Encrypt -- для закрытых сетей и своего УЦ
  --db postgresql|sqlite    база (по умолчанию postgresql на этой же машине;
                            sqlite -- только стенд и переход)

Кластер (не меньше трёх узлов; на каждом -- приложение, PostgreSQL под
Patroni и etcd; у каждого узла свой --domain, все они -- в описателе):
  --cluster-new n1=IP,n2=IP,n3=IP --node n1 --cluster-kit <каталог>
                            первый узел: создать комплект кластера и встать
  --cluster-kit <каталог> --node <имя>
                            остальные узлы: встать по перенесённому комплекту
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
        --tls-cert) TLS_CERT=$2; shift 2 ;;
        --tls-key) TLS_KEY=$2; shift 2 ;;
        --db) DB=$2; shift 2 ;;
        --cluster-new) CLUSTER_NEW=$2; shift 2 ;;
        --cluster-kit) CLUSTER_KIT=$2; shift 2 ;;
        --node) NODE=$2; shift 2 ;;
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
case "$DB" in postgresql|sqlite) ;; *) echo "--db: postgresql или sqlite" >&2; exit 1 ;; esac
if [ -n "$CLUSTER_KIT$CLUSTER_NEW$NODE" ]; then
    [ -n "$CLUSTER_KIT" ] && [ -n "$NODE" ] || { echo "кластер: нужны --cluster-kit и --node" >&2; exit 1; }
    [ -n "$CLUSTER_NEW" ] || [ -d "$CLUSTER_KIT" ] || { echo "нет комплекта $CLUSTER_KIT" >&2; exit 1; }
    DB=cluster
fi
if [ -n "$TLS_CERT$TLS_KEY" ]; then
    [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ] || { echo "--tls-cert и --tls-key: оба файла" >&2; exit 1; }
fi
[ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)

echo "== зависимости"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -q --no-install-recommends \
        python3 python3-cryptography openssl openvpn nginx certbot >/dev/null
    # libnss-systemd: имя временного пользователя службы (DynamicUser) должно
    # разрешаться -- по нему PostgreSQL пускает её без пароля (peer). Без него
    # «local user with ID … does not exist» (проверено на чистом Debian 13).
    [ "$DB" = sqlite ] || apt-get install -y -q --no-install-recommends \
        postgresql python3-psycopg2 libnss-systemd >/dev/null
    [ "$DB" != cluster ] || apt-get install -y -q --no-install-recommends \
        patroni python3-etcd etcd-server etcd-client >/dev/null   # DCS etcd3 Patroni -- на python3-etcd
    # openvpn нужен только как генератор ключей tls-crypt-v2, сама служба не включается.
    systemctl disable --now openvpn.service >/dev/null 2>&1 || true
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q python3 python3-cryptography openssl openvpn nginx certbot
    [ "$DB" != cluster ] || { echo "кластер пока ставится только на Debian" >&2; exit 1; }
    if [ "$DB" = postgresql ]; then
        dnf install -y -q postgresql-server python3-psycopg2
        [ -f /var/lib/pgsql/data/PG_VERSION ] || postgresql-setup --initdb
    fi
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
install -m 755 "$HERE/cluster.sh" "$LIB/cluster.sh"
if [ "$DB" = cluster ] && [ -z "$CLUSTER_NEW" ]; then
    # Ключи сервера на всех узлах одинаковые: до шага ниже, иначе он создал бы свой.
    sh "$LIB/cluster.sh" keys "$CLUSTER_KIT"
fi

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
if [ -n "$CLUSTER_NEW" ] && [ ! -d "$CLUSTER_KIT" ]; then
    sh "$LIB/cluster.sh" kit "$CLUSTER_KIT" "$ORG" "$CLUSTER_NEW"
fi

echo "== база"
# PostgreSQL на этой же машине, вход службы -- по unix-сокету без пароля
# (peer): у службы DynamicUser с именем юнита, ему и принадлежит роль. Пароля
# нет ни в юните, ни на диске. Старая база sqlite переносится один раз.
mkdir -p /etc/systemd/system/poa-revocation.service.d
if [ "$DB" = cluster ]; then
    sh "$LIB/cluster.sh" node "$CLUSTER_KIT" "$NODE"
elif [ "$DB" = postgresql ]; then
    PGURL="postgresql://poa-revocation@/poa_access?host=/run/postgresql"
    MIGRATE_URL="postgresql:///poa_access?host=/run/postgresql"   # вход как postgres
    systemctl enable --now postgresql >/dev/null 2>&1 || systemctl enable --now postgresql.service
    PSQL="runuser -u postgres -- psql -qtA"
    $PSQL -c "SELECT 1 FROM pg_roles WHERE rolname = 'poa-revocation'" | grep -q 1 \
        || $PSQL -c 'CREATE ROLE "poa-revocation" LOGIN'
    $PSQL -c "SELECT 1 FROM pg_database WHERE datname = 'poa_access'" | grep -q 1 \
        || runuser -u postgres -- createdb -O poa-revocation poa_access
    for OLD in /var/lib/private/poa-revocation/media.sqlite /var/lib/poa-revocation/media.sqlite; do
        [ -f "$OLD" ] || continue
        systemctl stop poa-revocation.service 2>/dev/null || true
        TMP=$(mktemp -d)
        cp "$OLD" "$TMP/media.sqlite"
        chown -R postgres "$TMP"
        (cd "$LIB" && runuser -u postgres -- python3 -m poa_access.migrate "$TMP/media.sqlite" \
            "$MIGRATE_URL" --role poa-revocation)
        rm -rf "$TMP"
        mv "$OLD" "$OLD.migrated"
        echo "база sqlite перенесена в PostgreSQL ($OLD.migrated оставлен как копия)"
        break
    done
    printf '[Service]\nEnvironment="POA_DB=%s"\n' "$PGURL" \
        > /etc/systemd/system/poa-revocation.service.d/db.conf
else
    rm -f /etc/systemd/system/poa-revocation.service.d/db.conf
fi

echo "== приём и TLS"
SITE=/etc/nginx/sites-available/poa-revocation
ACME=/var/www/poa-acme
mkdir -p "$ACME"
# Оба блока пишет этот скрипт, а certbot только выпускает сертификат.
# `certbot --nginx --redirect` переделывал блок порта 80 в перенаправление
# всего подряд -- и проба портала (/v3/probe, Э14) получала 301: любой
# носитель решил бы, что сидит за гостиничным порталом.
port80() {
    echo "server {"
    echo "    listen 80;"
    echo "    server_name $DOMAIN;"
    echo "    # Проба портала -- только по HTTP и без перенаправления."
    echo "    location = /v3/probe { proxy_pass http://127.0.0.1:8081; }"
    echo "    location /.well-known/acme-challenge/ { root $ACME; }"
    echo "    location / { return 301 https://\$host\$request_uri; }"
    echo "}"
}
port443() {
    echo "server {"
    echo "    listen 443 ssl;"
    echo "    server_name $DOMAIN;"
    echo "    ssl_certificate $1;"
    echo "    ssl_certificate_key $2;"
    echo "    ssl_protocols TLSv1.2 TLSv1.3;"
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
}
mkdir -p /etc/nginx/sites-enabled
ln -sf "$SITE" /etc/nginx/sites-enabled/poa-revocation
rm -f /etc/nginx/sites-enabled/default

if [ -n "$TLS_CERT" ]; then
    mkdir -p /etc/poa-revocation/tls
    install -m 644 "$TLS_CERT" /etc/poa-revocation/tls/fullchain.pem
    install -m 600 "$TLS_KEY" /etc/poa-revocation/tls/privkey.pem
    CERT=/etc/poa-revocation/tls/fullchain.pem
    CERT_KEY=/etc/poa-revocation/tls/privkey.pem
else
    CERT=/etc/letsencrypt/live/$DOMAIN/fullchain.pem
    CERT_KEY=/etc/letsencrypt/live/$DOMAIN/privkey.pem
    if [ ! -f "$CERT" ]; then
        # Сначала только порт 80 -- через него Let's Encrypt проверяет домен.
        port80 > "$SITE"
        nginx -t -q
        systemctl reload nginx
        if [ -n "$EMAIL" ]; then
            set -- --agree-tos -m "$EMAIL"
        else
            set -- --agree-tos --register-unsafely-without-email
        fi
        # Ключ TLS сохраняется при продлении: носители закрепляют его отпечаток
        # (SPKI) в описателе сервера, и новый ключ каждые 90 дней оборвал бы им связь.
        certbot certonly --webroot -w "$ACME" -d "$DOMAIN" --non-interactive --reuse-key \
            --deploy-hook "systemctl reload nginx" "$@"
    fi
fi
{ port80; port443 "$CERT" "$CERT_KEY"; } > "$SITE"
nginx -t -q
systemctl reload nginx
grep -q "listen 443" "$SITE" || { echo "TLS не настроен: в $SITE нет 443" >&2; exit 1; }

echo "== запуск"
systemctl daemon-reload
systemctl enable --now poa-revocation.service >/dev/null
systemctl restart poa-revocation.service
sleep 2
# В кластере служба поднимется, когда Patroni выберет ведущего (нужен
# кворум -- второй узел): до того она перезапускается сама.
systemctl is-active poa-revocation.service || [ "$DB" = cluster ]

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
