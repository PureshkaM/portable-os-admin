#!/bin/sh
# Кластер сервера доступа (LIVE-2.0, Э17): на каждом узле -- приложение,
# PostgreSQL под Patroni и etcd. Вызывается из install.sh, не напрямую.
#
#   cluster.sh kit  <каталог> <домен> n1=10.0.0.11,n2=10.0.0.12,n3=10.0.0.13
#   cluster.sh keys <каталог>            -- ключи сервера из комплекта
#   cluster.sh node <каталог> <имя>      -- etcd, Patroni, строка базы для службы
#
# Комплект создаётся один раз на первом узле и переносится на остальные, как
# комплект шлюза: УЦ кластера и сертификаты узлов (etcd, PostgreSQL, REST
# Patroni -- всё по взаимному TLS), пароли postgres/репликации/приложения и
# ключи сервера. Ключи на всех узлах одинаковые (до запечатанного сервера,
# Э21) -- поэтому комплект содержит секреты: перенести, развернуть, удалить.
#
# Почему так:
# * синхронная репликация (`synchronous_mode`): звено и TOTP не должны
#   «откатиться» при смене ведущего -- асинхронная копия теряет последние шаги;
# * кворум etcd из трёх: раздвоение базы невозможно, пока жив любой из двух;
# * приложение ищет ведущего само -- строкой libpq с несколькими адресами и
#   `target_session_attrs=read-write`, без HAProxy.
set -eu

CMD=${1:-}
KIT=${2:-}
KEYS=/etc/poa-revocation
CLUSTER=$KEYS/cluster

die() { echo "$*" >&2; exit 1; }

# Записать файл и перезапустить службу, только если содержимое изменилось:
# повторный запуск install.sh не должен ронять ведущего (на стенде повтор с
# перезапуском etcd и Patroni на всех узлах разом оставил кластер без него).
put() {   # put <файл> <служба> [владелец] < содержимое
    tmp=$(mktemp)
    cat > "$tmp"
    [ -z "${3:-}" ] || chown "$3" "$tmp"
    if [ -f "$1" ] && cmp -s "$tmp" "$1"; then
        rm -f "$tmp"
        systemctl enable --now "$2" >/dev/null 2>&1
        return 0
    fi
    mv "$tmp" "$1"
    systemctl enable "$2" >/dev/null 2>&1
    systemctl restart --no-block "$2"
}

node_ip() {   # имя -> адрес из nodes комплекта
    sed -n "s/^$1=//p" "$KIT/nodes"
}

