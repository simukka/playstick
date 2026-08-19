"""Every tunable, read from the environment exactly once at import.

The unit file is the documentation for these -- see playstick-web.service.j2,
which sets them and explains the ones whose values are load-bearing rather
than preference. Nothing here does work; it only decides what the rest will do.
"""

import ipaddress
import os
import re
import shlex
import sys
import threading
import time


BIND = os.environ.get("PLAYSTICK_BIND", "0.0.0.0")
PORT = int(os.environ.get("PLAYSTICK_PORT", "8080"))
UI_FILE = os.environ.get("PLAYSTICK_UI", "/usr/local/share/playstick/ui.html")

LIBRARY = os.environ.get("PLAYSTICK_LIBRARY", "/srv/movies")
EXTENSIONS = tuple(
    e if e.startswith(".") else "." + e
    for e in os.environ.get("PLAYSTICK_EXTENSIONS", ".mkv .mp4 .m4v .avi .mov").split()
)
MIN_SIZE = int(os.environ.get("PLAYSTICK_MIN_SIZE_MB", "100") or 0) * 1024 * 1024
SCAN_DEPTH = max(1, int(os.environ.get("PLAYSTICK_SCAN_DEPTH", "2") or 2))
SCAN_INTERVAL = max(30, int(os.environ.get("PLAYSTICK_SCAN_INTERVAL", "300") or 300))
SKIP_PATTERN = os.environ.get("PLAYSTICK_SKIP_PATTERN", r"sample|trailer|extras?\b")

# The index scripts/playstick-prep.py writes on the developer machine. When it
# is present the library comes from there instead of from a walk of the share,
# which is the difference between a poster grid that appears and one that has
# to stat every file over CIFS first. Anything wrong with it -- missing,
# unparseable, a schema from the future -- falls back to the walk, because a
# stale index must never be the reason a child cannot watch a film.
# Unset means "the usual place"; set-but-empty means "ignore any index there
# is". The distinction is load-bearing rather than pedantic: the unit always
# writes an Environment= line, so player_index_file: "" arrives here as an
# empty string, and treating that as "unset" would make the documented way of
# switching the index off do nothing at all.
_index_env = os.environ.get("PLAYSTICK_INDEX")
INDEX_FILE = (os.path.join(LIBRARY, "playstick-library.json")
              if _index_env is None else _index_env)
INDEX_SCHEMA = 1
# Runtime edits made from the desktop admin view -- a title corrected, a genre
# fixed, a film hidden from the children's grid -- kept in a small JSON sidecar
# keyed by the same opaque id the index uses. Deliberately a SEPARATE file from
# the index above, and that separation is the whole feature: the index is
# regenerated wholesale on a developer machine by playstick-prep.py, so an edit
# stored inside it would be erased on the next prep run. Stored beside it here,
# an edit is an overlay merged on top of whatever the index says and survives
# any number of regenerations. Unset means "the usual place"; set-but-empty
# means "no overlay", the same load-bearing distinction INDEX_FILE draws and
# for the same reason -- the unit always writes an Environment= line.
_overlay_env = os.environ.get("PLAYSTICK_OVERLAY")
OVERLAY_FILE = (os.path.join(LIBRARY, "playstick-overlay.json")
                if _overlay_env is None else _overlay_env)
# Subtitles the prep tool extracted. Off means they are not passed to mpv at
# all, which is not the same as mpv having none to choose.
SUBTITLES = (os.environ.get("PLAYSTICK_SUBTITLES", "1") or "1").lower() in ("1", "true", "yes")

