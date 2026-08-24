Name:           portable-os-admin
Version:        0.1.0
Release:        1%{?dist}
Summary:        Admin tool for portable LUKS-encrypted OS media

License:        MIT
URL:            https://github.com/PureshkaM/portable-os-admin
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# python3-devel supplies this macro on the RHEL family. Defining a fallback
# lets the spec be built -- and therefore checked -- on a machine that has
# rpmbuild but not the RHEL Python macros, which is the only way this package
# gets verified without a RedOS host to hand.
%{!?python3_sitelib: %global python3_sitelib %(python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")}

BuildRequires:  python3-devel

Requires:       python3 >= 3.9
Requires:       python3-tkinter
Requires:       python3-cryptography
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
Requires:       systemd-udev
# Reading RedOS repository metadata: its primary.xml is zstd-compressed.
Requires:       zstd
Requires:       ca-certificates
# rpm2cpio, used to read the target distribution's own kickstart parser out of
# an installation image. It is part of a RHEL-family base system, but naming it
# keeps a minimal install from surprising us.
Requires:       rpm
# polkit is desktop-agnostic; the agent that draws the password prompt is not.
# One per mainstream desktop, so whichever is installed brings its own.
Recommends:     (polkit-kde or polkit-gnome or mate-polkit or lxqt-policykit)
# Only needed to build a medium for another architecture. Without it such an
# install is refused before the medium is touched, so it is not required.
Suggests:       qemu-user-static

%description
Creates and administers an encrypted admin container, builds install
configurations, installs Debian/Astra Linux or RedOS onto an external
LUKS-encrypted medium, and manages its logs, exchange partition and
recovery codes afterwards. The interface is a guided Tkinter GUI.

Everything this package needs comes from the distribution. The only
third-party Python library is cryptography, taken from
python3-cryptography rather than downloaded, so nothing is fetched at
install time and the package installs on a machine with no route to the
internet -- which is the environment this tool is meant for.

Note: this spec targets RedOS/RHEL-family *admin hosts*. If your install
configurations target Debian or Astra Linux media, the admin host still
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
mkdir -p %{buildroot}%{python3_sitelib}
cp -r src/portable_os_admin %{buildroot}%{python3_sitelib}/
find %{buildroot}%{python3_sitelib} -name __pycache__ -type d -exec rm -rf {} +
mkdir -p %{buildroot}/usr/bin
cat > %{buildroot}/usr/bin/portable-os-admin <<'EOF'
#!/bin/sh
exec /usr/bin/python3 -m portable_os_admin.gui.app "$@"
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
%{python3_sitelib}/portable_os_admin
/usr/bin/portable-os-admin
/usr/share/applications/portable-os-admin.desktop
/usr/share/polkit-1/actions/ru.portableosadmin.policy
/usr/share/polkit-1/rules.d/49-portable-os-admin.rules
/usr/lib/portable-os-admin/launch-root

%changelog
* Thu Aug 20 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-2
- Install as a system Python package instead of a private virtualenv, so the
  package no longer downloads anything at install time.
- Drop the Textual TUI; the GUI is the only interface.
- Ask the desktop for administrator rights through polkit.

* Thu Jul 30 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-1
- Initial packaging.
