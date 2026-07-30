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
cp -r src pyproject.toml README.md LICENSE "$STAGE/"
tar -C "$WORKDIR" -czf "$WORKDIR/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"

rpmdev-setuptree
cp "$WORKDIR/$NAME-$VERSION.tar.gz" "$HOME/rpmbuild/SOURCES/"
rpmbuild -bb packaging/rpm/portable-os-admin.spec

echo "Built package(s) are in $HOME/rpmbuild/RPMS/noarch/"
