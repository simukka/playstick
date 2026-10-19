#!/usr/bin/env bash
# Give the admin app a new version: a debian/changelog entry, and the same
# number in src/admin/pyproject.toml.
#
#   scripts/bump-version.sh patch "Fixes the queue menu."    # 0.1.0 -> 0.1.1
#   scripts/bump-version.sh minor "..."                      # 0.1.0 -> 0.2.0
#   scripts/bump-version.sh major "..."                      # 0.1.0 -> 1.0.0
#   scripts/bump-version.sh 0.3.0 "..."                      # exactly that
#   make version V=minor MSG="..."
#
# WHY BOTH FILES, AND WHICH ONE COUNTS
#
# The changelog's first line is the package's version: build-deb.sh reads it
# to name the .deb, and apt reads it to decide what is an upgrade. The
# pyproject number is only what pip and the installed metadata report. Nothing
# keeps the two in step except this, so this is the way to change either.
#
# Not the stick's release id, which needs no bumping: that is a hash of what a
# deploy installs (scripts/release_id.py) and changes whenever the files do.
#
# The entry is written here rather than by dch so that nothing beyond coreutils
# is needed. The maintainer is copied from the entry before it, and the format
# is the one Debian's parser insists on -- two spaces before "*", one before
# "--", two between the address and the date.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHANGELOG="$ROOT/debian/changelog"
PYPROJECT="$ROOT/src/admin/pyproject.toml"

usage() {
  echo "usage: $0 major|minor|patch|<x.y.z> \"what changed\"" >&2
  exit 2
}
[ $# -eq 2 ] || usage
what="$1"
message="$2"
[ -n "$message" ] || usage

header="$(head -n 1 "$CHANGELOG")"
package="${header%% *}"
current="$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "$CHANGELOG")"
if ! [[ "$current" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
  echo "the current version, \"$current\", is not x.y.z; give the new one exactly" >&2
  [[ "$what" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
fi
major="${BASH_REMATCH[1]:-0}" minor="${BASH_REMATCH[2]:-0}" patch="${BASH_REMATCH[3]:-0}"

case "$what" in
  major) next="$((major + 1)).0.0" ;;
  minor) next="$major.$((minor + 1)).0" ;;
  patch) next="$major.$minor.$((patch + 1))" ;;
  *)
    [[ "$what" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || usage
    next="$what"
    ;;
esac

# Downgrading is how apt ends up refusing the package as "older than what is
# installed", so it is refused here, where the reason is obvious.
if command -v dpkg >/dev/null && ! dpkg --compare-versions "$next" gt "$current"; then
  echo "$next is not newer than $current" >&2
  exit 1
fi

maintainer="$(grep -m 1 '^ -- ' "$CHANGELOG" | sed 's/^ -- \(.*>\)  .*/\1/')"
[ -n "$maintainer" ] || { echo "no maintainer line in $CHANGELOG" >&2; exit 1; }

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
{
  printf '%s (%s) unstable; urgency=medium\n\n' "$package" "$next"
  printf '  * %s\n\n' "$message"
  printf ' -- %s  %s\n\n' "$maintainer" "$(LC_ALL=C date -R)"
  cat "$CHANGELOG"
} > "$tmp"
cat "$tmp" > "$CHANGELOG"

sed -i "s/^version = \"[^\"]*\"/version = \"$next\"/" "$PYPROJECT"
grep -q "^version = \"$next\"" "$PYPROJECT" || {
  echo "could not set the version in $PYPROJECT" >&2
  exit 1
}

echo "$package $current -> $next"
echo "  debian/changelog and src/admin/pyproject.toml updated; build with make admin-deb"