make_kit() {
    DOMAIN=$3
    SPEC=$4
    [ ! -e "$KIT" ] || die "каталог $KIT уже существует"
    umask 077
    mkdir -p "$KIT/tls"
    echo "$SPEC" | tr ',' '\n' > "$KIT/nodes"
    [ "$(wc -l < "$KIT/nodes")" -ge 3 ] || die "узлов кластера -- не меньше трёх"
    echo "$DOMAIN" > "$KIT/domain"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
        -subj "/CN=poa-cluster-ca" -keyout "$KIT/tls/ca.key" -out "$KIT/tls/ca.crt" 2>/dev/null
    while IFS='=' read -r name ip; do
        [ -n "$name" ] || continue
        openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -subj "/CN=$name" \
            -keyout "$KIT/tls/$name.key" -out "$KIT/tls/$name.csr" 2>/dev/null
        printf "subjectAltName=IP:%s,DNS:%s\nextendedKeyUsage=serverAuth,clientAuth\n" "$ip" "$name" \
            > "$KIT/tls/$name.ext"
        openssl x509 -req -in "$KIT/tls/$name.csr" -CA "$KIT/tls/ca.crt" -CAkey "$KIT/tls/ca.key" \
            -CAcreateserial -days 3650 -extfile "$KIT/tls/$name.ext" -out "$KIT/tls/$name.crt" 2>/dev/null
        rm -f "$KIT/tls/$name.csr" "$KIT/tls/$name.ext"
    done < "$KIT/nodes"
    rm -f "$KIT/tls/ca.key" "$KIT/tls/ca.srl"   # новых узлов без нового комплекта не будет
    for who in superuser replication rewind app; do
        openssl rand -hex 24 > "$KIT/$who.password"
    done
    mkdir -p "$KIT/keys"
    cp -p "$KEYS"/*.pem "$KEYS"/ca-key "$KEYS"/ca-cert "$KEYS"/data-key "$KEYS"/tls-crypt-v2 \
        "$KIT/keys/" 2>/dev/null || true
    echo "комплект кластера: $KIT -- перенесите на остальные узлы и удалите после установки"
}

install_keys() {
    [ -d "$KIT/keys" ] || die "в комплекте нет ключей сервера"
    mkdir -p "$KEYS"
    for file in "$KIT"/keys/*; do
        install -m 600 "$file" "$KEYS/$(basename "$file")"
    done
}

setup_node() {
    NAME=$3
    IP=$(node_ip "$NAME")
    [ -n "$IP" ] || die "узла $NAME нет в комплекте"
    PGV=$(ls /usr/lib/postgresql | sort -n | tail -n 1)
    HOSTS=$(sed 's/^[^=]*=//' "$KIT/nodes" | tr '\n' ' ')

    echo "== кластер: узел $NAME ($IP)"
    # Пакетный кластер Debian «main» мешает Patroni (тот же порт): его нет.
    if command -v pg_lsclusters >/dev/null && pg_lsclusters -h | grep -q " main "; then
        pg_dropcluster --stop "$PGV" main
    fi
    systemctl disable --now postgresql >/dev/null 2>&1 || true

    mkdir -p "$CLUSTER"
    install -m 644 "$KIT/tls/ca.crt" "$CLUSTER/ca.crt"
    for owner in etcd postgres; do
        install -d -m 750 -o "$owner" -g "$owner" "/etc/poa-cluster-$owner"
        install -m 644 -o "$owner" "$KIT/tls/ca.crt" "/etc/poa-cluster-$owner/ca.crt"
        install -m 644 -o "$owner" "$KIT/tls/$NAME.crt" "/etc/poa-cluster-$owner/node.crt"
        install -m 600 -o "$owner" "$KIT/tls/$NAME.key" "/etc/poa-cluster-$owner/node.key"
    done

    # -- etcd -------------------------------------------------------------
    INITIAL=$(sed 's/^\([^=]*\)=\(.*\)$/\1=https:\/\/\2:2380/' "$KIT/nodes" | paste -sd, -)
    E=/etc/poa-cluster-etcd
    put /etc/default/etcd etcd <<EOF
# generated by portable-os-admin (Э17)
ETCD_NAME="$NAME"
ETCD_DATA_DIR="/var/lib/etcd/poa"
ETCD_LISTEN_PEER_URLS="https://$IP:2380"
ETCD_LISTEN_CLIENT_URLS="https://$IP:2379,https://127.0.0.1:2379"
ETCD_INITIAL_ADVERTISE_PEER_URLS="https://$IP:2380"
ETCD_ADVERTISE_CLIENT_URLS="https://$IP:2379"
ETCD_INITIAL_CLUSTER="$INITIAL"
ETCD_INITIAL_CLUSTER_STATE="new"
ETCD_INITIAL_CLUSTER_TOKEN="poa-$(cat "$KIT/domain")"
ETCD_CERT_FILE="$E/node.crt"
ETCD_KEY_FILE="$E/node.key"
ETCD_TRUSTED_CA_FILE="$E/ca.crt"
ETCD_CLIENT_CERT_AUTH="true"
ETCD_PEER_CERT_FILE="$E/node.crt"
ETCD_PEER_KEY_FILE="$E/node.key"
ETCD_PEER_TRUSTED_CA_FILE="$E/ca.crt"
ETCD_PEER_CLIENT_CERT_AUTH="true"
EOF

    # -- Patroni ----------------------------------------------------------
    P=/etc/poa-cluster-postgres
    ETCD_HOSTS=$(sed 's/^[^=]*=\(.*\)$/\1:2379/' "$KIT/nodes" | paste -sd, -)
    HBA=""
    for host in $HOSTS; do
        HBA="$HBA    - hostssl replication replicator $host/32 scram-sha-256
    - hostssl all all $host/32 scram-sha-256
"
    done
    cat > /usr/local/lib/poa-access/cluster-post-bootstrap.sh <<EOF
#!/bin/sh
# Patroni: после initdb на первом ведущем -- роль и база приложения.
set -eu
psql "\$1" -v ON_ERROR_STOP=1 -qc "CREATE ROLE poa_app LOGIN PASSWORD '$(cat "$KIT/app.password")'"
psql "\$1" -v ON_ERROR_STOP=1 -qc "CREATE DATABASE poa_access OWNER poa_app"
EOF
    chown postgres /usr/local/lib/poa-access/cluster-post-bootstrap.sh
    chmod 700 /usr/local/lib/poa-access/cluster-post-bootstrap.sh
    install -d -m 750 -o postgres -g postgres /etc/patroni
    umask 077
    put /etc/patroni/config.yml patroni postgres:postgres <<EOF
# generated by portable-os-admin (Э17)
scope: poa
name: $NAME
restapi:
  listen: $IP:8008
  connect_address: $IP:8008
  certfile: $P/node.crt
  keyfile: $P/node.key
  cafile: $P/ca.crt
etcd3:
  hosts: $ETCD_HOSTS
  protocol: https
  cacert: $P/ca.crt
  cert: $P/node.crt
  key: $P/node.key
bootstrap:
  dcs:
    ttl: 30
    loop_wait: 10
    retry_timeout: 10
    maximum_lag_on_failover: 0
    synchronous_mode: true
    postgresql:
      use_pg_rewind: true
      parameters:
        password_encryption: scram-sha-256
        ssl: "on"
        ssl_cert_file: $P/node.crt
        ssl_key_file: $P/node.key
        ssl_ca_file: $P/ca.crt
  initdb:
    - encoding: UTF8
    - data-checksums
  post_bootstrap: /usr/local/lib/poa-access/cluster-post-bootstrap.sh
postgresql:
  listen: $IP,127.0.0.1:5432
  connect_address: $IP:5432
  data_dir: /var/lib/postgresql/$PGV/poa
  bin_dir: /usr/lib/postgresql/$PGV/bin
  pgpass: /var/lib/postgresql/.pgpass_patroni
  authentication:
    superuser:
      username: postgres
      password: $(cat "$KIT/superuser.password")
    replication:
      username: replicator
      password: $(cat "$KIT/replication.password")
      sslmode: verify-ca
      sslrootcert: $P/ca.crt
    rewind:
      username: rewinder
      password: $(cat "$KIT/rewind.password")
  parameters:
    unix_socket_directories: /var/run/postgresql
  pg_hba:
    - local all postgres peer
    - host all all 127.0.0.1/32 scram-sha-256
$HBA
EOF

    # -- строка базы для службы -------------------------------------------
    PGHOSTS=$(sed 's/^[^=]*=\(.*\)$/\1:5432/' "$KIT/nodes" | paste -sd, -)
    install -m 600 "$KIT/app.password" "$CLUSTER/db-password"
    rm -f "$CLUSTER/pgpass"
    mkdir -p /etc/systemd/system/poa-revocation.service.d
    cat > /etc/systemd/system/poa-revocation.service.d/db.conf <<EOF
# generated by portable-os-admin (Э17): ведущего находит libpq (read-write)
[Unit]
After=patroni.service
[Service]
# CA и пароль -- учётными данными: каталог ключей закрыт для временного
# пользователя службы (DynamicUser), прямой путь ему не прочитать.
Environment="POA_DB=postgresql://poa_app@$PGHOSTS/poa_access?target_session_attrs=read-write&sslmode=verify-full&sslrootcert=%d/cluster-ca&connect_timeout=5"
LoadCredential=db-password:$CLUSTER/db-password
LoadCredential=cluster-ca:$CLUSTER/ca.crt
Environment=POA_DB_PASSWORD_FILE=%d/db-password
EOF
}

case "$CMD" in
    kit) make_kit "$@" ;;
    keys) install_keys ;;
    node) setup_node "$@" ;;
    *) die "cluster.sh kit|keys|node" ;;
esac
