#!/bin/sh
# Builds the portable-os-admin .rpm from this checkout.
# Requires rpmbuild. The application package contains platform-independent code.
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
# from rpmdevtools, which is not everywhere, while rpmbuild itself is.
TOPDIR="${RPM_TOPDIR:-$HOME/rpmbuild}"
mkdir -p "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/BUILD "$TOPDIR"/RPMS "$TOPDIR"/SRPMS
cp "$WORKDIR/$NAME-$VERSION.tar.gz" "$TOPDIR/SOURCES/"
rpmbuild --define "_topdir $TOPDIR" -bb packaging/rpm/portable-os-admin.spec

echo "Готовый пакет: $TOPDIR/RPMS/noarch/"
