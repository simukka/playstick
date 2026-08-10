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

# Films with more than one audio track, which is what the headphone feature is
# for: several people watching one silent projector, each hearing their own
# language. The tones differ per track on purpose -- which language is playing
# has to be something you can HEAR, not something you infer from a tick mark.
multiclip() {  # <path> <lavfi video> <freq:lang> [freq:lang ...]
  local name="$1" out="$LIBRARY/$1" src="$2"
  shift 2
  mkdir -p "$(dirname "$out")"
  local argv=(ffmpeg -y -hide_banner -loglevel error
              -f lavfi -i "${src}=size=640x360:rate=25")
  local maps=(-map 0:v) meta=() i=0 spec
  for spec in "$@"; do
    argv+=(-f lavfi -i "sine=frequency=${spec%%:*}:sample_rate=48000")
    maps+=(-map "$((i + 1)):a")
    meta+=(-metadata:s:a:$i "language=${spec##*:}")
    i=$((i + 1))
  done
  "${argv[@]}" "${maps[@]}" -t "$CLIP_SECONDS" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 700k \
    -c:a aac -b:a 64k "${meta[@]}" "$out"
  echo "    $name  ($# audio tracks)"
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

  # --- the headphone-audio fixtures ---------------------------------------
  # Everything below exists to be run through playstick-prep.py, which is what
  # extracts the per-language tracks the phones play. Without prep they show up
  # in the grid like anything else and the sheet says so.

  # "Two Voices" -- the one film where the Language list has a real choice in
  # it. 220 Hz for English, 660 Hz for Finnish: switching in the sheet should
  # audibly change pitch, which is the only way to prove the right track is
  # playing rather than merely that something is.
  multiclip "Two.Voices.2023.1080p.BluRay.x264-GROUP.mkv" smptebars 220:eng 660:fin

  # "Late Sound" -- audio that starts 1.5 s after the video, which is the whole
  # reason prep passes aresample=first_pts=0. Without that filter this film is
  # permanently 1.5 s out of lip sync and the page cannot discover it: drift
  # correction only ever measures CHANGE, and a constant offset never changes.
  # ffprobe'ing the extracted track and seeing start_time=0 is the test.
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "testsrc2=size=640x360:rate=25" \
    -itsoffset 1.5 -f lavfi -i "sine=frequency=440:sample_rate=48000" \
    -map 0:v -map 1:a -t "$CLIP_SECONDS" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 700k \
    -c:a aac -b:a 64k -metadata:s:a:0 language=eng \
    "$LIBRARY/Late.Sound.2022.1080p.WEB-DL.mkv"
  echo "    Late.Sound.2022.1080p.WEB-DL.mkv  (audio offset by 1.5s)"

  # "Silent Film" -- no audio stream at all. The sheet has to say so rather
  # than offering a language that would 404 on the first request.
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "rgbtestsrc=size=640x360:rate=25" -t "$CLIP_SECONDS" -an \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 700k \
    "$LIBRARY/Silent.Film.1927.1080p.BluRay.mkv"
  echo "    Silent.Film.1927.1080p.BluRay.mkv  (no audio)"

  # --- the episodic-television fixtures -------------------------------------
  # Skipped by playstick-prep.py, and by the two different mechanisms it has:
  # the first is only recognisable from the folder it sits in, the second only
  # from its own name. Note that the DAEMON's fallback walk has no episode
  # filter -- it is prep's job -- so before a prep run these two do appear.

  clip "The Wire/Season 1/The.Wire.S01E03.1080p.mkv" testsrc
  clip "friends.1x02.the.one.with.the.sonogram.avi" testsrc2

  # And the one that must SURVIVE. A title beginning "12x12" looks exactly like
  # a season/episode number, and a tool that quietly hides somebody's film is
  # one nobody trusts with the rest of the library. If this tile ever stops
  # appearing after a prep run, EPISODE_RE has been loosened too far.
  clip "12x12 A Cabin Story (2020).mkv" rgbtestsrc

  echo "==> 13 files -> 11 tiles now, 10 after a prep run"
  echo "==>   now:  the sample and the too-deep Fourth Level are filtered"
  echo "==>   prep: drops the 2 episodic files, and indexes Fourth Level --"
  echo "==>         its --max-depth is 3, the daemon's walk stops at 2"
  echo "==> for headphone audio, prepare them first:"
  echo "      playstick-prep.py --library $LIBRARY --transcode never \\"
  echo "          --verify none --no-posters --min-duration 10 --min-size-mb 0"
}

if [ "${PLAYSTICK_GUI_SAMPLES:-1}" != "0" ] &&
   [ -z "$(find "$LIBRARY" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
  generate
fi

exec "$@"
