#!/bin/sh
# Стенд 09.10: ВМ рабочего места РЕД ОС 8 (x86_64) для проверки USB/IP + xrdp + polkit
set -e
W=/root/redws; IMG=$W/disk.img; R=$W/root
mountpoint -q $W/iso || mount -o loop,ro /root/images/redos-8с.iso $W/iso
[ -f $IMG ] || { truncate -s 7G $IMG; mkfs.ext4 -q -L redws $IMG; }
mkdir -p $R; mountpoint -q $R || mount -o loop $IMG $R
REPO="--repofrompath=riso,file://$W/iso --repofrompath=ros,https://repo1.red-soft.ru/redos/8.0/x86_64/os --repofrompath=rup,https://repo1.red-soft.ru/redos/8.0/x86_64/updates --setopt=ros.sslcacert=$W/ca.pem --setopt=rup.sslcacert=$W/ca.pem --repo=riso --repo=ros --repo=rup"
dnf --installroot $R --setopt=cachedir=/root/redws/cache --setopt=keepcache=1 --releasever 8.0 --forcearch x86_64 --nogpgcheck -y $REPO \
  --setopt=install_weak_deps=False install \
  basesystem setup filesystem bash coreutils systemd systemd-udev util-linux shadow-utils passwd \
  procps-ng iproute iputils NetworkManager openssh-server kernel-lt dracut kmod \
  selinux-policy-targeted policycoreutils rootfiles less \
  polkit pcsc-lite pcsc-lite-ccid pcsc-lite-libs opensc python3 usbip \
  xrdp xorgxrdp xterm xorg-x11-xauth dbus
echo "== установлено: $(chroot $R rpm -qa | wc -l) пакетов"
