#!/bin/sh
# Builds the portable-os-admin .rpm from this checkout.
#
# Run on the real RedOS/RHEL-family host with:
#   sudo dnf install -y rpmdevtools rpm-build python3-devel
#
# python3-devel is not optional here (see the %{python3_sitelib} check
# below): without it this script refuses to build at all.
set -e

cd "$(dirname "$0")/.."

NAME=portable-os-admin
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -1)
if [ -z "$VERSION" ]; then
    echo "error: could not read version from pyproject.toml" >&2
    exit 1
fi

# %{python3_sitelib} only means something once python3-rpm-macros (pulled in
# by python3-devel) is installed: it is how rpm's macro table learns where
# *this RHEL-family host's* python3 keeps site-packages -- a fact about the
# target distribution's packaging convention, not something this script, or
# rpmbuild itself, can work out on its own. This used to be worked around by
# falling back to this host's own `sysconfig` and building anyway (see the
# spec's git history): on a Debian host that produced a .rpm shipping the
# code to /usr/lib/python3/dist-packages, a path no RHEL-family python3
# searches -- `dnf install` succeeded and `portable-os-admin` then failed
# with ModuleNotFoundError on first launch. There is no host-independent
# value to fall back to, so refuse instead of shipping a package that only
# looks like it works.
SITELIB=$(rpm --eval '%{?python3_sitelib}' 2>/dev/null || true)
if [ -z "$SITELIB" ] || [ "$SITELIB" = '%{python3_sitelib}' ]; then
    cat >&2 <<'EOF'
error: %{python3_sitelib} is not defined by this rpm installation.
Install python3-devel (RedOS/RHEL: dnf install python3-devel) and retry --
this package can only be built for real on a RHEL-family host, because the
install path it needs to write to is that host's own packaging convention
and there is no way to guess it from outside that family.
EOF
    exit 1
fi

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

STAGE="$WORKDIR/$NAME-$VERSION"
mkdir -p "$STAGE"
# packaging/ has to travel with the sources: the spec installs the polkit
# action, its rule and the launcher out of packaging/polkit, and without them
# in the tarball the build fails at %install rather than at %prep, which is a
# confusing place to find out.
cp -r src packaging pyproject.toml README.md LICENSE "$STAGE/"
tar -C "$WORKDIR" -czf "$WORKDIR/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"

# The tree is made by hand rather than with rpmdev-setuptree: that tool comes
# from rpmdevtools, which is not everywhere, while rpmbuild itself is.
TOPDIR="${RPM_TOPDIR:-$HOME/rpmbuild}"
mkdir -p "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/BUILD "$TOPDIR"/RPMS "$TOPDIR"/SRPMS
cp "$WORKDIR/$NAME-$VERSION.tar.gz" "$TOPDIR/SOURCES/"
rpmbuild --define "_topdir $TOPDIR" -bb packaging/rpm/portable-os-admin.spec

echo "Built package(s) are in $TOPDIR/RPMS/noarch/"
