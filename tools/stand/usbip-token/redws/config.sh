#!/bin/sh
set -e
R=/root/redws/root; K=6.12.101-1.red80.x86_64
for d in proc sys dev; do mountpoint -q $R/$d || { mount --rbind /$d $R/$d; mount --make-rslave $R/$d; }; done
chroot $R depmod $K
chroot $R dracut -f --no-hostonly --kver $K --add-drivers "virtio_blk virtio_pci virtio_net virtio_scsi ext4" /boot/initramfs-$K.img
echo "LABEL=redws / ext4 defaults 0 1" > $R/etc/fstab
echo redws > $R/etc/hostname
echo "root:RedWs123" | chroot $R chpasswd
for u in rdpuser rdpuser2; do chroot $R id $u >/dev/null 2>&1 || chroot $R useradd -m $u; echo "$u:RdpTest123" | chroot $R chpasswd; done
chroot $R systemctl enable sshd xrdp xrdp-sesman NetworkManager pcscd.socket
sed -i "s/^SELINUX=.*/SELINUX=enforcing/" $R/etc/selinux/config; touch $R/.autorelabel
echo vhci-hcd > $R/etc/modules-load.d/poa-vhci.conf
# связка
install -d $R/usr/local/libexec
install -m 755 /root/redws/poa-token-attach /root/redws/poa-token-owner $R/usr/local/libexec/
install -m 644 /root/redws/49-poa-token.rules $R/etc/polkit-1/rules.d/
grep -q poa-token-attach $R/etc/pam.d/xrdp-sesman || echo "session    optional     pam_exec.so /usr/local/libexec/poa-token-attach" >> $R/etc/pam.d/xrdp-sesman
# проверка в сеансе (только стенд)
T=$R/opt/poa-token-test; install -d $T; install -m 755 /root/redws/session-check.py $T/
cp /root/rutoken/rutoken-probe.py /root/rutoken/x64/librtpkcs11ecp.so $T/; cp -r /root/poa-live2/src/portable_os_admin/install/access/runtime $T/; chmod -R a+rX $T
for u in rdpuser rdpuser2; do a=""; [ $u = rdpuser ] && a=" --probe"; printf "#!/bin/sh\n/opt/poa-token-test/session-check.py$a\nexec xterm\n" > $R/home/$u/startwm.sh; chmod 755 $R/home/$u/startwm.sh; chroot $R chown $u: /home/$u/startwm.sh; done
install -d -m 700 $R/root/.ssh; install -m 600 /root/redws/k.pub $R/root/.ssh/authorized_keys
for d in dev sys proc; do umount -R $R/$d; done
ls -la $R/boot/initramfs-$K.img