# Per-language audio for headphones, also from the prep tool.
#
# This is the only sound the appliance has. The projector has no speakers, and
# HAS_AUDIO below is false because Cherry Trail's HDMI audio has never produced
# a sample here -- so a film plays silently on the screen and everybody who
# wants to hear it opens this page on their own phone, picks a language, and
# listens on their own headphones. Two people can pick differently.
#
# Off hides the control on the page rather than showing one that does nothing,
# which is the same choice HAS_AUDIO makes about the volume buttons.
PHONE_AUDIO = (os.environ.get("PLAYSTICK_PHONE_AUDIO", "1") or "1").lower() in ("1", "true", "yes")
# Bytes per second per listener, after an initial burst. Safari pulls a
# progressive media file as fast as the socket will go, and every byte of it
# crosses this box's single SDIO-attached Wi-Fi radio TWICE -- once reading the
# track from the NAS, once writing it to the phone -- while the film's own
# ~4 Mbps CIFS read is on the same radio. Unpaced, somebody choosing a language
# is indistinguishable from the film stuttering. 0 disables the pacing.
PHONE_AUDIO_BPS = int(os.environ.get("PLAYSTICK_PHONE_AUDIO_KBPS", "384") or 0) * 1000 // 8
# Seconds of audio to hand over at full speed before the pacing starts, so that
# playback begins immediately and has something to survive a hiccup with.
PHONE_AUDIO_BURST = float(os.environ.get("PLAYSTICK_PHONE_AUDIO_BURST", "30") or 0)
# Concurrent audio responses. Each one holds a thread for as long as somebody
# is listening, so a phone that reconnects on every seek must not be able to
# accumulate them until the server has no threads left for the page itself.
PHONE_AUDIO_STREAMS = max(1, int(os.environ.get("PLAYSTICK_PHONE_AUDIO_STREAMS", "6") or 6))

# Matched against the WHOLE path, unlike the thumbnail route's startswith-and-
# slice. That is the load-bearing detail rather than a stylistic one: "..",
# "%2e%2e", an uppercased id and a three-digit track number all simply fail to
# match and fall through to the 404 at the bottom of do_GET.
AUDIO_ROUTE_RE = re.compile(r"^/api/audio/([0-9a-f]{16})/([0-9]{1,2})$")
# Small enough that the pacing below has fine-grained control over the rate and
# that a client dropping out is noticed promptly; large enough that a two-hour
# soundtrack is not a million syscalls.
# Deliberately small, and the arithmetic is the reason. The pacing loop in
# _stream_audio sleeps until a chunk's worth of time has passed, so the chunk
# size IS the delivery granularity: at 64 KiB and the default 384 kbps the
# daemon wrote once and then slept 1.37 s, handing Safari the whole stream in
# lumps on roughly the period a listener reports dropouts on. 8 KiB is one
# write per ~170 ms for about 6 KB/s of extra syscalls.
AUDIO_CHUNK = 8 * 1024
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
AUDIO_SLOTS = threading.Semaphore(PHONE_AUDIO_STREAMS)

# Playback telemetry from a phone with ?debug in its URL, carried on the status
# poll and written to the journal. It exists because the fault being chased --
# headphone audio that breaks up for a few milliseconds every second or two --
# only reproduces on a real iPhone over real Wi-Fi, where there is no console to
# read and nothing to attach a profiler to. The phone measures; this box keeps
# the record. See the header of Handler._log_sync for the field legend.
SYNC_HEADER = "X-Playstick-Sync"
# The value is a request header from an unauthenticated LAN client, so it is
# filtered rather than trusted: anything outside this set is dropped before the
# line is written. Notably absent are the two characters that would matter --
# newline, which would forge a journal entry, and '%', which would reach a
# format string. The length cap is a second, blunter version of the same idea.
SYNC_KEEP_RE = re.compile(r"[^A-Za-z0-9=;:.,+_-]")
# 512 rather than 400: the page sends about 240 characters, and the rest is
# headroom for the `tun` field, which lists any controller constant a listener
# has adjusted from the debug sheet. A capture taken mid-experiment is not
# interpretable without it.
SYNC_MAX = 512
# Lines per second, across every client. Six phones polling once a second is
# the design load and this is triple it, so the cap is invisible in use and
# still bounds what a client stuck in a retry loop can do to a device whose
# journal shares 32 GB of eMMC with everything else.
SYNC_MAX_RATE = 20

