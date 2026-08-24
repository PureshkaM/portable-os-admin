#!/bin/sh
# Builds the portable-os-admin .rpm from this checkout.
#
# Run on the real RedOS/RHEL-family host with:
#   sudo dnf install -y rpmdevtools rpm-build
set -e

cd "$(dirname "$0")/.."

NAME=portable-os-admin
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' pyproject.toml | head -1)
if [ -z "$VERSION" ]; then
    echo "error: could not read version from pyproject.toml" >&2
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
# from rpmdevtools, which is not everywhere, while rpmbuild itself is. This
# also lets the build run on a Debian host (its `rpm` package carries
# rpmbuild), which is how the .rpm gets checked without a RedOS machine.
TOPDIR="${RPM_TOPDIR:-$HOME/rpmbuild}"
mkdir -p "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/BUILD "$TOPDIR"/RPMS "$TOPDIR"/SRPMS
cp "$WORKDIR/$NAME-$VERSION.tar.gz" "$TOPDIR/SOURCES/"
# --nodeps when asked: on a non-RHEL host the BuildRequires cannot be
# satisfied, but the spec itself, the file list and the payload can still be
# checked. On a real RedOS host, leave RPM_NODEPS unset.
rpmbuild --define "_topdir $TOPDIR" ${RPM_NODEPS:+--nodeps} -bb packaging/rpm/portable-os-admin.spec

echo "Built package(s) are in $TOPDIR/RPMS/noarch/"
