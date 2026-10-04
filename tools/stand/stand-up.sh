#!/bin/sh
# Стенд LIVE-2.0: то, что не переживает перезагрузку ВМ (адреса, цель,
# служба сервера доступа). Запускается юнитом poa-stand.service.
ip addr show lo | grep -q 10.0.2.2 || ip addr add 10.0.2.2/32 dev lo
ip link show poa-tgt >/dev/null 2>&1 || { ip link add poa-tgt type dummy; ip addr add 10.99.0.1/24 dev poa-tgt; }
ip link set poa-tgt up
systemctl stop e6srv 2>/dev/null || true
systemctl reset-failed e6srv 2>/dev/null || true
systemd-run --unit=e6srv --quiet python3 /root/poa-live2/packaging/revocation-server/poa-revocation-server.py \
    --key /root/e6/keys/revocation-key --credentials /root/e6/keys --state /root/e6/state \
    --db /root/e6/state/media.sqlite --admins /root/e6/state/admins --host 127.0.0.1 --port 8081