THUMB_DIR = os.environ.get("PLAYSTICK_THUMB_DIR", "/var/cache/playstick/thumbs")
THUMB_ARGS = shlex.split(os.environ.get("PLAYSTICK_THUMB_ARGS", ""))
THUMB_AT = os.environ.get("PLAYSTICK_THUMB_AT", "20%")
THUMB_TIMEOUT = int(os.environ.get("PLAYSTICK_THUMB_TIMEOUT", "60") or 60)

MPV = os.environ.get("PLAYSTICK_MPV", "/usr/bin/mpv")
MPV_ARGS = shlex.split(os.environ.get("PLAYSTICK_MPV_ARGS", ""))
MPV_SOCKET = os.environ.get("PLAYSTICK_MPV_SOCKET", "/run/playstick/mpv.sock")
# mpv's DRM backend opens a VT to arbitrate console switching. Handing it tty1
# on stdin is what the probe harness already had to do -- uxplay-probe-matrix.sh
# runs its sweep under `openvt -c 1` because DRM work started from an SSH
# session has no controlling terminal. Empty disables it.
MPV_TTY = os.environ.get("PLAYSTICK_MPV_TTY", "/dev/tty1")

BUSY_FILE = os.environ.get("PLAYSTICK_BUSY_FILE", "/run/playstick/playing")
# Written before the AirPlay unit is stopped and removed after it is started
# again. Its presence is how a restarted daemon -- or the unit's ExecStopPost --
# knows the receiver was taken down by us and owes a restore.
RESTORE_FILE = os.environ.get("PLAYSTICK_RESTORE_FILE", "/run/playstick/restore-airplay")
AIRPLAY_UNIT = os.environ.get("PLAYSTICK_AIRPLAY_UNIT", "uxplay-kms.service")
AIRPLAY_PORT = os.environ.get("PLAYSTICK_AIRPLAY_PORT", "")
SETTLE_SECONDS = float(os.environ.get("PLAYSTICK_SETTLE_SECONDS", "1.0") or 1.0)
# Whether mpv has a real audio output. False means --ao=null, and the page
# hides its volume controls rather than showing a child two buttons that do
# nothing. See player_audio in group_vars/all.yml for why this is off.
HAS_AUDIO = (os.environ.get("PLAYSTICK_AUDIO", "0") or "0").lower() in ("1", "true", "yes")


# --- the projector -------------------------------------------------------
# Which driver, and where. Empty model means there is no projector, which is
# the default and the only configuration the development GUI ever runs: the
# daemon then behaves exactly as it did before this feature existed. See
# projector/__init__.py for why every way of getting this wrong ends up at the
# same place rather than at an exception.
PROJECTOR_MODEL = os.environ.get("PLAYSTICK_PROJECTOR_MODEL", "")
# A /dev/serial/by-id path rather than /dev/ttyUSB0. The number depends on USB
# enumeration order, so a hub that comes up differently after a reboot silently
# moves it; the by-id link is derived from the adapter's own serial number and
# does not.
PROJECTOR_DEVICE = os.environ.get("PLAYSTICK_PROJECTOR_DEVICE", "")
# Which socket the stick is plugged into. Empty means "do not switch inputs",
# which is the right setting for a projector that auto-selects the live source
# and the safe one if the code below turns out to be wrong -- an input this
# model does not have answers ER401 and the step is skipped.
PROJECTOR_INPUT = (os.environ.get("PLAYSTICK_PROJECTOR_INPUT", "") or "").strip().upper()
# Seconds to wait for a whole reply. Generous: five ASCII characters at 9600
# baud take five milliseconds, so anything near this means the projector is not
# answering rather than answering slowly.
PROJECTOR_TIMEOUT = float(os.environ.get("PLAYSTICK_PROJECTOR_TIMEOUT", "1.5") or 1.5)
# The manual's post-PON blackout, during which the projector ignores every
# command including the one asking whether it is on yet. Spent sleeping rather
# than polling, because a timeout inside this window says nothing.
PROJECTOR_WARMUP_SECONDS = float(
    os.environ.get("PLAYSTICK_PROJECTOR_WARMUP_SECONDS", "10") or 10)
