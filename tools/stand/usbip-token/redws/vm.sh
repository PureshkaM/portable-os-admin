#!/bin/sh
# Стенд 09.10: ВМ рабочего места РЕД ОС. vm.sh start <enforcing 0|1> | stop | ssh <команда>
W=/root/redws
case "$1" in
start)
  K=6.12.101-1.red80.x86_64
  mountpoint -q $W/root && { echo "образ смонтирован — не запускаю"; exit 1; }
  if [ ! -f $W/vmlinuz ] || [ $W/disk2.img -nt $W/vmlinuz.stamp ]; then
    unshare -m --propagation private sh -c "mount -o loop,ro,noload $W/disk2.img $W/root && cp $W/root/boot/vmlinuz-$K $W/vmlinuz && cp $W/root/boot/initramfs-$K.img $W/initrd.img; umount $W/root"; touch $W/vmlinuz.stamp
  fi
  : > $W/serial.log
  setsid qemu-system-x86_64 -machine q35 -accel tcg,thread=multi -cpu max -m 2560 -smp 3 -kernel $W/vmlinuz -initrd $W/initrd.img \
    -append "root=LABEL=redws rw console=ttyS0 selinux=1 enforcing=${2:-1}" -drive file=$W/disk2.img,if=virtio,format=raw \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:13389-:3389,hostfwd=tcp:127.0.0.1:10022-:22 -device virtio-net-pci,netdev=n0 \
    -display none -serial file:$W/serial.log -monitor unix:$W/mon.sock,server,nowait -pidfile $W/qemu.pid > $W/qemu.log 2>&1 &
  for i in $(seq 1 150); do sleep 4; grep -aq "redws login:" $W/serial.log && break; done
  for i in $(seq 1 30); do $0 ssh true 2>/dev/null && { echo "ВМ готова"; exit 0; }; sleep 5; done; echo "ssh не отвечает"; exit 1 ;;
stop)
  $0 ssh poweroff 2>/dev/null || echo system_powerdown | socat - UNIX-CONNECT:$W/mon.sock >/dev/null
  for i in $(seq 1 60); do sleep 3; pgrep -x qemu-system-x86 >/dev/null || { echo "ВМ остановлена"; exit 0; }; done
  echo quit | socat - UNIX-CONNECT:$W/mon.sock >/dev/null; echo "ВМ остановлена принудительно" ;;
ssh)
  shift; exec ssh -q -i $W/k -o ConnectTimeout=20 -o ServerAliveInterval=15 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 10022 root@127.0.0.1 "$@" ;;
esac
