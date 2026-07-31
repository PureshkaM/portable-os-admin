Name:           portable-os-admin
Version:        0.1.0
Release:        1%{?dist}
Summary:        Admin tool for portable LUKS-encrypted OS media

License:        MIT
URL:            https://github.com/PureshkaM/portable-os-admin
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       python3 >= 3.9
Requires:       python3-pip
Requires:       python3-tkinter
Requires:       parted
Requires:       cryptsetup

%description
Creates and administers an encrypted admin container, builds install
configurations, installs Debian/Astra Linux or RedOS onto an external
LUKS-encrypted medium, and manages its logs/exchange partition/recovery
codes afterwards. Ships both a guided Tkinter GUI (portable-os-admin, the
default) and a Textual TUI (portable-os-admin-tui) built on the same
backend.

The Python dependencies this project actually needs (textual>=0.58,
cryptography>=41.0) are not reliably available as matching system
packages across RHEL-family hosts, so this package installs them into a
private virtualenv at /opt/portable-os-admin/venv during %%post instead
of declaring them as rpm Requires -- see packaging/README.md in the
source tree for why, and what that means for offline installs. The GUI's
own dependency, Tkinter, is the one exception: it's a C extension bound to
the system Python (confirmed present in RedOS 8.0's own repodata as
python3-tkinter), not installable from PyPI, so it comes from the
Requires line above and the venv is created with --system-site-packages
so it can see it.

Note: this spec targets RedOS/RHEL-family *admin hosts*. If your install
configurations target Debian or Astra Linux media, the admin host still
needs debootstrap available -- RedOS's own repositories were not found to
carry it, so that has to come from elsewhere (see docs/ARCHITECTURE.md
"Known limitations" in the source tree).

%prep
%setup -q

%build
# nothing to build -- pure Python, installed via pip into a private venv at %%post time

%install
rm -rf %{buildroot}
mkdir -p %{buildroot}/usr/share/%{name}/app
cp -r src pyproject.toml README.md %{buildroot}/usr/share/%{name}/app/
mkdir -p %{buildroot}/usr/bin
cat > %{buildroot}/usr/bin/portable-os-admin <<'EOF'
#!/bin/sh
exec /opt/portable-os-admin/venv/bin/portable-os-admin "$@"
EOF
chmod 755 %{buildroot}/usr/bin/portable-os-admin
cat > %{buildroot}/usr/bin/portable-os-admin-tui <<'EOF'
#!/bin/sh
exec /opt/portable-os-admin/venv/bin/portable-os-admin-tui "$@"
EOF
chmod 755 %{buildroot}/usr/bin/portable-os-admin-tui
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

%post
VENV=/opt/portable-os-admin/venv
APP=/usr/share/portable-os-admin/app
if [ ! -x "$VENV/bin/python3" ]; then
    # --system-site-packages: the GUI needs Tkinter (python3-tkinter Requires
    # above), a C extension bound to the system Python that pip cannot
    # install into an isolated venv.
    python3 -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install --quiet "$APP"

%postun
if [ "$1" = "0" ]; then
    rm -rf /opt/portable-os-admin/venv
fi

%files
%license LICENSE
/usr/share/%{name}/app
/usr/bin/portable-os-admin
/usr/bin/portable-os-admin-tui
/usr/share/applications/portable-os-admin.desktop

%changelog
* Thu Jul 30 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-1
- Initial packaging.
