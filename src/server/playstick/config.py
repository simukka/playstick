"""Every tunable, read from one file exactly once at import.

That file is /etc/playstick/settings.toml, named by the single environment
variable this package reads, and rendered by Ansible from group_vars/all.yml --
which is where a value is actually changed and where each one is explained at
length. Nothing here does work; it only decides what the rest will do.

WHY A FILE RATHER THAN THE ENVIRONMENT, since it used to be forty-five
Environment= lines in the unit. Every knob had to be spelled out five times --
declared in a role's defaults, given a value in group_vars, threaded through
the unit template, parsed here, and defaulted again in the dev container and
the test harness -- and the copies drifted. PLAYSTICK_OVERLAY was the proof:
this file read it, the unit template never set it, and nothing said so, so the
overlay path silently fell back to the default here. One file cannot drift from
itself, and [library] overlay_file is now rendered like every other key.

The defaults below are still real and still matter. They are what you get
running src/server/playstick-web.py by hand on a developer machine with no
settings file at all, which is a path worth keeping working -- see PORT.

TOML because tomllib is in the standard library from 3.11 and PyYAML is not.
This package has no third-party dependencies and this was not going to be the
change that gave it one.
"""

import ipaddress
import os
import re
import sys
import threading
import time
import tomllib


SETTINGS_FILE = os.environ.get("PLAYSTICK_SETTINGS", "/etc/playstick/settings.toml")


def _load(path):
    """The settings, or an empty dict and one line in the journal.

    Never raises. A daemon that refuses to start because its configuration file
    has a stray bracket in it is a daemon that a bad Ansible run turns into a
    dark projector and a child who cannot watch anything -- whereas one that
    falls back to the defaults below at least serves the page and says what is
    wrong. This is the same judgement Library makes about a corrupt index.
    """
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        # Not an error: this is the developer-machine path, and saying so on
        # every hand-run would train people to ignore the line that matters.
        return {}
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        print("playstick: ignoring unreadable settings %s: %s" % (path, exc),
              file=sys.stderr, flush=True)
        return {}


SETTINGS = _load(SETTINGS_FILE)


def _get(section, key, default):
    """One value, or the default. Type coercion is the caller's job, done at
    the assignment below so that the expected type is visible next to the name
    rather than buried in a schema."""
    table = SETTINGS.get(section)
    if not isinstance(table, dict):
        return default
    value = table.get(key, default)
    return default if value is None else value


