#!/usr/bin/env bash
# Entrypoint for the GUI development container -- see Dockerfile.gui.
#
# Four jobs: write the settings the daemon will read, put something in the
# library worth looking at, push it into the database the daemon reads, then
# hand over to the same daemon the appliance runs, unmodified.
#
# The third one is new and stands in for the desktop admin app. The daemon no
# longer discovers films -- its library is a table something else fills over
# /api/admin/films -- so a container that only generated files would serve an
# empty grid. gui-seed.py does what the admin app does, minus the HTTP, since
# the daemon it would POST to has not started yet.
#
# The generated films are deliberately named the way a real collection is,
# because most of what the library code does is undo those names: clean_title()
# cuts at the first release tag and then removes the year, and a test library
# of "movie1.mkv" would never exercise it. Each entry below is annotated with
# the behaviour it is there to show.
set -euo pipefail

LIBRARY=/srv/movies
# The film table, the metadata edits, the posters and the resume positions.
# On the device systemd makes this directory with StateDirectory=.
STATE=/var/lib/playstick
CLIP_SECONDS="${PLAYSTICK_GUI_CLIP_SECONDS:-45}"
# One film long enough to skip around in. Everything else here is under a
# minute, and on a forty-five second clip every jump the skip buttons can make
# lands at the end -- a hold of three seconds is half an hour of film, so the
# control cannot be judged at all without something that long to move inside.
LONG_SECONDS="${PLAYSTICK_GUI_LONG_SECONDS:-900}"
SETTINGS="${PLAYSTICK_SETTINGS:-/etc/playstick/settings.toml}"

# PLAYSTICK_GUI_NAS is "host" or "host:port". Split here rather than in the
# heredoc: an unset value there would render `probe_port =` with nothing after
# it, which is not a TOML syntax error the daemon reports -- it falls back to
# every default at once and the deviations below silently stop applying.
GUI_NAS="${PLAYSTICK_GUI_NAS:-}"
NAS_HOST="${GUI_NAS%%:*}"
NAS_PORT="${GUI_NAS##*:}"
if [ -z "$NAS_PORT" ] || [ "$NAS_PORT" = "$GUI_NAS" ]; then
  NAS_PORT=445
fi

# /run/playstick holds the mpv IPC socket and the two flag files. On the device
# systemd makes it with RuntimeDirectory=; here nothing does.
mkdir -p /run/playstick "$STATE" "$LIBRARY" "$(dirname "$SETTINGS")"

# --- the settings ---------------------------------------------------------
# The same file the appliance gets from roles/player/templates/settings.toml.j2,
# written here from the handful of knobs a developer turns. Only the DEVIATIONS
# from the device are commented; everything absent takes config.py's own
# default, which is the point of writing a partial file -- a value nobody
# overrides is exercised exactly as it ships.
#
# The knobs, all optional, all documented in the Makefile:
#   PLAYSTICK_GUI_PROJECTOR        "fake" (default), "" for the NullProjector
#   PLAYSTICK_GUI_PROJECTOR_IDLE   minutes before the lamp goes out (2)
#   PLAYSTICK_GUI_NAS              host[:port] to probe, for the wake button
#   PLAYSTICK_GUI_NAS_MAC          MAC to aim the magic packet at
cat > "$SETTINGS" <<TOML
[server]
port = 80
ui_file = "/usr/local/share/playstick/ui.html"
# player_allow_networks is deliberately empty: the Docker bridge is inside
# 172.16.0.0/12 and would pass the real list anyway, so enforcing it here would
# only ever reject the host and teach nothing.
allow_networks = []

[library]
path = "$LIBRARY"
# 300 s on the device. All this paces now is one stat asking whether the share
# is still there, and a container's is always there -- but the interval is also
# how quickly the page recovers after PLAYSTICK_GUI_NAS is pointed at a dead
# port and back, which is worth watching at a human pace.
scan_interval = 30

# The one file the daemon writes. A named volume in compose, so `docker compose
# down -v` resets it -- and resetting it means an empty library until
# playstick-gui-seed runs again, which the entrypoint does on every start.
[database]
path = "$STATE/playstick.db"

