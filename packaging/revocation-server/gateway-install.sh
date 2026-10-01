#!/bin/sh
# OpenVPN-шлюз для носителей: роль `gateway` (LIVE-2.0).
#
#   sh gateway-install.sh --kit <каталог комплекта> --server https://access.example.org \
#                         --lan <сеть рабочих мест, например 10.20.0.0/16> [--pool 10.8.0.0/24]
#
# Комплект выпускает сервер доступа: `install.sh --gateway-kit <имя> <каталог>`.
# Шлюз доверяет CA сервера доступа и раз в минуту забирает у него CRL -- больше
# он с сервером не говорит (модель ViPNet: центр выпускает, шлюз проверяет).
#
# Что делает:
# * два экземпляра OpenVPN: UDP 1194 и запасной TCP 443 (гостиницы режут UDP);
# * TLS 1.3, AEAD-шифры, tls-crypt-v2 (пакеты без клиентского ключа
#   отбрасываются до TLS), проверка CRL, перепроверка сеанса раз в 15 минут;
# * пересылку из пула VPN только в сеть рабочих мест (`--lan`), с NAT.
#
# Сегментацию по рабочим местам делает внутренний межсетевой экран компании;
# этот сценарий её не заменяет.
set -eu

KIT=""
SERVER=""
LAN=""
POOL_UDP="10.8.0.0"
POOL_TCP="10.9.0.0"

while [ $# -gt 0 ]; do
    case "$1" in
        --kit) KIT=$2; shift 2 ;;
        --server) SERVER=$2; shift 2 ;;
        --lan) LAN=$2; shift 2 ;;
        *) echo "неизвестный параметр: $1" >&2; exit 2 ;;
    esac
done
[ -n "$KIT" ] && [ -n "$SERVER" ] && [ -n "$LAN" ] || {
    echo "нужны --kit, --server и --lan" >&2; exit 2; }
[ "$(id -u)" = "0" ] || { echo "нужны права root" >&2; exit 1; }
for f in ca.crt gateway.key gateway.crt tls-crypt-v2-server.key name; do
    [ -f "$KIT/$f" ] || { echo "в комплекте нет $f" >&2; exit 1; }
done
NAME=$(cat "$KIT/name")

echo "== зависимости"
if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -q --no-install-recommends openvpn nftables curl >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y -q openvpn nftables curl
fi

echo "== ключи"
D=/etc/openvpn/poa
install -d -m 700 "$D"
install -m 644 "$KIT/ca.crt" "$D/ca.crt"
install -m 600 "$KIT/gateway.key" "$D/gateway.key"
install -m 644 "$KIT/gateway.crt" "$D/gateway.crt"
install -m 600 "$KIT/tls-crypt-v2-server.key" "$D/tls-crypt-v2.key"
# CRL -- в отдельном читаемом каталоге. OpenVPN перечитывает его на каждом
# подключении уже от имени nobody; в каталоге 0700 он не может даже сделать stat
# и **молча** работает по списку, загруженному при старте, -- отзыв перестаёт
# действовать, а в журнале одно предупреждение (найдено на стенде 01.10.2026).
C=/etc/openvpn/poa-crl
install -d -m 755 "$C"

echo "== CRL"
cat > /usr/local/sbin/poa-fetch-crl <<EOF
#!/bin/sh
# Забирает CRL у сервера доступа. Пустой или битый ответ не заменяет прежний:
# лучше проверять по минутной давности списку, чем остаться без списка.
set -eu
tmp=\$(mktemp)
if curl -fsS --max-time 20 "$SERVER/v3/crl" -o "\$tmp" && openssl crl -in "\$tmp" -noout 2>/dev/null; then
    install -m 644 "\$tmp" $C/crl.pem
fi
rm -f "\$tmp"
EOF
chmod 755 /usr/local/sbin/poa-fetch-crl
cat > /etc/systemd/system/poa-fetch-crl.service <<'EOF'
[Unit]
Description=CRL сервера доступа для OpenVPN-шлюза
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/poa-fetch-crl
EOF
cat > /etc/systemd/system/poa-fetch-crl.timer <<'EOF'
[Unit]
Description=CRL сервера доступа раз в минуту
[Timer]
OnBootSec=10s
OnUnitActiveSec=60s
AccuracySec=5s
[Install]
WantedBy=timers.target
EOF
/usr/local/sbin/poa-fetch-crl || true
[ -f "$C/crl.pem" ] || { echo "CRL не получен с $SERVER -- без него шлюз не запустится" >&2; exit 1; }

