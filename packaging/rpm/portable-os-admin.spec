Name:           portable-os-admin
Version:        1.2.0
Release:        3%{?dist}
Summary:        Ткач — приложение администратора системы «Шлейф»

License:        MIT
URL:            https://github.com/PureshkaM/portable-os-admin
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# Зависимости интерфейса доступны в репозитории сертифицированного ISO РЕД ОС 8.
Requires:       python3 >= 3.9
Requires:       gtk3
Requires:       python3-gobject
Requires:       python3-cryptography >= 2.6
# pkexec, through which the application asks the desktop for administrator
# rights. The agent that draws the dialog comes with the desktop environment.
Requires:       polkit
Requires:       parted
Requires:       gdisk
Requires:       cryptsetup
Requires:       dnf
Requires:       attr
# The installer creates the target's filesystems and erases the medium itself,
# so the admin host needs these even though the *target* system gets its own.
Requires:       dosfstools
Requires:       e2fsprogs
Requires:       util-linux
# udevadm ships in the `systemd` package itself on the RHEL family -- there
# is no separate systemd-udev subpackage there. install/tooling.py's
# PROVIDED_BY table says the same thing and is what the installer's own
# "missing tool" messages quote; the spec has to agree with it, not guess.
Requires:       systemd
# Reading RedOS repository metadata: its primary.xml is zstd-compressed.
Requires:       zstd
Requires:       squashfs-tools
Requires:       ca-certificates
# rpm2cpio, used to read the target distribution's own kickstart parser out of
# an installation image. It is part of a RHEL-family base system, but naming it
# keeps a minimal install from surprising us.
Requires:       rpm
# rpm2cpio only unwraps the outer cpio archive; cpio is what actually reads
# the result. install/image_catalog.py also calls it directly to read
# tzdata and xkeyboard-config off a RedOS image -- that path has no bsdtar
# fallback (unlike install/rpm_extract.py's), so this is a hard Requires
# rather than the Recommends it is on the .deb side.
Requires:       cpio
# polkit is desktop-agnostic; the agent that draws the password prompt is not.
# One per mainstream desktop, so whichever is installed brings its own. This
# rich (boolean) dependency needs rpm >= 4.13; RedOS is RHEL8-based
# (rpm 4.14+), the same base-system assumption install/tooling.py already
# makes by naming `dnf` unconditionally.
Recommends:     (polkit-kde or polkit-gnome or mate-polkit or lxqt-policykit)
# Only needed to build a medium for another architecture. Without it such an
# install is refused before the medium is touched, so it is not required.
Suggests:       qemu-user-static

%description
Creates and administers an encrypted admin container, builds install
configurations, installs Debian or RedOS onto an external
LUKS-encrypted medium, and manages its logs, exchange partition and
recovery codes afterwards. The interface is a guided GTK 3 GUI.

Uses system Python, GTK 3, PyGObject and cryptography from the operating system repositories.
No Python interpreter or third-party libraries are bundled.

%prep
%setup -q

%build
# nothing to build: pure Python, and the only compiled dependency
# (cryptography) comes from the distribution.

%install
rm -rf %{buildroot}
# Каталог собственных исходников не зависит от пути site-packages ОС.
mkdir -p %{buildroot}/usr/lib/portable-os-admin/app
cp -r src/portable_os_admin %{buildroot}/usr/lib/portable-os-admin/app/
find %{buildroot}/usr/lib/portable-os-admin/app -name __pycache__ -type d -exec rm -rf {} +
cat > %{buildroot}/usr/lib/portable-os-admin/app/__main__.py <<'EOF'
from portable_os_admin.gui.gtk3 import run

run()
EOF
mkdir -p %{buildroot}/usr/bin
cat > %{buildroot}/usr/bin/portable-os-admin <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -I /usr/lib/portable-os-admin/app "$@"
EOF
chmod 755 %{buildroot}/usr/bin/portable-os-admin
mkdir -p %{buildroot}/usr/share/applications
cat > %{buildroot}/usr/share/applications/portable-os-admin.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Ткач
GenericName=Администрирование переносных устройств
Comment=Система «Шлейф»: создание и обслуживание устройств «Нить» с шифрованием LUKS
Exec=portable-os-admin
Icon=utilities-terminal
Terminal=false
Categories=System;Settings;
EOF
# polkit action, the rule naming who may authorise it, and the launcher the
# action points at. The path inside the .policy file is absolute and must match
# where the launcher lands.
mkdir -p %{buildroot}/usr/share/polkit-1/actions
install -m 644 packaging/polkit/ru.portableosadmin.policy \
    %{buildroot}/usr/share/polkit-1/actions/
