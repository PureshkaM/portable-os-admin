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
Requires:       parted
Requires:       cryptsetup

%description
Creates and administers an encrypted admin container, builds install
configurations, installs Debian/Astra Linux or RedOS onto an external
LUKS-encrypted medium, and manages its logs/exchange partition/recovery
codes afterwards.

The Python dependencies this project actually needs (textual>=0.58,
cryptography>=41.0) are not reliably available as matching system
packages across RHEL-family hosts, so this package installs them into a
private virtualenv at /opt/portable-os-admin/venv during %%post instead
of declaring them as rpm Requires -- see packaging/README.md in the
source tree for why, and what that means for offline installs.

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

%post
VENV=/opt/portable-os-admin/venv
APP=/usr/share/portable-os-admin/app
if [ ! -x "$VENV/bin/python3" ]; then
    python3 -m venv "$VENV"
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

%changelog
* Thu Jul 30 2026 PureshkaM <xripmax@gmail.com> - 0.1.0-1
- Initial packaging.
