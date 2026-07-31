#!/usr/bin/env bash
# Build H.264 test clips on the CONTROL machine (the X1), not on the stick --
# x264 on a 1.44 GHz Airmont would take longer than the measurement it feeds.
#
# The encode deliberately mirrors what AirPlay actually sends: H.264 High
# profile, CABAC, ~10 Mbps, 30 fps, moderate GOP. Noise is layered on so the
# stream does not compress into something unrealistically cheap to decode.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$HERE/../roles/probe/files"
DURATION="${DURATION:-60}"

mkdir -p "$OUT_DIR"

command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; exit 1; }

encode() {
  local name="$1" size="$2" bitrate="$3"
  local out="$OUT_DIR/$name.h264"
  echo "==> $name (${size}, ${bitrate}, ${DURATION}s)"
  ffmpeg -y -hide_banner -loglevel warning \
    -f lavfi -i "testsrc2=size=$size:rate=30:duration=$DURATION" \
    -vf "noise=alls=18:allf=t" \
    -c:v libx264 -profile:v high -preset medium \
    -b:v "$bitrate" -maxrate "$bitrate" -bufsize "$bitrate" \
    -x264-params "keyint=60:min-keyint=60:bframes=2:cabac=1:scenecut=0" \
    -an -f h264 "$out"
  ls -lh "$out"
}

encode clip-720p30  1280x720  6M
encode clip-1080p30 1920x1080 10M

echo
echo "Clips written to $OUT_DIR"
echo "They are gitignored; the probe role ships whatever it finds there."