def _flag(section, key, default):
    """TOML has real booleans, so this is nearly a no-op -- but a hand-edited
    file may still say "yes" or 1, and refusing those would be pedantry aimed
    at the one person most likely to be debugging something else."""
    value = _get(section, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _words(section, key, default):
    """A list, tolerating the space-separated string the environment had to
    use. Every caller wants a sequence; only the file format changed."""
    value = _get(section, key, default)
    if isinstance(value, str):
        return value.split()
    return list(value)


BIND = _get("server", "bind", "0.0.0.0")
# 8080 rather than the 80 the appliance uses, deliberately: this default is
# only ever reached with no settings file, i.e. by a developer running the
# daemon by hand as themselves, where binding 80 would fail before printing
# anything useful.
PORT = int(_get("server", "port", 8080))
UI_FILE = _get("server", "ui_file", "/usr/local/share/playstick/ui.html")

LIBRARY = _get("library", "path", "/srv/movies")
EXTENSIONS = tuple(
    e if e.startswith(".") else "." + e
    for e in _words("library", "extensions", ".mkv .mp4 .m4v .avi .mov")
)
MIN_SIZE = int(_get("library", "min_size_mb", 100) or 0) * 1024 * 1024
SCAN_DEPTH = max(1, int(_get("library", "scan_depth", 2) or 2))
SCAN_INTERVAL = max(30, int(_get("library", "scan_interval", 300) or 300))
SKIP_PATTERN = _get("library", "skip_pattern", r"sample|trailer|extras?\b")

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
_UNSET = object()
_index_set = _get("library", "index_file", _UNSET)
INDEX_FILE = (os.path.join(LIBRARY, "playstick-library.json")
              if _index_set is _UNSET else _index_set)
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
_overlay_set = _get("library", "overlay_file", _UNSET)
OVERLAY_FILE = (os.path.join(LIBRARY, "playstick-overlay.json")
                if _overlay_set is _UNSET else _overlay_set)
# Subtitles the prep tool extracted. Off means they are not passed to mpv at
# all, which is not the same as mpv having none to choose.
SUBTITLES = _flag("library", "subtitles", True)

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
PHONE_AUDIO = _flag("phone_audio", "enabled", True)
# Bytes per second per listener, after an initial burst. Safari pulls a
# progressive media file as fast as the socket will go, and every byte of it
# crosses this box's single SDIO-attached Wi-Fi radio TWICE -- once reading the
# track from the NAS, once writing it to the phone -- while the film's own
# ~4 Mbps CIFS read is on the same radio. Unpaced, somebody choosing a language
# is indistinguishable from the film stuttering. 0 disables the pacing.
PHONE_AUDIO_BPS = int(_get("phone_audio", "kbps", 384) or 0) * 1000 // 8
# Seconds of audio to hand over at full speed before the pacing starts, so that
# playback begins immediately and has something to survive a hiccup with.
PHONE_AUDIO_BURST = float(_get("phone_audio", "burst_seconds", 30) or 0)
# Concurrent audio responses. Each one holds a thread for as long as somebody
# is listening, so a phone that reconnects on every seek must not be able to
# accumulate them until the server has no threads left for the page itself.
PHONE_AUDIO_STREAMS = max(1, int(_get("phone_audio", "max_streams", 6) or 6))

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

THUMB_DIR = _get("thumbs", "dir", "/var/cache/playstick/thumbs")
# A real list in the file, so a path with a space in it survives. The
# environment could only carry one string and this went through shlex.split.
THUMB_ARGS = list(_get("thumbs", "args", []))
THUMB_AT = _get("thumbs", "at", "20%")
THUMB_TIMEOUT = int(_get("thumbs", "timeout", 60) or 60)

MPV = _get("mpv", "binary", "/usr/bin/mpv")
# Likewise a list, and here it matters more: player_mpv_extra_args could not
# previously carry a quoted path at all.
MPV_ARGS = list(_get("mpv", "args", []))
MPV_SOCKET = _get("mpv", "socket", "/run/playstick/mpv.sock")
# mpv's DRM backend opens a VT to arbitrate console switching. Handing it tty1
# on stdin is what the probe harness already had to do -- uxplay-probe-matrix.sh
# runs its sweep under `openvt -c 1` because DRM work started from an SSH
# session has no controlling terminal. Empty disables it.
MPV_TTY = _get("mpv", "tty", "/dev/tty1")

BUSY_FILE = _get("display", "busy_file", "/run/playstick/playing")
# Written before the AirPlay unit is stopped and removed after it is started
# again. Its presence is how a restarted daemon -- or the unit's ExecStopPost --
# knows the receiver was taken down by us and owes a restore.
RESTORE_FILE = _get("display", "restore_file", "/run/playstick/restore-airplay")
AIRPLAY_UNIT = _get("display", "airplay_unit", "uxplay-kms.service")
# A string, because airplay_active() interpolates it into an ss filter and
# empty means "do not ask" -- see the guard at the top of that function.
AIRPLAY_PORT = str(_get("display", "airplay_port", "") or "")
SETTLE_SECONDS = float(_get("display", "settle_seconds", 1.0) or 1.0)
# Whether mpv has a real audio output. False means --ao=null, and the page
# hides its volume controls rather than showing a child two buttons that do
# nothing. See player_audio in group_vars/all.yml for why this is off.
HAS_AUDIO = _flag("display", "has_audio", False)


# --- the projector -------------------------------------------------------
# Which driver, and where. Empty model means there is no projector, which is
# the default and the only configuration the development GUI ever runs: the
# daemon then behaves exactly as it did before this feature existed. See
# projector/__init__.py for why every way of getting this wrong ends up at the
# same place rather than at an exception.
PROJECTOR_MODEL = _get("projector", "model", "")
# A /dev/serial/by-id path rather than /dev/ttyUSB0. The number depends on USB
# enumeration order, so a hub that comes up differently after a reboot silently
# moves it; the by-id link is derived from the adapter's own serial number and
# does not.
PROJECTOR_DEVICE = _get("projector", "device", "")
# Which socket the stick is plugged into. Empty means "do not switch inputs",
# which is the right setting for a projector that auto-selects the live source
# and the safe one if the code below turns out to be wrong -- an input this
# model does not have answers ER401 and the step is skipped.
PROJECTOR_INPUT = str(_get("projector", "input", "") or "").strip().upper()
# Seconds to wait for a whole reply. Generous: five ASCII characters at 9600
# baud take five milliseconds, so anything near this means the projector is not
# answering rather than answering slowly.
PROJECTOR_TIMEOUT = float(_get("projector", "timeout", 1.5) or 1.5)
# The manual's post-PON blackout, during which the projector ignores every
# command including the one asking whether it is on yet. Spent sleeping rather
# than polling, because a timeout inside this window says nothing.
PROJECTOR_WARMUP_SECONDS = float(_get("projector", "warmup_seconds", 10) or 10)
# ...and how long after that to keep asking before starting the film regardless.
# Long enough to cover a cold lamp and a post-POF cool-down retry; short enough
# that a projector which will never answer does not hold a child indefinitely.
PROJECTOR_READY_SECONDS = float(_get("projector", "ready_seconds", 90) or 90)
# Minutes of no film and no mirroring before the lamp goes out. 0 disables it.
# Minutes in the environment because that is the unit the decision is made in;
# seconds here because that is the unit the arithmetic is done in.
PROJECTOR_IDLE_SECONDS = int(float(_get("projector", "idle_minutes", 30) or 0) * 60)
# How often the keeper thread looks. Nothing it watches changes faster than
# this, and every tick costs one ss invocation.
PROJECTOR_TICK_SECONDS = float(_get("projector", "tick_seconds", 15) or 15)
# Whether a mirroring session may switch the projector on. The film path always
# may; this is only about AirPlay.
PROJECTOR_WAKE_ON_AIRPLAY = _flag("projector", "wake_on_airplay", True)
# ...and how many consecutive ticks a confirmed session must survive first.
# This is the safeguard on the whole idea: iOS opens connections to UxPlay's
# port merely from having the AirPlay picker on screen, so at the default tick
# two of them is about thirty seconds of sustained mirroring. A glance does not
# reach that; a session does. Raise it if the lamp ever strikes on its own.
PROJECTOR_AIRPLAY_WAKE_TICKS = max(1, int(
    _get("projector", "airplay_wake_ticks", 2) or 2))


# --- the NAS -------------------------------------------------------------
# The daemon knows the NAS exists for exactly one reason: so that a page which
# has to say "the films are unreachable" can offer to do something about it
# instead of only reporting it. Nothing here mounts anything -- that is the
# .automount's job and deliberately stays there.
#
# An empty host is the default and switches the whole watcher off: power reads
# "unknown", the page shows the sentence it always showed, and an install with
# no NAS -- or the development GUI -- behaves exactly as it did before this
# existed. That is the same shape as PROJECTOR_MODEL above and for the same
# reason.
NAS_HOST = _get("nas", "host", "")
# The wake-on-LAN target. Parsed at import by nas.py rather than here, because
# a bad value has to hide the button rather than raise in a request thread.
NAS_MAC = _get("nas", "mac", "")
# The .mount unit to reset-failed once the NAS answers again. Empty means
# derive it from LIBRARY with systemd-escape -- ask systemd rather than guess,
# which is the same call the nas role makes when it installs the unit.
NAS_MOUNT_UNIT = _get("nas", "mount_unit", "")

# 445, and the port is the point rather than an implementation detail. A
# Synology answers ICMP well before smbd is listening, so a ping-green light
# would promise films that are not there yet -- and a button whose result the
# page then contradicts is worse than no button. This is the port the mount
# itself will use, so an answer here means the next scan can succeed.
NAS_PROBE_PORT = int(_get("nas", "probe_port", 445) or 445)
# A TCP handshake on a LAN takes under a millisecond. Anything near this means
# the NAS is not answering rather than answering slowly.
NAS_PROBE_TIMEOUT = float(_get("nas", "probe_timeout", 1.0) or 1.0)
# Three cadences, because the cost of looking is the same but the value of a
# fresh answer is not. Awake is the boring case. Unreachable is worth watching
# a little harder, since somebody may be about to open the page. The minute
# after a wake is the only moment anyone is actually looking at the screen
# waiting for it to change, so it gets the dearest one.
NAS_PROBE_INTERVAL = max(1.0, float(_get("nas", "probe_interval", 60) or 60))
NAS_PROBE_INTERVAL_DOWN = max(1.0, float(_get("nas", "probe_interval_down", 10) or 10))
NAS_PROBE_INTERVAL_WAKING = max(1.0, float(_get("nas", "probe_interval_waking", 5) or 5))

NAS_WAKE_BROADCAST = _get("nas", "wake_broadcast", "255.255.255.255")
NAS_WAKE_PORT = int(_get("nas", "wake_port", 9) or 9)
# Least seconds between packets, enforced here rather than in the page. Six
# phones in a room, each with a button, should still send one packet -- and a
# child who taps twice because nothing visibly happened must not be the reason
# the NAS gets a second one.
NAS_WAKE_COOLDOWN = float(_get("nas", "wake_cooldown", 60) or 60)
# How long after a packet the daemon keeps answering "waking", i.e. how long
# the page says "this takes a minute" rather than offering the button again.
# A sleeping Synology reaches smbd in about twenty seconds; one that was fully
# off takes most of two minutes.
NAS_WAKE_WINDOW = float(_get("nas", "wake_window", 180) or 180)


def _parse_networks(items):
    """Thin, and honest about it. ufw is purged by explicit decision, so this
    is the only filtering there is -- it keeps a misconfigured router from
    publishing the UI to the internet. It is not a defence against anybody
    already on the LAN, who can equally well mirror to the projector over
    AirPlay with no authentication at all."""
    nets = []
    for item in items:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            # log() is not defined yet at import time.
            print("playstick: ignoring unparseable network %r" % item,
                  file=sys.stderr)
    return nets


ALLOW_NETWORKS = _parse_networks(_words("server", "allow_networks", []))

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
