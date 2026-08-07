#!/usr/bin/env bash
# The page's own tests. Separate from `python3 -m unittest discover -s tests`
# because they need node, which nothing else here does -- and which the device
# itself certainly does not have. Falls back to a container when there is no
# local node, so this works on a machine that only has Docker.
set -euo pipefail

cd "$(dirname "$0")"

if command -v node >/dev/null 2>&1; then
  run() { node "$@"; }
else
  echo "no local node; using node:22-alpine"
  run() {
    docker run --rm -v "$(cd ../.. && pwd):/w:ro" -w /w/tests/js \
      node:22-alpine node "$@"
  }
fi

status=0
for t in time.js clock.js telemetry.js tune.js library.js preparing.js admin.js build.js; do
  echo "== $t"
  run "$t" || status=1
  echo
done
exit $status