mkdir -p %{buildroot}/usr/share/polkit-1/rules.d
install -m 644 packaging/polkit/49-portable-os-admin.rules \
    %{buildroot}/usr/share/polkit-1/rules.d/
mkdir -p %{buildroot}/usr/lib/portable-os-admin
install -m 755 packaging/polkit/launch-root %{buildroot}/usr/lib/portable-os-admin/

%files
%license LICENSE
/usr/lib/portable-os-admin/app
/usr/bin/portable-os-admin
/usr/share/applications/portable-os-admin.desktop
/usr/share/polkit-1/actions/ru.portableosadmin.policy
/usr/share/polkit-1/rules.d/49-portable-os-admin.rules
/usr/lib/portable-os-admin/launch-root

%changelog
* Thu Sep 17 2026 PureshkaM <xripmax@gmail.com> - 1.2.0-1
- One release wizard instead of three: the first step asks for the source (an
  installation image or a ready distribution) and the release path follows from
  it. The dashboard offers one button, "Выпустить носитель", instead of three.
- Four sections in the sidebar instead of six: logs and the exchange partition
  became actions on the selected medium and arrive with the answer already
  given.
- Six buttons on the configurations screen instead of nine: exchanging
  configurations with another administrator moved to its own screen.
- Configuration values come from the installation image rather than from lists
  written into the program; the image is read even when its path was typed.
- Closing the session ends the program. The container key is wiped in place,
  but the administrator secret lives in a string, and only leaving the process
  takes it out of memory. The window cross and termination signals go the same
  way.
- Both ways out ask before interrupting a medium being written, not just the
  window cross.
- Internal: releasing a medium and acting on one moved into services; the
  administrator secret no longer reaches the screens.

* Tue Sep 15 2026 PureshkaM <xripmax@gmail.com> - 1.1.0-1
- Revocation v2: the medium signs its request with its own key and asks the
  server every five minutes and whenever the network comes up; anyone else
  gets 403.
- Session gate: kiosk applications start only after a signed "ok" from the
  server; without a connection the user gets a desktop notification.
- The revocation server records when each medium last checked in; the revoke
  screen shows it.
- Distributions are signed with the administrator's key and checked before a
  medium is written.
- The package list is read from the installation image, without a network.
- Verbose boot and serial console output are wizard options.
- A revoked medium wipes the partition table before the data: wiping the data
  cuts the running system off its own root, and later steps never ran.
- Debian: CA certificates on the medium, a working MATE window list and
  notifications that reach the session.

* Sun Sep 14 2026 PureshkaM <xripmax@gmail.com> - 1.0.0-1
- First complete version. A revoked medium now destroys itself for good: key
  slots of all three volumes, the data itself, the boot part and finally the
  partition table, in that order, so that losing power at any step still
  leaves no way to the data.
- Both ways of writing a medium (deploy and install) go through one helper,
  so a new file on the medium can no longer reach only one of them.
- One medium per port in the media list: rewritten media are no longer
  offered as plugged in.

* Fri Sep 11 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-3
- Dropped the sysconfig-based fallback for %{python3_sitelib}: it encoded the
  build host's own Python layout rather than the target's, which is how a
  .rpm built on Debian ended up installing to a path no RHEL-family python3
  searches. build-rpm.sh now refuses to build at all without the real
  python3-devel macros, and Release is bumped to record that this build's
  file layout genuinely changed, not just its packaging metadata.
- Requires now names python3(abi) = %{python3_version} instead of a hardcoded
  `python3 >= 3.9` floor RHEL's own python3 package never satisfies, and
  systemd instead of a systemd-udev subpackage that does not exist there.
- Added cpio, which install/image_catalog.py calls directly to read a RedOS
  image's tzdata and xkeyboard-config, with no bsdtar fallback for that path.

* Thu Aug 20 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-2
- Install as a system Python package instead of a private virtualenv, so the
  package no longer downloads anything at install time.
- Drop the Textual TUI; the GUI is the only interface.
- Ask the desktop for administrator rights through polkit.

* Thu Jul 30 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-1
- Initial packaging.