[nas]
# Empty by default: power reads "unknown" and the page behaves exactly as it
# does with no NAS, which is the shipped state. Point PLAYSTICK_GUI_NAS at a
# port nothing is listening on -- "127.0.0.1:9999" -- to drive the whole
# unreachable-library path, banner and button included, on a laptop.
host = "$NAS_HOST"
probe_port = $NAS_PORT
mac = "${PLAYSTICK_GUI_NAS_MAC:-00:11:32:aa:bb:cc}"
# Seconds, not the device's minute-scale values: the point here is to watch the
# banner change while looking at it.
probe_interval = 5
probe_interval_down = 3
probe_interval_waking = 2
wake_window = 20
wake_cooldown = 5
# Nothing in a container is going to reset-failed a mount unit that does not
# exist. Naming it explicitly stops the daemon shelling out to systemd-escape
# to derive one it will then fail to use.
mount_unit = "playstick-gui-none.mount"

[thumbs]
# Identical to the unit's: poster extraction is one part of this that a
# container reproduces exactly.
args = ["--no-config", "--really-quiet", "--frames=1", "--aid=no", "--sid=no",
        "--hwdec=no", "--vf=scale=320:-2", "--vo=image",
        "--vo-image-format=jpg", "--vo-image-jpeg-quality=80"]

[mpv]
binary = "/usr/bin/mpv"
socket = "/run/playstick/mpv.sock"
# mpv's DRM backend wants a VT for console switching and there is none. Empty
# disables it; the daemon logs the absence and carries on.
tty = ""
# The device's argv minus the display: --vo=drm, --drm-device, --drm-connector,
# --drm-mode and --hwdec have no meaning in a container. Everything that shapes
# the daemon's view of mpv is kept, and --idle=no / --keep-open=no are as
# load-bearing here as there -- mpv exiting at end of file is how the daemon
# learns the film is over. --really-quiet is dropped so mpv's complaints reach
# \`docker compose logs\`.
args = ["--no-config", "--vo=null", "--ao=null", "--hwdec=no", "--profile=fast",
        "--video-sync=audio", "--framedrop=vo", "--interpolation=no",
        "--osd-level=0", "--no-osc", "--input-terminal=no", "--idle=no",
        "--keep-open=no", "--sub-auto=fuzzy", "--cache=yes",
        "--demuxer-max-bytes=48MiB", "--demuxer-max-back-bytes=16MiB",
        "--cache-on-disk=no"]

[display]
busy_file = "/run/playstick/playing"
restore_file = "/run/playstick/restore-airplay"
airplay_unit = "uxplay-kms.service"
# The port the interlock watches. Nothing listens on it until you run
# \`fake-airplay\`, which is the point -- see scripts/gui-fake-airplay.py.
airplay_port = 7000
# Films play silently on the device (player_audio: false), which also hides the
# page's volume controls. Switched on here so that half of the UI can be looked
# at; set has_audio = false to see the layout as it currently ships.
has_audio = true

