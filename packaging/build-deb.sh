#!/bin/sh
# Builds the portable-os-admin .deb from this checkout.
#
# Run on a real Debian/Ubuntu/Astra Linux host with:
#   sudo apt-get install -y build-essential debhelper devscripts
#
# dpkg-buildpackage always looks for ./debian at the repo root, so this
# script stages packaging/debian there for the duration of the build and
# removes it afterwards -- the canonical files live under packaging/debian
# so the repo root stays clean the rest of the time.
set -e

cd "$(dirname "$0")/.."

if [ -e debian ]; then
    echo "error: ./debian already exists -- refusing to overwrite" >&2
    exit 1
fi

cleanup() {
    rm -rf debian
}
trap cleanup EXIT

cp -r packaging/debian debian
dpkg-buildpackage -us -uc -b

echo "Built package(s) are in the parent directory of this checkout."
