Name:           portable-os-admin
Version:        1.1.0
Release:        1%{?dist}
Summary:        Admin tool for portable LUKS-encrypted OS media

License:        MIT
URL:            https://github.com/PureshkaM/portable-os-admin
Source0:        %{name}-%{version}.tar.gz
# Не noarch: пакет несёт собственный рантайм -- Python, Tcl/Tk и cryptography,
# собранные для x86_64. Это цена самодостаточности, и она осознанная: без
# рантайма пакет требовал бы python3-tkinter, которого на DVD РЕД ОС 8 нет.
BuildArch:      x86_64

# %{python3_sitelib} comes from python3-rpm-macros (pulled in by
# python3-devel below): it is how rpm's macro table learns where *this
# RHEL-family host's* python3 keeps site-packages, e.g.
# /usr/lib/python3.9/site-packages on RHEL8, a different path on RHEL9 --
# a fact about the target distribution's own packaging convention.
#
# This used to have a fallback that asked the *build host's own* `sysconfig`
# for that path when the macro was undefined, specifically to let the spec be
# built -- and so checked -- on a machine with rpmbuild but no RHEL macros.
# That is exactly the bug it looks like: sysconfig answers for whatever
# happens to be running rpmbuild, not for the RedOS/RHEL box that will later
# run `dnf install`. On a Debian host it resolved to
# /usr/lib/python3/dist-packages, a path no RHEL-family python3 ever
# searches -- `dnf install` succeeded and `portable-os-admin` then failed
# with ModuleNotFoundError on first launch.
#
# There is no host-independent value to fall back to -- the whole point of
# the macro is that it is a RHEL-family convention, not a computable fact --
# so there is no fallback here any more. A build without the real macro must
# refuse loudly instead of shipping a package that only looks like it works.
# build-rpm.sh checks for exactly this before invoking rpmbuild at all.

# BuildRequires больше нет вовсе.
#
# python3-devel требовался, пока код ставился в системный site-packages: путь
# туда даёт макрос %{python3_sitelib}, а его приносит именно этот пакет. Со
# своим рантаймом в собственном префиксе системный Python не нужен ни при
# сборке, ни при установке, и требование стало ложным: сборка 13.09.2026 на
# раннере GitHub упала на «python3-devel is needed by portable-os-admin».

# RHEL-family python3 stays at 3.6 forever; 3.9/3.11/... arrive as separate,
# differently-named packages. `python3 >= 3.9` therefore either fails to
# resolve at all or resolves against the wrong interpreter entirely.
# python3(abi) is the virtual provide rpm itself generates for whichever
# interpreter python3-devel's macros were computed for on the build host --
# the same one %{python3_sitelib} above resolves against -- so this tracks
# the actual build, not a guessed floor.
# Python, Tk и cryptography пакет несёт с собой, поэтому от системных здесь
# ничего не требуется. Так и получается «ставится на любую машину семейства, не
# спрашивая репозиториев»: `python3-tkinter` на DVD РЕД ОС 8 нет вовсе, и
# требование его наличия делало пакет неустановимым ровно там, ради чего он
# написан. Рантайм собирается packaging/rpm/build-runtime.sh из пакетов самого
# дистрибутива -- ни компиляции, ни сторонних двоичных файлов.
# pkexec, through which the application asks the desktop for administrator
# rights. The agent that draws the dialog comes with the desktop environment.
Requires:       polkit
Requires:       parted
Requires:       gdisk
Requires:       cryptsetup
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
recovery codes afterwards. The interface is a guided Tkinter GUI.

Everything this package needs comes from the distribution. The only
third-party Python library is cryptography, taken from
python3-cryptography rather than downloaded, so nothing is fetched at
install time and the package installs on a machine with no route to the
internet -- which is the environment this tool is meant for.

Note: this spec targets RedOS/RHEL-family *admin hosts*. If your install
configurations target Debian media, the admin host still
needs debootstrap available -- RedOS's own repositories were not found to
carry it, so that has to come from elsewhere (see docs/ARCHITECTURE.md
"Known limitations" in the source tree).

%prep
%setup -q

%build
# nothing to build: pure Python, and the only compiled dependency
# (cryptography) comes from the distribution.

%install
rm -rf %{buildroot}
# Программа и её рантайм -- в собственном префиксе, а не в системном
# site-packages: пакет ничего не берёт у системного Python, потому что нужного
# ему там может не быть вовсе.
mkdir -p %{buildroot}/usr/lib/portable-os-admin/app
cp -r src/portable_os_admin %{buildroot}/usr/lib/portable-os-admin/app/
find %{buildroot}/usr/lib/portable-os-admin/app -name __pycache__ -type d -exec rm -rf {} +
# Рантайм собирается packaging/rpm/build-runtime.sh ДО rpmbuild и приезжает в
# каталоге runtime/ вместе с исходниками. Собирать его здесь нельзя: он
# тянется из сетевого репозитория, а сборка пакета должна работать из того,
# что уже есть, и не зависеть от сети в этот момент.
mkdir -p %{buildroot}/usr/lib/portable-os-admin/runtime
cp -a runtime/. %{buildroot}/usr/lib/portable-os-admin/runtime/
mkdir -p %{buildroot}/usr/bin
cat > %{buildroot}/usr/bin/portable-os-admin <<'EOF'
#!/bin/sh
# Свой интерпретатор и свой Tk. PYTHONHOME -- чтобы стандартная библиотека
# искалась рядом, а не в системе; LD_LIBRARY_PATH -- чтобы _tkinter нашёл
# Tcl/Tk там же, где лежит сам, а не в /usr/lib64, где их может не быть.
RT=/usr/lib/portable-os-admin/runtime
export PYTHONHOME="$RT/usr"
export LD_LIBRARY_PATH="$RT/usr/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH=/usr/lib/portable-os-admin/app
exec "$RT/usr/bin/python3.11" -m portable_os_admin.gui.app "$@"
EOF
chmod 755 %{buildroot}/usr/bin/portable-os-admin
mkdir -p %{buildroot}/usr/share/applications
cat > %{buildroot}/usr/share/applications/portable-os-admin.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Portable OS Admin
GenericName=Portable OS Admin Tool
Comment=Create and administer a portable LUKS-encrypted OS on external media
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
/usr/lib/portable-os-admin/runtime
/usr/bin/portable-os-admin
/usr/share/applications/portable-os-admin.desktop
/usr/share/polkit-1/actions/ru.portableosadmin.policy
/usr/share/polkit-1/rules.d/49-portable-os-admin.rules
/usr/lib/portable-os-admin/launch-root

%changelog
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
