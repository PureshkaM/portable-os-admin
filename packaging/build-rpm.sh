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
# Встроенный рантайм: свой Python с Tk и cryptography.
#
# Раньше здесь стояла проверка на %{python3_sitelib} и отказ собирать без
# python3-devel: пакет ставил код в системный site-packages, и путь к нему был
# свойством хоста. Теперь программа живёт в собственном префиксе вместе со
# своим интерпретатором, поэтому системный Python не нужен ни при сборке, ни
# при установке -- а именно из-за него пакет и был неустановим на РЕД ОС, где
# python3-tkinter отсутствует.
#
# Рантайм собирается до rpmbuild, а не внутри него: он тянется из сетевого
# репозитория дистрибутива, и сборка пакета не должна зависеть от сети в тот
# момент, когда её уже поздно спрашивать.
RUNTIME=${RUNTIME:-$PWD/runtime}
if [ ! -x "$RUNTIME/usr/bin/python3.11" ]; then
    echo "собираю рантайм в $RUNTIME"
    sh packaging/rpm/build-runtime.sh "$RUNTIME"
else
    echo "рантайм уже собран: $RUNTIME"
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
# Рантайм едет вместе с исходниками: spec берёт его из runtime/ в распакованном
# архиве. Копируется, а не собирается заново -- сборка уже прошла выше и
# проверила себя запуском.
cp -a "$RUNTIME" "$STAGE/runtime"
tar -C "$WORKDIR" -czf "$WORKDIR/$NAME-$VERSION.tar.gz" "$NAME-$VERSION"

# The tree is made by hand rather than with rpmdev-setuptree: that tool comes
# from rpmdevtools, which is not everywhere, while rpmbuild itself is.
TOPDIR="${RPM_TOPDIR:-$HOME/rpmbuild}"
mkdir -p "$TOPDIR"/SOURCES "$TOPDIR"/SPECS "$TOPDIR"/BUILD "$TOPDIR"/RPMS "$TOPDIR"/SRPMS
cp "$WORKDIR/$NAME-$VERSION.tar.gz" "$TOPDIR/SOURCES/"
# `--target x86_64` явно: пакет несёт двоичный рантайм этой архитектуры, и на
# машине другой архитектуры rpmbuild иначе отказывается со словами «No
# compatible architectures found for build». Сборка при этом честная -- ничего
# не компилируется, содержимое рантайма уже готово.
rpmbuild --define "_topdir $TOPDIR" --target x86_64 -bb packaging/rpm/portable-os-admin.spec

echo "Готовый пакет: $TOPDIR/RPMS/x86_64/"