echo "== OpenVPN"
conf() {  # $1 -- имя, $2 -- proto, $3 -- порт, $4 -- пул
    cat > "/etc/openvpn/server/poa-$1.conf" <<EOF
# Сформировано gateway-install.sh. Шлюз $NAME.
dev tun-poa-$1
proto $2
port $3
topology subnet
server $4 255.255.255.0
push "route ${LAN%/*} $(lan_mask)"
ca $D/ca.crt
cert $D/gateway.crt
key $D/gateway.key
dh none
tls-crypt-v2 $D/tls-crypt-v2.key
crl-verify $C/crl.pem
remote-cert-tls client
verify-client-cert require
tls-version-min 1.3
data-ciphers AES-256-GCM:CHACHA20-POLY1305
# Сеанс перепроверяется раз в 15 минут: отозванный по CRL отпадёт к этому сроку.
reneg-sec 900
# Прежний ключ канала данных живёт после перепроверки ещё tran-window секунд
# (по умолчанию 3600). Без этой строки отозванный сеанс, проваливший
# перепроверку, работал бы ещё час (найдено на стенде 01.10.2026).
tran-window 60
keepalive 10 60
# После сброса прав до nobody ключи с диска уже не прочесть (они 0600 root), а
# серверный ключ tls-crypt-v2 OpenVPN перечитывает на каждом подключении.
# Без persist-key первое же подключение роняет шлюз (найдено на стенде 01.10.2026).
persist-key
persist-tun
user nobody
group nogroup
verb 3
EOF
}
lan_mask() {
    bits=${LAN#*/}; m=""; i=0
    while [ $i -lt 4 ]; do
        if [ "$bits" -ge 8 ]; then o=255; bits=$((bits-8))
        else o=$((256 - (1 << (8-bits)))); [ "$bits" -le 0 ] && o=0; bits=0; fi
        m="$m${m:+.}$o"; i=$((i+1))
    done
    echo "$m"
}
conf udp udp 1194 "$POOL_UDP"
conf tcp tcp-server 443 "$POOL_TCP"
getent group nogroup >/dev/null || sed -i 's/^group nogroup$/group nobody/' /etc/openvpn/server/poa-*.conf

echo "== пересылка"
echo "net.ipv4.ip_forward = 1" > /etc/sysctl.d/90-poa-gateway.conf
sysctl -q -p /etc/sysctl.d/90-poa-gateway.conf
cat > /etc/nftables.d-poa-gateway.nft <<EOF
table inet poa_gateway {
    chain forward {
        type filter hook forward priority 0; policy drop;
        ct state established,related accept
        iifname "tun-poa-*" ip daddr $LAN accept
    }
    chain postrouting {
        type nat hook postrouting priority 100;
        iifname "tun-poa-*" ip daddr $LAN masquerade
    }
}
EOF
nft -f /etc/nftables.d-poa-gateway.nft
# Правила переживают перезагрузку отдельной службой: общий nftables.conf
# системы этот сценарий не трогает.
cat > /etc/systemd/system/poa-gateway-nft.service <<'EOF'
[Unit]
Description=Пересылка пула VPN в сеть рабочих мест
Before=openvpn-server@poa-udp.service openvpn-server@poa-tcp.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/nftables.d-poa-gateway.nft
ExecStop=/usr/sbin/nft delete table inet poa_gateway
[Install]
WantedBy=multi-user.target
EOF

echo "== запуск"
systemctl daemon-reload
systemctl enable --now poa-fetch-crl.timer >/dev/null
systemctl enable poa-gateway-nft.service >/dev/null
systemctl enable openvpn-server@poa-udp openvpn-server@poa-tcp >/dev/null
# restart, а не --now: повторный запуск сценария должен применить новую
# конфигурацию к уже работающему шлюзу.
systemctl restart openvpn-server@poa-udp openvpn-server@poa-tcp
systemctl is-active openvpn-server@poa-udp openvpn-server@poa-tcp
# Отзыв должен действовать: CRL обязан читаться от имени, под которым работает
# OpenVPN после сброса прав.
RUNAS=nobody
if command -v runuser >/dev/null 2>&1; then
    runuser -u "$RUNAS" -- test -r "$C/crl.pem" || { echo "CRL не читается от имени $RUNAS -- отзыв не будет действовать" >&2; exit 1; }
fi
echo
echo "Готово. Шлюз $NAME. Удалите комплект: rm -rf $KIT"
echo "На сервере доступа задайте адреса шлюза командой vpn (программа администратора)."
