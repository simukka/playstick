#!/usr/bin/env bash
# Build the admin app's .deb in a container, then install it in a fresh
# container of every release it is meant for and run the smoke test there.
#
#   make admin-deb                       # -> dist/playstick_<ver>_all.deb
#   TEST_IMAGES=debian:trixie make admin-deb
#
# WHICH RELEASES, AND WHY ONLY THOSE
#
# The package carries no Python, Qt or PySide6 of its own: it depends on the
# distribution's python3-pyside6.* and python3-ruamel.yaml, which keeps it to a
# few hundred kilobytes. The cost is that it installs only where PySide6 is
# packaged. Checked October 2026:
#
#   Debian 13 trixie     PySide6 6.8.2    supported
#   Ubuntu 26.04 LTS     PySide6 6.10.2   supported
#   Debian 12, Ubuntu 24.04               no PySide6 packages at all
#
# ONE BUILD, SEVERAL TESTS
#
# It is Architecture: all, pure Python installed into /usr/lib/python3/
# dist-packages, and the package names are the same in every release above --
# so one .deb serves them all, and building it per release would only produce
# identical files. It is built on the oldest supported release, so that
# nothing the build tools add can demand something newer, and then installed
# with --no-install-recommends in each, which proves the Depends line alone is
# enough. Should a release ever need its own build, BUILD_IMAGE builds there.
#
# Only Docker is needed on the host. Nothing is written to the working tree
# but dist/: the build runs on a copy of the files git knows about (tracked
# and untracked, minus anything ignored), so .venv-admin and node_modules stay
# out of it and no build/ or *.egg-info lands in src/admin.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$ROOT/dist"
BUILD_IMAGE="${BUILD_IMAGE:-debian:trixie}"
TEST_IMAGES="${TEST_IMAGES:-debian:trixie ubuntu:26.04}"

command -v docker >/dev/null || { echo "docker not found" >&2; exit 1; }
mkdir -p "$OUT"

source_tar() {
  (cd "$ROOT" && git ls-files -z --cached --others --exclude-standard \
     | tar --null --files-from=- -cf -)
}

echo "==> building in $BUILD_IMAGE"
# --no-same-owner because the archive carries the host uid, which a container
# may not be able to map; files reach dist/ owned by whoever root is mapped to,
# which under rootless Docker is the user who ran this.
source_tar | docker run --rm -i -v "$OUT:/out" "$BUILD_IMAGE" bash -euc '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -qq -y --no-install-recommends build-essential >/dev/null
  mkdir -p /build/playstick && cd /build/playstick && tar --no-same-owner -xf -
  apt-get build-dep -qq -y ./ >/dev/null
  dpkg-buildpackage -b -us -uc
  for deb in ../*.deb; do
    cp "$deb" /out/
    echo "built $(basename "$deb")"
  done
'

VERSION="$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "$ROOT/debian/changelog")"
DEB="playstick_${VERSION}_all.deb"
[ -f "$OUT/$DEB" ] || { echo "expected $OUT/$DEB, not found" >&2; exit 1; }

# The smoke test alone in a directory, so it imports the installed package and
# not the copy beside it in src/admin.
for image in $TEST_IMAGES; do
  echo "==> installing in $image"
  docker run --rm -v "$OUT/$DEB:/tmp/$DEB:ro" \
      -v "$ROOT/src/admin/smoke_test.py:/tmp/smoke/smoke_test.py:ro" \
      "$image" bash -euc '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -qq -y --no-install-recommends "/tmp/'"$DEB"'" >/dev/null
    command -v playstick
    cd /tmp/smoke && QT_QPA_PLATFORM=offscreen python3 -I smoke_test.py
  '
done

echo "==> $OUT/$DEB ($(du -h "$OUT/$DEB" | cut -f1))"