[projector]
# A projector made of arithmetic, so the preparing view can be looked at and its
# wording argued about on a laptop. "fake" warms up in three seconds instead of
# forty and honours the two rules the sequence is built around: standby accepts
# nothing but PON, and the lamp is not lit the instant PON returns.
#
# Set PLAYSTICK_GUI_PROJECTOR= (empty) to get the NullProjector instead, which
# is what the appliance ships with and the path that must never stop a film.
model = "${PLAYSTICK_GUI_PROJECTOR-fake}"
input = "HD3"
# Minutes. Short enough to watch the lamp go out over a cup of tea.
idle_minutes = ${PLAYSTICK_GUI_PROJECTOR_IDLE:-2}
tick_seconds = 5
TOML

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

  # Does NOT appear in the grid: [library] skip_pattern. On the device the
  # size filter catches these first and for free, but min_size_mb is
  # 0 here -- every clip above is a few MB -- so the regex is doing it alone.
  clip "Colour.Bars.2019.sample.mkv" testsrc

  # Two folders deep and it DOES appear -- player_scan_depth counts the
  # directories it will descend into, so "the share, and one folder per film"
  # is one level tighter than the number actually allows.
  clip "Archive/Boxed Set/Third.Level.2020.mkv" testsrc

  # Three deep, and this one does not: the walk stops descending at
  # [library] scan_depth. Worth generating rather than describing, because
  # "my film is missing" is otherwise indistinguishable from a bug.
  clip "Archive/Boxed Set/Disc One/Fourth.Level.2020.mkv" testsrc

  # --- the transport fixtures ---------------------------------------------
  # The two controls that only exist while a film is playing: skipping, and
  # subtitles. Neither can be judged on a forty-five second clip.

  # "The Feature" -- fifteen minutes of it, with subtitles in two languages
  # burned into the container as text tracks. The one film here that both
  # controls added with them can be judged on: hold the forward button for
  # three seconds and the label should climb to about +30:00 and the jump
  # should clamp near the end rather than ending the film, and the CC button
  # should offer English and Suomi over Off.
  #
  # Subtitle tracks INSIDE the container on purpose. The extracted SRTs prep
  # writes live under .playstick/subs/ and only reach mpv via the index, which
  # nothing here generates -- but mpv finds a container's own text tracks
  # whatever the daemon passes, and the page treats the two identically
  # because both come back through mpv's track-list.
  echo "    generating a ${LONG_SECONDS}s feature (this one takes a moment)"
  : > /tmp/feature.eng.srt
  : > /tmp/feature.fin.srt
  minute=0
  while [ "$minute" -lt $(( (LONG_SECONDS + 59) / 60 )) ]; do
    start=$(printf "00:%02d:00,000" "$minute")
    end=$(printf "00:%02d:55,000" "$minute")
    printf '%d\n%s --> %s\nMinute %d of the feature.\n\n' \
      "$((minute + 1))" "$start" "$end" "$minute" >> /tmp/feature.eng.srt
    printf '%d\n%s --> %s\nElokuvan minuutti %d.\n\n' \
      "$((minute + 1))" "$start" "$end" "$minute" >> /tmp/feature.fin.srt
    minute=$((minute + 1))
  done
  ffmpeg -y -hide_banner -loglevel error \
    -f lavfi -i "testsrc2=size=640x360:rate=25" \
    -f lavfi -i "sine=frequency=330:sample_rate=48000" \
    -i /tmp/feature.eng.srt -i /tmp/feature.fin.srt \
    -map 0:v -map 1:a -map 2:s -map 3:s -t "$LONG_SECONDS" \
    -c:v libx264 -preset ultrafast -pix_fmt yuv420p -b:v 400k \
    -c:a aac -b:a 64k -c:s srt \
    -metadata:s:a:0 language=eng \
    -metadata:s:s:0 language=eng -metadata:s:s:1 language=fin \
    "$LIBRARY/The.Feature.2024.1080p.BluRay.x264-GROUP.mkv"
  rm -f /tmp/feature.eng.srt /tmp/feature.fin.srt
  echo "    The.Feature.2024.1080p.BluRay.x264-GROUP.mkv  (${LONG_SECONDS}s, 2 subtitle tracks)"

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

  echo "==> 14 files -> 12 tiles now, 11 after a prep run"
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

# --- into the database ----------------------------------------------------
# Unconditional, and an upsert rather than a replace, so it is cheap on a
# container that has already been started once and correct on one whose volume
# was just reset. --reconcile so a film deleted from a bind-mounted library
# stops appearing, which is the behaviour a developer pointing
# PLAYSTICK_GUI_LIBRARY at their own films expects.
#
# Never fatal: a seeder that cannot run must not be the reason the daemon does
# not start. An empty grid is a thing you can see and debug; a container that
# exits at boot is not.
playstick-gui-seed --library "$LIBRARY" --reconcile || \
  echo "==> seed failed; the library will be empty until something syncs it" >&2

exec "$@"
