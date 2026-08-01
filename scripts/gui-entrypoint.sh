#!/usr/bin/env bash
# Entrypoint for the GUI development container -- see Dockerfile.gui.
#
# Two jobs: put something in the library worth looking at, then hand over to
# the same daemon the appliance runs, unmodified.
#
# The generated films are deliberately named the way a real collection is,
# because most of what the library code does is undo those names: clean_title()
# cuts at the first release tag and then removes the year, and a test library
# of "movie1.mkv" would never exercise it. Each entry below is annotated with
# the behaviour it is there to show.
set -euo pipefail

LIBRARY="${PLAYSTICK_LIBRARY:-/srv/movies}"
THUMBS="${PLAYSTICK_THUMB_DIR:-/var/cache/playstick/thumbs}"
CLIP_SECONDS="${PLAYSTICK_GUI_CLIP_SECONDS:-45}"

# /run/playstick holds the mpv IPC socket and the two flag files. On the device
# systemd makes it with RuntimeDirectory=; here nothing does.
mkdir -p /run/playstick "$THUMBS" "$LIBRARY"

clip() {  # <path relative to the library> <lavfi video source>
  local out="$LIBRARY/$1" src="$2"
  mkdir -p "$(dirname "$out")"
  # ultrafast and 640x360 because nothing here measures decode cost -- that is
  # a question about a 1.44 GHz Airmont and cannot be asked on a dev machine.
  # The audio track is real so mpv's audio clock paces playback the way it does
  # on the device; --ao=null still consumes it in real time.
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "${src}=size=640x360:rate=25" \
    -f lavfi -i "sine=frequency=220:sample_rate=48000" \
    -t "$CLIP_SECONDS" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 700k \
    -c:a aac -b:a 64k \
    "$out"
  echo "    $1"
}

generate() {
  echo "==> generating a sample library under $LIBRARY (${CLIP_SECONDS}s clips)"

  # A distinct lavfi source per film, so the poster grid is something you can
  # actually tell apart once the thumbnailer has worked through it.

  # "Colour Bars" -- tag cut at 1080p, then the year.
  clip "Colour.Bars.2019.1080p.BluRay.x264-GROUP.mkv" smptebars

  # "The Long Wait" -- all lowercase, so clean_title() title-cases it.
  clip "the.long.wait.2004.720p.web-dl.mp4" testsrc2

  # "Gradient Hill" -- no release tag at all, parenthesised year.
  clip "Gradient Hill (1998).mkv" rgbtestsrc

  # One folder per film, which is the layout player_scan_depth: 2 exists for,
  # plus a poster the collection already carries: find_sidecar() prefers it
  # over extracting a frame, and this is the only tile that appears instantly.
  clip "Test Pattern Two/Test.Pattern.Two.2021.1080p.HEVC.mkv" yuvtestsrc
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "gradients=size=400x600" -frames:v 1 \
    "$LIBRARY/Test Pattern Two/poster.jpg"

  # Does NOT appear in the grid: PLAYSTICK_SKIP_PATTERN. On the device the
  # size filter catches these first and for free, but PLAYSTICK_MIN_SIZE_MB is
  # 0 here -- every clip above is a few MB -- so the regex is doing it alone.
  clip "Colour.Bars.2019.sample.mkv" testsrc

  # Two folders deep and it DOES appear -- player_scan_depth counts the
  # directories it will descend into, so "the share, and one folder per film"
  # is one level tighter than the number actually allows.
  clip "Archive/Boxed Set/Third.Level.2020.mkv" testsrc

  # Three deep, and this one does not: the walk stops descending at
  # PLAYSTICK_SCAN_DEPTH. Worth generating rather than describing, because
  # "my film is missing" is otherwise indistinguishable from a bug.
  clip "Archive/Boxed Set/Disc One/Fourth.Level.2020.mkv" testsrc

  echo "==> 5 films should appear; 2 of the 7 files are filtered on purpose"
}

if [ "${PLAYSTICK_GUI_SAMPLES:-1}" != "0" ] &&
   [ -z "$(find "$LIBRARY" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
  generate
fi

exec "$@"