# ...and how long after that to keep asking before starting the film regardless.
# Long enough to cover a cold lamp and a post-POF cool-down retry; short enough
# that a projector which will never answer does not hold a child indefinitely.
PROJECTOR_READY_SECONDS = float(
    os.environ.get("PLAYSTICK_PROJECTOR_READY_SECONDS", "90") or 90)
# Minutes of no film and no mirroring before the lamp goes out. 0 disables it.
# Minutes in the environment because that is the unit the decision is made in;
# seconds here because that is the unit the arithmetic is done in.
PROJECTOR_IDLE_SECONDS = int(
    float(os.environ.get("PLAYSTICK_PROJECTOR_IDLE_MINUTES", "30") or 0) * 60)
# How often the keeper thread looks. Nothing it watches changes faster than
# this, and every tick costs one ss invocation.
PROJECTOR_TICK_SECONDS = float(
    os.environ.get("PLAYSTICK_PROJECTOR_TICK_SECONDS", "15") or 15)
# Whether a mirroring session may switch the projector on. The film path always
# may; this is only about AirPlay.
PROJECTOR_WAKE_ON_AIRPLAY = (
    os.environ.get("PLAYSTICK_PROJECTOR_WAKE_ON_AIRPLAY", "1") or "1"
).lower() in ("1", "true", "yes")
# ...and how many consecutive ticks a confirmed session must survive first.
# This is the safeguard on the whole idea: iOS opens connections to UxPlay's
# port merely from having the AirPlay picker on screen, so at the default tick
# two of them is about thirty seconds of sustained mirroring. A glance does not
# reach that; a session does. Raise it if the lamp ever strikes on its own.
PROJECTOR_AIRPLAY_WAKE_TICKS = max(1, int(
    os.environ.get("PLAYSTICK_PROJECTOR_AIRPLAY_WAKE_TICKS", "2") or 2))


def _parse_networks(spec):
    """Thin, and honest about it. ufw is purged by explicit decision, so this
    is the only filtering there is -- it keeps a misconfigured router from
    publishing the UI to the internet. It is not a defence against anybody
    already on the LAN, who can equally well mirror to the projector over
    AirPlay with no authentication at all."""
    nets = []
    for item in spec.split():
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # log() is not defined yet at import time.
            print("playstick: ignoring unparseable network %r" % item,
                  file=sys.stderr)
    return nets


ALLOW_NETWORKS = _parse_networks(os.environ.get("PLAYSTICK_ALLOW_NETWORKS", ""))

SKIP_RE = re.compile(SKIP_PATTERN, re.IGNORECASE) if SKIP_PATTERN else None

# Directories that are never part of a film library and are expensive to walk.
# @eaDir in particular is Synology's per-file metadata sidecar tree -- it holds
# a directory per media file, so walking it multiplies the scan cost by more
# than the library contains.
SKIP_DIRS = {"@eaDir", ".AppleDouble", "#recycle", "$RECYCLE.BIN",
             "lost+found", "System Volume Information"}

# Release tags and everything after them. Cutting at the first one turns
# "Ponyo.2008.1080p.BluRay.x264-GROUP.mkv" into "Ponyo 2008", and the year
# goes next.
TAG_RE = re.compile(
    r"\b(?:\d{3,4}p|4k|x26[45]|h\.?26[45]|hevc|xvid|divx|blu-?ray|b[rd]rip|"
    r"web-?dl|web-?rip|hd(?:tv|rip)|dvd-?rip|remux|aac\d*|ac3|dts(?:-hd)?|"
    r"ddp?5|truehd|atmos|proper|repack|internal|limited|extended|unrated|"
    r"multi|dual|imax)\b.*",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\s*[\(\[]?\b(19|20)\d{2}\b[\)\]]?\s*$")

SIDECAR_NAMES = ("poster.jpg", "poster.png", "cover.jpg", "cover.png",
                 "folder.jpg", "folder.png")


def log(msg, *args):
    print("playstick: " + (msg % args if args else msg), file=sys.stderr, flush=True)
