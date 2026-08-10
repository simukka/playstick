#!/usr/bin/env python3
"""Prepare a movie library for the playstick appliance. Runs on the DEVELOPER
machine, never on the stick.

    ./scripts/playstick-prep.py --library /mnt/nas/video

WHY THIS EXISTS

The stick is an Atom x5-Z8350 with 2 GB of RAM decoding H.264 in software at
roughly 29 fps for 720p -- see uxplay_decoder in group_vars/all.yml for the
measurements. It has exactly enough headroom to play a film that is already in
the shape it wants, and none at all to spare for anything else. Every question
that can be answered in advance therefore should be: what the film is called,
what it looks like as a poster, whether it even decodes, and whether it is
already H.264 at 720p or needs an encode that would take the stick longer than
the film runs.

So this script does the expensive half on a machine that has cores, and leaves
behind a JSON index the daemon reads instead of walking the share. That walk is
the other reason: playstick-web.py stats every file over CIFS on SDIO-attached
Wi-Fi, and a poster grid that has to wait for it is a grid a child gives up on.

WHAT IT LEAVES BEHIND

    <library>/playstick-library.json     the index the daemon reads
    <library>/.playstick/media/<id>.mp4  the transcode, when one was needed.
                                         Named for the id -- a sha1 of the
                                         source's path -- and never for the
                                         title, which is re-derived every run
                                         and so would rename the file out from
                                         under the "already encoded?" check
    <library>/.playstick/posters/        one JPEG per film
    <library>/.playstick/subs/           extracted subtitles, as UTF-8 SRT
    <library>/.playstick/audio/<id>/     one AAC-LC m4a per language, so that
                                         several people can watch one silent
                                         projector and each hear their own
                                         language in their own headphones
    <library>/.playstick/prep-state.json cache, so a re-run is cheap

The directory is dotted deliberately: playstick-web.py's fallback walk skips
dot-directories, so a daemon that ignores the index -- or one running against a
half-prepared library -- sees the original films and nothing else, rather than
each film twice.

FILMS, NOT TELEVISION

Episodic files are recognised and skipped -- S01E02, 1x02, a "Season 2" folder
anywhere in the path. --min-duration does not catch them, because an episode is
45 minutes long, and left in they turn the child's poster grid into a wall of
near-identical tiles and confuse de-duplication into treating a season as
twenty-four copies of one film. --allow-episodes keeps them.

WATCHING IT

Every long ffmpeg call -- the encode, the phone audio, a --verify full decode --
reports where it has got to, how many times faster than real time it is going,
and how much of the film is left. On a terminal that is one line, redrawn. In a
log it is a line a minute. --progress never turns it off.

STOPPING IT

Ctrl-C at any point. The running ffmpeg is ended, half-written files are
removed, the probe cache is written anyway, and the run says how far it got.
Running the same command again carries on from there. A second Ctrl-C stops
immediately without any of that.

The index is rewritten after each film rather than once at the end, so a run
that is stopped -- or that never finishes -- still leaves a library the daemon
can serve, with everything prepared so far in it. Films it had not reached yet
are listed against their original file, with no poster and no phone audio, which
is what an unprepared film looks like anyway.

WHAT IT NEVER DOES

Modify or delete anything in the library. Sources are read; everything written
goes under --output (which defaults to the library, but only ever into
.playstick/ and the index file). Duplicates are dropped from the index and
reported, not removed -- deciding which copy of a film to delete is not a
decision a script gets to make. --duplicates-dir moves them if you want that,
and it moves rather than deletes.

Nothing contacts the network unless --tmdb-key is given, and that flag sends
film titles to themoviedb.org. Ratings and genres otherwise come from the
library itself: .nfo sidecars first, then container tags.

REQUIRES  ffmpeg and ffprobe on PATH. Nothing else -- no pip install.
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "1.2"
SCHEMA = 1

# Bumped whenever the rules that turn a file into a title, a year or a TMDb
# match change. The state cache stores the whole derived movie dict, so without
# this a corrected rule would never reach a library that is already prepared --
# every film would sail past the fingerprint check carrying the old answer. A
# mismatch costs one ffprobe per film and nothing else: the encodes, the phone
# audio and the subtitles are guarded by their own output files.
META_VERSION = 2

INDEX_NAME = "playstick-library.json"
WORK_DIR = ".playstick"
STATE_NAME = "prep-state.json"

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

# Matches playstick-web.py's EXTENSIONS default plus the containers worth
# transcoding out of. .iso and .vob are deliberately absent: a DVD structure is
# not one film in one file and pretending otherwise produces nonsense titles.
VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".m2ts", ".ts", ".webm",
             ".wmv", ".flv", ".mpg", ".mpeg", ".divx", ".ogm")

SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")

# Straight from playstick-web.py, and deliberately a copy rather than a shared
# module: the two programs must agree on what a film is called, and the daemon
# has no dependencies by design. If you change one, change both -- the test at
# the bottom of this file checks a handful of names against the expectations
# these regexes encode.
TAG_RE = re.compile(
    r"\b(?:\d{3,4}p|4k|x26[45]|h\.?26[45]|hevc|xvid|divx|blu-?ray|b[rd]rip|"
    r"web-?dl|web-?rip|hd(?:tv|rip)|dvd-?rip|remux|aac\d*|ac3|dts(?:-hd)?|"
    r"ddp?5|truehd|atmos|proper|repack|internal|limited|extended|unrated|"
    r"multi|dual|imax)\b.*",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"\s*[\(\[]?\b(19|20)\d{2}\b[\)\]]?\s*$")
YEAR_ANYWHERE_RE = re.compile(r"[\(\[\.\s_-]((?:19|20)\d{2})[\)\]\.\s_-]")

SKIP_DIRS = {"@eaDir", ".AppleDouble", "#recycle", "$RECYCLE.BIN",
             "lost+found", "System Volume Information", "extrafanart",
             "extrathumbs", "behind the scenes", "featurettes", "trailers"}

# Episodic television, which this is not a library for.
#
# --min-duration does not catch it: a drama episode runs 45 minutes and sails
# past a 20-minute floor. Left in, a single season becomes 24 near-identical
# tiles a child has to scroll through to reach the next film, and every one of
# them is a poster extraction and -- with phone audio -- an AAC encode per
# language. Worse, de-duplication sees 24 entries whose titles normalise to the
# same string and starts dropping them as copies of each other.
#
# Matched against the path relative to the library, not the filename alone:
# "Detectorists/Series 2/03.mkv" is episodic and its filename says nothing.
#
# What is deliberately NOT here is a bare "Episode <n>", because "Star Wars
# Episode 1 The Phantom Menace" is a film and a tool that hides it is a tool
# nobody trusts again. Real rips carry SxxExx or sit in a season folder; the
# ambiguous case is left to --allow-episodes.
EPISODE_RE = re.compile(
    r"(?:"
    # S01E02, s1e2, S01.E02 -- and S01E02E03, a double episode in one file,
    # which is why the last group repeats.
    r"(?:^|[^a-z0-9])s\d{1,2}[ ._-]?e\d{1,3}(?:[ ._-]?e\d{1,3})*"
    r"|(?:^|[^a-z0-9])(?:season|series|staffel)[ ._-]?\d{1,2}"
    r"|(?:^|[^a-z0-9])part[ ._-]?\d{1,2}[ ._-]?of[ ._-]?\d{1,2}"
    # 1x02, and the weakest signal here, so it is the fussiest. It must be
    # preceded by a separator WITHIN a name -- not the start of one, and not a
    # directory boundary -- because an episode marker never opens the name:
    # the show's title comes first, as in "friends.1x02". Without that rule
    # "12x12 A Cabin Story (2020).mkv" is a film this quietly hides, which is
    # the failure that makes a tool untrustworthy. The same leading separator
    # is what keeps it off "1920x1080" in a filename.
    r"|[ ._-]\d{1,2}x\d{2}"
    r")(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)

# Directories worth pruning rather than walking: catching them here means not
# stat'ing 24 files over CIFS to reject them one at a time.
EPISODE_DIR_RE = re.compile(
    r"^(?:season|series|staffel|s)[ ._-]?\d{1,2}$|^specials?$", re.IGNORECASE)

SIDECAR_POSTERS = ("poster.jpg", "poster.png", "cover.jpg", "cover.png",
                   "folder.jpg", "folder.png", "movie.jpg")

# Subtitle codecs that are text and therefore convertible to SRT without OCR.
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt",
                   "text", "microdvd"}

# Audio for the phones. AAC-LC in mp4 is the one thing every iPhone ever made
# can decode, and progressive mp4 is what Safari will seek around in.
#
# Not HE-AAC, which would halve the bitrate and which iOS plays perfectly well:
# ffmpeg's native AAC encoder implements no SBR at all, so HE would need
# libfdk_aac -- which Debian and Ubuntu cannot ship for licensing reasons, and
# which would therefore break this tool's promise of "ffmpeg and ffprobe on
# PATH. Nothing else".
#
# Not Opus either. It is the better codec and iOS support for it outside WebM
# is not something to bet a family's film night on.
PHONE_AUDIO_CODEC = "aac"

# Which downmix to use when a track has more channels than headphones do.
# ffmpeg's default 5.1 matrix is notoriously shy with the centre channel, and
# the centre channel is where the dialogue is. On speakers you get away with
# it; on headphones, with no room to help, you get a film you cannot follow.
# This lifts the centre and keeps the rest present.
PHONE_AUDIO_PAN = ("pan=stereo|FL=0.7*FC+0.5*FL+0.4*BL+0.3*LFE"
                   "|FR=0.7*FC+0.5*FR+0.4*BR+0.3*LFE")

LEVELS = {"quiet": 0, "normal": 1, "verbose": 2}
_verbosity = 1


def log(msg, *args, level="normal", stream=sys.stderr):
    if LEVELS.get(level, 1) <= _verbosity:
        print(msg % args if args else msg, file=stream, flush=True)


def warn(msg, *args):
    log("  ! " + (msg % args if args else msg), level="quiet")


def die(msg, *args):
    print("playstick-prep: " + (msg % args if args else msg), file=sys.stderr)
    raise SystemExit(1)


# --- progress --------------------------------------------------------------
#
# A six-hour encode that prints nothing is indistinguishable from a six-hour
# encode that has hung, and the only way anybody has ever told those apart is by
# giving up on both. So the long ffmpeg calls -- the transcode, the phone audio,
# a --verify full decode -- are asked for -progress pipe:1, and what comes back
# is redrawn as one line saying how far into the film it is, how many times
# faster than real time that is, and therefore how long is left.
#
# On stdout, deliberately. ffmpeg's own human-readable stats go to stderr, where
# they would interleave with this script's log lines and where their carriage
# returns would fight with these; -nostats turns them off and -progress gives us
# the same numbers as key=value blocks, twice a second, on a stream nothing else
# is using.
#
# On a terminal the line is redrawn in place. Redirected to a log it would
# become several thousand near-identical lines, so there it prints once a minute
# with a newline instead -- which is the form you want when you come back to a
# nohup'd run in the morning and need to know whether it moved.

PROGRESS_TTY_INTERVAL = 0.5
PROGRESS_LOG_INTERVAL = 60.0

_progress_mode = "auto"


def progress_wanted():
    """--quiet means quiet, and that includes this."""
    return _progress_mode != "never" and _verbosity >= 1


def redraw_in_place():
    """Whether to redraw one line or print a series of them. --progress always
    forces the redraw for the case of something that renders a carriage return
    without being a terminal -- a CI log viewer, mostly."""
    return _progress_mode == "always" or sys.stderr.isatty()


class Progress:
    """One line of "where has it got to", fed ffmpeg's -progress blocks."""

    def __init__(self, label, total):
        self.label = label
        self.total = total or 0
        self.tty = redraw_in_place()
        self.interval = PROGRESS_TTY_INTERVAL if self.tty else PROGRESS_LOG_INTERVAL
        self.last = 0.0
        self.width = 0
        self.drawn = False

    def feed(self, fields):
        """One complete -progress block, as a dict of its key=value lines."""
        # progress=end arrives after the last frame; there is nothing useful to
        # draw at that point and close() is about to wipe the line anyway.
        if fields.get("progress") == "end":
            return
        now = time.monotonic()
        if now - self.last < self.interval:
            return
        self.last = now
        self.draw(self.line(position(fields), to_float(fields.get("fps")),
                            speed(fields)))

    def line(self, done, fps, rate):
        bits = []
        if self.total:
            bits.append("%3d%%" % min(100, int(done * 100 / self.total)))
            bits.append("%s/%s" % (hms(done), hms(self.total)))
        else:
            bits.append(hms(done))
        if fps:
            bits.append("%.0f fps" % fps)
        if rate:
            bits.append("%.3gx" % rate)
            if self.total:
                left = (self.total - done) / rate
                if left > 0:
                    bits.append("eta %s" % hms(left))
        return "    %s  %s" % (self.label, "  ".join(bits))

    def draw(self, text):
        if not self.tty:
            print(text, file=sys.stderr, flush=True)
            return
        # Padded to the longest line drawn so far: the percentage and the eta
        # both shrink as they go, and without this the tail of the previous,
        # longer line stays on screen and reads as part of this one.
        pad = max(0, self.width - len(text))
        self.width = len(text)
        self.drawn = True
        sys.stderr.write("\r" + text + " " * pad)
        sys.stderr.flush()

    def close(self):
        """Take the line back down, so whatever logs next starts on a clean
        one -- including a warning about the very call this was watching."""
        if self.drawn:
            sys.stderr.write("\r" + " " * self.width + "\r")
            sys.stderr.flush()
            self.drawn = False


def position(fields):
    """How far into the output ffmpeg has got, in seconds.

    out_time_us is microseconds. out_time_ms is also microseconds -- it was
    named before anybody noticed and cannot be changed now -- so it is only a
    fallback for an ffmpeg old enough not to write the first, and out_time
    ("01:02:03.400000") is the fallback for both."""
    for key, scale in (("out_time_us", 1e6), ("out_time_ms", 1e6)):
        raw = fields.get(key)
        if raw and raw != "N/A":
            value = to_float(raw, -1.0)
            if value >= 0:
                return value / scale
    raw = fields.get("out_time") or ""
    seconds = 0.0
    try:
        for part in raw.split(":"):
            seconds = seconds * 60 + float(part)
    except ValueError:
        return 0.0
    return seconds


def speed(fields):
    """"1.02x" -> 1.02. It is "N/A" for the first block or two."""
    return to_float((fields.get("speed") or "").rstrip("x"), 0.0)


def with_progress(argv, label, total):
    """An ffmpeg argv with its progress stream switched on, and the Progress
    that reads it -- or the argv untouched and None, when nobody is watching.

    The flags go in immediately after the binary because they are global
    options: after the first -i they would be read as input options and ignored.
    """
    if not progress_wanted():
        return argv, None
    return argv[:1] + ["-progress", "pipe:1", "-nostats"] + argv[1:], Progress(label, total)


# --- stopping --------------------------------------------------------------
#
# A full run is hours of ffmpeg, and the thing you want at hour two is to be
# able to stop it -- because you picked the wrong library, or you need the
# machine back, or you have seen enough of the log to know a flag is wrong.
#
# The default KeyboardInterrupt is a poor way to get that. It only lands
# between bytecodes, so it arrives at an arbitrary point in whatever step is
# running; it leaves .part files behind; it abandons the state cache, so the
# next run re-probes every film it had already probed; and it says nothing
# about what was finished and what was not.
#
# So SIGINT (and SIGTERM, for `docker stop`) sets a flag instead. The running
# ffmpeg is ended immediately, every loop checks the flag between items, the
# staging files are cleaned up on the way out, and the state cache is still
# written -- a cancelled run costs nothing on the next one. A second Ctrl-C
# restores the default handler and dies on the spot, for the case where
# something in here is itself stuck.

_stop = threading.Event()
_child = None            # the ffmpeg/ffprobe subprocess, so it can be ended


def cancelled():
    return _stop.is_set()


def _end_child(proc):
    """SIGTERM, then SIGKILL for the one that ignores it. Waiting matters: the
    caller is about to unlink a .part file the child still has open, and on a
    network mount deleting a file out from under a writer is how you get a
    half-written one left behind under a temporary name."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
    except OSError:
        pass


def _on_signal(signum, frame):
    if _stop.is_set():
        # Second one. Hand the signal back to the default handler and let it
        # do what it always does.
        signal.signal(signum, signal.SIG_DFL)
        print("\nstopping now", file=sys.stderr)
        os.kill(os.getpid(), signum)
        return
    _stop.set()
    print("\nstopping -- ending the current step, then writing what is "
          "finished (again to stop now)", file=sys.stderr)
    # Only a nudge. The reaping -- waiting, escalating to SIGKILL, unlinking
    # the .part file -- happens back in run(), which notices within 250 ms.
    # Doing it here would mean calling Popen.wait() from a signal handler while
    # the main thread it interrupted may be holding Popen's own waitpid lock,
    # and that spins for the full timeout rather than returning.
    proc = _child
    if proc is not None:
        try:
            proc.terminate()
        except OSError:
            pass


def install_signal_handlers():
    for signum in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if signum is not None:
            try:
                signal.signal(signum, _on_signal)
            except (ValueError, OSError):
                pass        # not the main thread, or no such signal here


def discard(path):
    """Remove a staging file, saying nothing if it was never created."""
    try:
        os.unlink(path)
    except OSError:
        pass


# --- shelling out ----------------------------------------------------------

def _wait_watching(proc, argv, progress, deadline, timeout):
    """The waiting half of run(), for a call whose progress is being shown.

    Both pipes are drained by threads here rather than by communicate(): stdout
    has to be read line by line as it arrives for the progress line to move at
    all, and the moment you read one pipe yourself you have taken on the other,
    because an encode that fills the stderr buffer nobody is reading stops dead
    and never exits. The main thread keeps polling at the same 250 ms as the
    plain path, so a stop request and a timeout still land exactly as promptly.
    """
    tail = []
    fields = {}

    def read_stderr():
        for line in proc.stderr:
            tail.append(line)
            if len(tail) > 40:
                del tail[:-40]      # only the last of it is ever reported

    def read_progress():
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            fields[key] = value
            # Every block ends with a progress= line, and drawing on that means
            # drawing from a complete set of fields rather than half of one.
            if key == "progress":
                progress.feed(fields)

    threads = [threading.Thread(target=read_stderr, daemon=True),
               threading.Thread(target=read_progress, daemon=True)]
    for thread in threads:
        thread.start()

    while True:
        try:
            code = proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            if cancelled():
                progress.close()
                _end_child(proc)
                return subprocess.CompletedProcess(argv, 130, "", "cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                progress.close()
                _end_child(proc)
                return subprocess.CompletedProcess(
                    argv, 124, "", "timed out after %ss" % timeout)

    # The child is gone, so both pipes are at EOF and these are about to end;
    # joining them is what guarantees the last of stderr is in tail before it
    # gets reported as the reason something failed.
    for thread in threads:
        thread.join(timeout=5)
    progress.close()
    if cancelled():
        return subprocess.CompletedProcess(argv, 130, "", "cancelled")
    return subprocess.CompletedProcess(argv, code, "", "".join(tail))


def run(argv, timeout=None, progress=None):
    """Never inherits stdin: ffmpeg reads it and a stray keystroke in a long
    batch would otherwise reach the encoder.

    Popen and a polling wait rather than subprocess.run(), so that a stop
    request ends a six-hour encode at the moment it is made instead of whenever
    ffmpeg next happens to finish. Interactively the child is in this process
    group and gets the terminal's SIGINT anyway; the explicit terminate() is
    for every other case -- a script, a CI job, systemd, `kill -INT` from
    another window -- where the signal reaches only this process.

    Returns 130 for a cancelled call, so callers can tell "you stopped it"
    from "it failed" and report accordingly.

    With a Progress, stdout is ffmpeg's -progress stream and is consumed as it
    arrives instead of being collected; the result's stdout is empty. Only the
    calls with nothing to say on stdout are ever run that way.
    """
    global _child
    if cancelled():
        return subprocess.CompletedProcess(argv, 130, "", "cancelled")
    deadline = time.monotonic() + timeout if timeout else None
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))

    _child = proc
    try:
        if progress is not None:
            return _wait_watching(proc, argv, progress, deadline, timeout)
        while True:
            wait = 0.25
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    _end_child(proc)
                    return subprocess.CompletedProcess(
                        argv, 124, "", "timed out after %ss" % timeout)
                wait = min(wait, left)
            try:
                # Re-calling communicate() after a timeout is the documented
                # way to do this and keeps whatever it has already read, so the
                # pipes cannot fill and deadlock a chatty --verify full decode.
                out, err = proc.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                if cancelled():
                    _end_child(proc)
                    return subprocess.CompletedProcess(argv, 130, "", "cancelled")
    finally:
        _child = None

    if cancelled():
        # It exited on its own SIGINT from the terminal. Same situation.
        return subprocess.CompletedProcess(argv, 130, out, "cancelled")
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def ffprobe_json(path, timeout=120):
    res = run([FFPROBE, "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", path], timeout=timeout)
    if res.returncode != 0:
        return None, (res.stderr or "ffprobe failed").strip().splitlines()[-1:][0] if res.stderr else "ffprobe failed"
    try:
        return json.loads(res.stdout), ""
    except ValueError as exc:
        return None, "unreadable ffprobe output: %s" % exc


# --- probing ---------------------------------------------------------------

def video_stream(info):
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        # Cover art is a video stream with one frame in it. It is a poster, not
        # a film, and treating it as one is how a music-video rip ends up
        # reported as 300x300.
        if stream.get("disposition", {}).get("attached_pic"):
            continue
        return stream
    return None


def audio_streams(info):
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def subtitle_streams(info):
    return [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]


def attached_pic_index(info):
    for stream in info.get("streams", []):
        if (stream.get("codec_type") == "video"
                and stream.get("disposition", {}).get("attached_pic")):
            return stream.get("index")
    return None


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def duration_of(info):
    """Containers disagree about where the duration lives. mkv often carries it
    only as a tag on the stream, and some remuxes carry it nowhere at all."""
    dur = to_float(info.get("format", {}).get("duration"))
    if dur > 0:
        return dur
    vid = video_stream(info) or {}
    dur = to_float(vid.get("duration"))
    if dur > 0:
        return dur
    for key, value in (vid.get("tags") or {}).items():
        if key.lower() == "duration":
            # "01:52:31.008000000"
            parts = str(value).split(":")
            try:
                secs = 0.0
                for part in parts:
                    secs = secs * 60 + float(part)
                return secs
            except ValueError:
                continue
    return 0.0


def frame_rate(stream):
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = stream.get(key) or ""
        if "/" in raw:
            num, _, den = raw.partition("/")
            num, den = to_float(num), to_float(den)
            if den:
                return num / den
    return 0.0


def bit_rate(info, vid):
    rate = to_float(info.get("format", {}).get("bit_rate"))
    if rate > 0:
        return rate
    rate = to_float(vid.get("bit_rate"))
    if rate > 0:
        return rate
    size = to_float(info.get("format", {}).get("size"))
    dur = duration_of(info)
    return (size * 8 / dur) if size and dur else 0.0


def is_8bit_420(stream):
    return (stream.get("pix_fmt") or "yuv420p") in ("yuv420p", "yuvj420p", "nv12")


# --- verification ----------------------------------------------------------

def decode_window(path, start, length, timeout, label=None):
    """Decode a slice and report what ffmpeg complained about.

    -ss before -i so the seek is a container seek rather than a decode of
    everything up to that point; on a 20 GB remux the difference is minutes.
    Video only: a broken audio track is not a reason to reject a film, and
    mpv would play it anyway.

    A label asks for a progress line, which only the whole-film decode of
    --verify full is long enough to want. It also moves the output off stdout,
    because the null muxer's "-" and -progress's pipe:1 are the same stream.
    """
    argv = [FFMPEG, "-nostdin", "-v", "error", "-hide_banner"]
    if start > 0:
        argv += ["-ss", "%.3f" % start]
    argv += ["-i", path, "-t", "%.3f" % length, "-an", "-sn", "-dn", "-f", "null"]
    watching = None
    if label:
        argv, watching = with_progress(argv, label, length)
    argv += [os.devnull if watching is not None else "-"]
    res = run(argv, timeout=timeout, progress=watching)
    err = (res.stderr or "").strip()
    if res.returncode == 124:
        return "decode timed out"
    if res.returncode != 0 and not err:
        return "ffmpeg exited %d" % res.returncode
    return err


def last_video_pts(path, duration, timeout=90):
    """When the last frame actually is, as opposed to when the header claims.

    A download that stopped at 80% keeps the container's declared duration --
    the header was written first, or was copied from the source -- so the only
    honest way to ask whether the film is all there is to look at the packets
    near the end. -read_intervals seeks straight there.
    """
    start = max(0.0, duration - 60.0)
    res = run([FFPROBE, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "packet=pts_time", "-print_format", "json",
               "-read_intervals", "%.3f%%+60" % start, path], timeout=timeout)
    if res.returncode != 0:
        return None
    try:
        packets = json.loads(res.stdout).get("packets", [])
    except ValueError:
        return None
    times = [to_float(p.get("pts_time"), -1.0) for p in packets]
    times = [t for t in times if t >= 0]
    return max(times) if times else None


def verify_file(path, info, args):
    """Returns (ok, level, reasons). level is what was actually checked, so the
    index can say how much confidence the answer carries."""
    reasons = []
    if args.verify == "none":
        return True, "none", reasons

    vid = video_stream(info)
    if vid is None:
        return False, "probe", ["no video stream"]

    dur = duration_of(info)
    if dur <= 0:
        return False, "probe", ["no duration -- container header is incomplete"]
    if dur < args.min_duration:
        return False, "probe", ["only %s long, below --min-duration %s"
                                % (hms(dur), hms(args.min_duration))]

    # Truncation: what the header promises against what is there. The tolerance
    # is generous because a container's duration is frequently a rounded
    # estimate, and because the last packets of a legitimately-muxed file can
    # sit a second or two short of it.
    tail = last_video_pts(path, dur)
    if tail is not None:
        missing = dur - tail
        # Absolute, not proportional: 2% of a two-hour film is two and a half
        # minutes, which is an entire ending. The floor is there because a
        # container's duration is often a rounded estimate and the last packets
        # of a legitimately-muxed file can sit a second or two short of it.
        tolerance = max(5.0, min(20.0, dur * 0.005))
        if missing > tolerance:
            reasons.append("truncated: %s of video missing from the end "
                           "(header says %s, last frame is at %s)"
                           % (hms(missing), hms(dur), hms(tail)))

    if args.verify in ("quick", "full"):
        head = decode_window(path, 0, min(8.0, dur), args.decode_timeout)
        if head:
            reasons.append("decode errors at the start: " + first_line(head))
        # The end is where a partial file fails and a full decode is the only
        # other way to find out.
        tail_start = max(0.0, dur - 20.0)
        errs = decode_window(path, tail_start, 20.0, args.decode_timeout)
        if errs:
            reasons.append("decode errors at the end: " + first_line(errs))

    if args.verify == "full":
        errs = decode_window(path, 0, dur + 1, args.full_decode_timeout,
                             label="verifying")
        if errs:
            reasons.append("decode errors: " + first_line(errs))

    if reasons and args.allow_truncated and all("truncat" in r for r in reasons):
        return True, args.verify, reasons
    return (not reasons), args.verify, reasons


def first_line(text):
    line = text.strip().splitlines()[0] if text.strip() else text
    return line[:180]


def hms(seconds):
    seconds = int(seconds or 0)
    if seconds >= 3600:
        return "%dh%02dm" % (seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "%dm%02ds" % (seconds // 60, seconds % 60)
    return "%ds" % seconds


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return "%.1f %s" % (num, unit)
        num /= 1024.0
    return "%.1f TB" % num


# --- titles ----------------------------------------------------------------

def clean_title(filename):
    """Kept byte-for-byte in step with playstick-web.py's clean_title()."""
    stem = os.path.splitext(filename)[0]
    stem = re.sub(r"[._]+", " ", stem)
    stem = TAG_RE.sub("", stem)
    stem = re.sub(r"[-\s]+$", "", stem)
    stem = YEAR_RE.sub("", stem)
    stem = re.sub(r"\s{2,}", " ", stem).strip(" -[](){}")
    if not stem:
        stem = os.path.splitext(filename)[0]
    if stem.islower():
        stem = stem.title()
    return stem


def plausible_year(year):
    """Between the first film and a little after now. The upper bound is what
    keeps "Blade Runner 2049" and "2012" from being read as release dates."""
    try:
        year = int(year)
    except (TypeError, ValueError):
        return False
    return 1888 <= year <= time.localtime().tm_year + 2


def guess_year(filename):
    stem = os.path.splitext(os.path.basename(filename))[0]
    matches = YEAR_ANYWHERE_RE.findall(" " + stem.replace("_", " ") + " ")
    if not matches:
        return None
    # The last plausible year wins: "2001 A Space Odyssey 1968" is the case
    # this exists for, and the release year is conventionally at the end.
    for candidate in reversed(matches):
        if plausible_year(candidate):
            return int(candidate)
    return None


def year_from_path(rel):
    """The year, from the filename if it has one and the folder if it does not.

    "Die.Hard.1988.1080p.bdrip.x265-FINKLEROY/die_hard.mkv" is the case: every
    fact about that film is in the directory name and none of it is in the
    file's. Without this it goes to TMDb with no year at all, and the search is
    left to popularity -- which is how it came back as "Don't Die Too Hard!".

    The filename still wins where it has one, because "Trilogy (2003)/The
    Matrix.1999.mkv" is the other shape a folder takes.
    """
    year = guess_year(os.path.basename(rel))
    if year:
        return year
    parent = os.path.basename(os.path.dirname(rel))
    if parent and not EPISODE_DIR_RE.match(parent):
        return guess_year(parent)
    return None


def normalize_title(title):
    """For comparison only. Strips accents, punctuation, a leading article and
    the difference between "and" and "&", so that "The Fifth Element" and
    "fifth.element,.the" land in the same bucket."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    text = re.sub(r"^(the|a|an)\s+", "", text)
    text = re.sub(r"\s+(the|a|an)$", "", text)   # "fifth element, the"
    return re.sub(r"\s+", " ", text).strip()


# Editions. Deliberately NOT in TAG_RE: that regex truncates everything from
# the tag onwards and is shared byte-for-byte with the daemon, so teaching it
# "director's cut" would rename the film on the shelf as well. The shelf should
# keep saying "Alien Director's Cut" -- it is what the file is. Only the search
# wants the canonical title, because TMDb has one entry for Alien and none for
# that edition of it, and asking for the edition is how you end up with Aliens.
EDITION_RE = re.compile(
    r"\b(?:remastered|restored|theatrical|extended|unrated|uncut|redux|"
    r"open[ ._-]?matte|(?:\d+th[ ._-]?)?anniversary|imax|"
    r"(?:director|collector|ultimate|special|final|criterion)(?:'?s)?)"
    r"(?:[ ._-]?(?:cut|edition|version))?\b",
    re.IGNORECASE,
)

# Source and stream words TAG_RE does not reach, because it only fires on a
# word boundary and these arrive glued to something else ("UHDRip") or as the
# language list a multi-audio remux carries around.
SEARCH_NOISE_RE = re.compile(
    r"\b(?:u?hd-?rip|uhd|hdr\d*|sdr|dv|10-?bit|8-?bit|av1|bdremux|"
    r"multi-?subs?|scan|35mm|dual[ ._-]?audio)\b",
    re.IGNORECASE,
)

# A release group's signature at the end of a string: "Contact - YIFY". Two or
# more capitals so a trailing initial is safe, and at least one letter so
# "THX 1138" is not mistaken for one.
GROUP_SUFFIX_RE = re.compile(r"[-\s]\s*[A-Z]{2,}[A-Z0-9]*\s*$")


def search_title(title):
    """The title to ASK TMDb for, which is not the title to show.

    clean_title() is held in step with the daemon and decides what a child sees
    on the shelf, so it is left alone. This is the other half: strip the year,
    the edition, and the half-open bracket that a truncating TAG_RE leaves
    behind ("Sphere (1998", "Valkyrie (2008"), and hand over the plain name of
    the film. Nothing here reaches the index.
    """
    text = re.sub(r"[._]+", " ", title or "")
    text = TAG_RE.sub("", text)
    text = EDITION_RE.sub(" ", text)
    text = SEARCH_NOISE_RE.sub(" ", text)
    text = GROUP_SUFFIX_RE.sub("", text)
    # Any plausible year, anywhere -- bracketed, bare or trailing -- except one
    # that opens the string, which is a title rather than a date: "2001 A Space
    # Odyssey" keeps its 2001. The search carries the year as its own
    # parameter, so leaving it in the query only makes the string match worse.
    text = re.sub(
        r"[\(\[]?\b((?:19|20)\d{2})\b[\)\]]?",
        lambda m: "" if m.start() and plausible_year(m.group(1)) else m.group(0),
        text)
    # An unbalanced bracket is what a truncating TAG_RE leaves behind, and half
    # a parenthesis is worse than none.
    if text.count("(") != text.count(")") or text.count("[") != text.count("]"):
        text = re.sub(r"[\(\)\[\]]", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -,:[](){}")
    # "Blade Runner The Final Cut" loses its edition and is left holding an
    # article that used to belong to it.
    text = re.sub(r"[\s,-]+(?:the|a|an)$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*-\s*$|\s+-\s+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" -,:[](){}")
    return text or (title or "").strip()


def title_is_release_string(text):
    """Does this look like a scene release rather than the name of a film?

    Asked only of a title read out of a container tag. Muxers write whatever
    was in front of them, and what is in front of them is usually the torrent
    name; every one of these signals was taken from a title that really was
    sitting in a real file in a real library.
    """
    if not text:
        return False
    if TAG_RE.search(text):
        return True                                   # 1080p, x264, BRrip, DTS
    if GROUP_SUFFIX_RE.search(text):
        return True                                   # "Contact - YIFY"
    if re.search(r"\b\d\.\d\b", text):
        return True                                   # a channel layout
    if re.search(r"\w\.\w+\.\w", text):
        return True                                   # "Boss.Level.2020..."
    # A plausible year anywhere except as the opening token, which is where a
    # real title has one: "2001 A Space Odyssey" and "1917" survive, while
    # "The Recruit (2003)" and "... SPIDERMAN 3 2007 !" do not.
    for match in re.finditer(r"\b((?:19|20)\d{2})\b", text):
        if match.start() > 0 and plausible_year(match.group(1)):
            return True
    return False


# --- metadata --------------------------------------------------------------

def parse_nfo(path):
    """Kodi/Jellyfin .nfo sidecars, which is how most real libraries already
    carry a rating and a genre list. Anything unparseable is ignored: an .nfo
    is frequently a bare URL, and half of them have a stray & in them."""
    try:
        text = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    start = text.find("<movie")
    if start < 0:
        return {}
    try:
        root = ET.fromstring(text[start:])
    except ET.ParseError:
        return {}

    meta = {}
    title = (root.findtext("title") or root.findtext("originaltitle") or "").strip()
    if title:
        meta["title"] = title
    year = (root.findtext("year") or "").strip()
    if year.isdigit():
        meta["year"] = int(year)
    elif (root.findtext("premiered") or "")[:4].isdigit():
        meta["year"] = int(root.findtext("premiered")[:4])

    # <rating> in old files, <ratings><rating><value> in current ones.
    rating = root.findtext("rating")
    if rating is None:
        node = root.find("./ratings/rating/value")
        rating = node.text if node is not None else None
    if rating:
        try:
            value = float(rating)
            # Some writers store a 0-100 scale. Normalise to 0-10.
            meta["rating"] = round(value / 10.0, 1) if value > 10 else round(value, 1)
        except ValueError:
            pass

    genres = [g.text.strip() for g in root.findall("genre") if (g.text or "").strip()]
    # One <genre>Action / Sci-Fi</genre> is as common as several elements.
    flat = []
    for item in genres:
        flat.extend(p.strip() for p in re.split(r"[/,|]", item) if p.strip())
    if flat:
        meta["genres"] = dedupe_keep_order(flat)

    runtime = (root.findtext("runtime") or "").strip()
    if runtime.isdigit():
        meta["expected_runtime"] = int(runtime) * 60
    plot = (root.findtext("plot") or "").strip()
    if plot:
        meta["plot"] = plot[:600]
    if meta:
        meta["metadata_source"] = "nfo"
    return meta


def find_nfo(path):
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    for cand in (os.path.join(directory, stem + ".nfo"),
                 os.path.join(directory, "movie.nfo")):
        if os.path.isfile(cand):
            return cand
    return None


def metadata_from_tags(info):
    """Whatever the muxer left behind. Rarely a rating, sometimes a genre,
    often a year -- and quite often a title that is better than the filename."""
    tags = {k.lower(): v for k, v in (info.get("format", {}).get("tags") or {}).items()}
    meta = {}
    title = (tags.get("title") or "").strip()
    # Muxers love writing the filename, or the encoder's name, into the title.
    if title and not re.search(r"\b(x26[45]|encoded|handbrake|mkvmerge)\b", title, re.I):
        meta["title"] = title
    # NOT creation_time. It is the moment the file was muxed, which for a scene
    # release is years or decades after the film came out -- Hook's 720p rip
    # carries 2012-10-08 and no other date at all. Reading it as a release year
    # is what asked TMDb for a 1991 film released in 2012 and got back Red Hook
    # Summer, with its poster and its plot, on the shelf a child picks from. It
    # was the ONLY date-ish tag on 69 of the 547 films in the library this was
    # written for, so nothing is lost by refusing it and a great deal is.
    for key in ("date", "year", "originalyear"):
        value = str(tags.get(key) or "")
        match = re.search(r"(19|20)\d{2}", value)
        if match:
            meta["year"] = int(match.group(0))
            break
    genre = (tags.get("genre") or "").strip()
    if genre:
        parts = [p.strip() for p in re.split(r"[/,;|]", genre) if p.strip()]
        if parts:
            meta["genres"] = dedupe_keep_order(parts)
    rating = tags.get("rating") or tags.get("imdb_rating")
    if rating:
        try:
            meta["rating"] = round(float(str(rating).split("/")[0]), 1)
        except ValueError:
            pass
    if meta:
        meta["metadata_source"] = "tags"
    return meta


def dedupe_keep_order(items):
    seen = set()
    out = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def title_match_score(want, got):
    """0-100: how much two titles look like the same film.

    normalize_title() has already dealt with accents, punctuation, articles and
    "&" versus "and", so what is left is the interesting part -- one side
    saying more than the other, and one side spelling it differently.
    """
    a, b = normalize_title(want), normalize_title(got)
    if not a or not b:
        return 0
    if a == b:
        return 100
    # A whole-word prefix or suffix, and WHICH SIDE is the longer one decides
    # how much that is worth. A ripper decorates: "Star Wars Episode 4 A New
    # Hope" is the file's name for "Star Wars", and stripping decoration back
    # to a canonical title is a match. TMDb does not decorate -- its titles are
    # canonical -- so when the candidate is the longer one it is usually a
    # different, more specific film: "Munich" is not "Lost in Munich", and
    # "Home Alone" is not "Home Alone 2: Lost in New York". Both are scaled by
    # how much of the longer string the shorter one covers.
    short, wide = sorted((a, b), key=len)
    if wide.startswith(short + " ") or wide.endswith(" " + short):
        coverage = len(short) / float(len(wide))
        if wide == a:
            return 60 + int(35 * coverage)      # the file said more
        return 40 + int(25 * coverage)          # TMDb said more
    # Everything else: a spelling difference, a regional title, a typo in the
    # filename. "Contac" -> "Contact" and "Sorcerers" -> "Philosophers" both
    # live here.
    return int(95 * difflib.SequenceMatcher(None, a, b).ratio())


class Tmdb:
    """Optional, opt-in, and the only thing here that touches the network.

    Enabled by --tmdb-key, which sends the film titles in your library to
    themoviedb.org. That is a real disclosure and it is why this is off by
    default rather than merely unconfigured.
    """

    BASE = "https://api.themoviedb.org/3"
    IMG = "https://image.tmdb.org/t/p/w500"

    # How sure this has to be before it will put another film's name, poster
    # and plot on your file. An exact title with no year still clears it; a
    # title that merely contains the right words does not.
    #
    # The number is not arbitrary. "Star Wars" scores 65 against "Star Wars
    # Episode 4 A New Hope", and 95 with the year agreeing -- so 90 is the line
    # that keeps the honest partial matches and drops "Hook" -> "Red Hook
    # Summer", which reaches 70 even when the year it was searched with is the
    # wrong one that caused the whole problem.
    ACCEPT = 90

    # And a floor on the name alone, whatever the rest of the evidence says. A
    # film whose title does not look right is not rescued by a runtime that
    # happens to agree -- two films of the same length are not the same film.
    # The lowest a real match measured here is "Star Wars" against "Star Wars
    # Episode 4 A New Hope", at exactly 70.
    MIN_TITLE = 65

    # How many leaders are worth a second request to check the runtime against.
    DETAIL_CANDIDATES = 3

    def __init__(self, key, cache_path, language="en-US"):
        self.key = key
        self.language = language
        self.cache_path = cache_path
        self.cache = {}
        self._genres = None
        self._last_call = 0.0
        if cache_path and os.path.isfile(cache_path):
            try:
                self.cache = json.load(open(cache_path, "r", encoding="utf-8"))
            except (OSError, ValueError):
                self.cache = {}

    def save(self):
        if not self.cache_path:
            return
        try:
            write_json_atomic(self.cache_path, self.cache)
        except OSError as exc:
            warn("could not write the TMDb cache: %s", exc)

    def _get(self, path, params):
        params = dict(params, api_key=self.key, language=self.language)
        url = "%s%s?%s" % (self.BASE, path, urllib.parse.urlencode(params))
        cache_key = url.replace(self.key, "KEY")
        if cache_key in self.cache:
            return self.cache[cache_key]
        # TMDb's published limit is generous, but a library of a few hundred
        # films should still not arrive as a burst.
        delta = time.time() - self._last_call
        if delta < 0.3:
            time.sleep(0.3 - delta)
        self._last_call = time.time()
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError) as exc:
            warn("TMDb lookup failed: %s", exc)
            return None
        self.cache[cache_key] = data
        return data

    def genre_names(self, ids):
        if self._genres is None:
            data = self._get("/genre/movie/list", {}) or {}
            self._genres = {g["id"]: g["name"] for g in data.get("genres", [])}
        return [self._genres[i] for i in ids if i in self._genres]

    def search(self, query, year):
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = str(year)
        data = self._get("/search/movie", params) or {}
        return data.get("results") or []

    def candidates(self, query, year):
        """Everything worth scoring, from at most two searches."""
        pool, seen = [], set()

        def add(items):
            for item in items[:10]:
                key = item.get("id")
                if key is not None and key not in seen:
                    seen.add(key)
                    pool.append(item)

        want = normalize_title(query)
        if year:
            add(self.search(query, year))
            # One search is enough when the year filter has already produced
            # the film by name. Otherwise the year is the suspect: a filename
            # frequently carries another country's release date, and dropping
            # the filter is the only way to see the film at all. Note this no
            # longer *replaces* the first search the way the old retry did --
            # both sets are scored together, so a near-miss under the right
            # year still beats a popular stranger under no year.
            if any(normalize_title(c.get("title") or "") == want
                   or normalize_title(c.get("original_title") or "") == want
                   for c in pool):
                return pool
        add(self.search(query, None))
        return pool

    @staticmethod
    def year_term(candidate_year, want_year):
        if not want_year or not candidate_year:
            return 0
        gap = abs(candidate_year - want_year)
        if gap == 0:
            return 30
        if gap == 1:
            return 18        # a release date is a different day per country
        if gap == 2:
            return 5
        # Bounded at -30, deliberately. A year that is far out is evidence
        # against, but it is the weakest of the three signals -- it is the one
        # a muxer, a region or a folder name can get wrong on its own -- and an
        # exact title backed by an exact runtime has to be able to outweigh it.
        # Aladdin (1992) under a filed year of 2013 is still Aladdin.
        return -5 * min(gap, 6)

    @staticmethod
    def runtime_term(runtime, duration, confident):
        """How well TMDb's runtime agrees with the film actually in the file.

        The one signal here that owes nothing to anybody's spelling, and the
        one that would have caught Red Hook Summer on its own: 121 minutes of
        it against 142 minutes of Hook.

        `confident` says the title and the year already agree exactly, in which
        case a long file is an extended cut rather than a different film -- the
        Abyss and the Aliens on this shelf are both half an hour over what TMDb
        lists, and disqualifying them would be the same class of mistake in the
        other direction.
        """
        if not runtime or not duration:
            return 0
        off = abs(duration - runtime) / float(runtime)
        if off <= 0.05:
            return 25
        if off <= 0.10:
            return 15
        if off <= 0.25:
            return 0
        return -15 if confident else -60

    def score(self, item, query, year):
        title = max(title_match_score(query, item.get("title") or ""),
                    title_match_score(query, item.get("original_title") or ""))
        release = (item.get("release_date") or "")[:4]
        cyear = int(release) if release.isdigit() else None
        score = title + self.year_term(cyear, year)
        # Popularity decides nothing on its own -- it is precisely what put Red
        # Hook Summer above Hook -- but between two candidates that look
        # equally like the film, the one the world has heard of is the better
        # guess. Two points, and it can only ever break a tie.
        score += min(item.get("vote_count") or 0, 2000) / 1000.0
        return score, title, cyear

    def lookup(self, title, year=None, duration=None):
        """The best-matching film, or nothing at all.

        Returns (meta, note). `note` is None when a match was accepted and a
        one-line explanation when it was not -- because the alternative to
        saying "I could not tell" is saying "Red Hook Summer", and a shelf a
        child picks from is the worst place to guess.
        """
        query = search_title(title)
        pool = self.candidates(query, year)
        if not pool:
            return {}, "TMDb returned nothing for %r" % query

        scored = [list(self.score(item, query, year)) + [item, None]
                  for item in pool]
        scored.sort(key=lambda row: -row[0])

        # Only the leaders are worth the second request. Anything more than 25
        # behind cannot be rescued by a runtime that agrees, and a library of a
        # few hundred films should not pay for a lookup it will not use.
        leader = scored[0][0]
        for row in scored[:self.DETAIL_CANDIDATES]:
            base, tscore, cyear, item, _ = row
            if base < leader - 25:
                break
            detail = self._get("/movie/%s" % item["id"], {}) or {}
            row[4] = detail
            row[0] = base + self.runtime_term(
                int(detail.get("runtime") or 0) * 60, duration,
                confident=(tscore >= 100 and cyear == year and year is not None))
        scored.sort(key=lambda row: -row[0])

        best, tscore, cyear, item, detail = scored[0]
        if best < self.ACCEPT or tscore < self.MIN_TITLE:
            return {}, ("best was %s (%s), score %d of %d" %
                        (item.get("title") or "?", cyear or "?",
                         round(best), self.ACCEPT))
        if detail is None:
            detail = self._get("/movie/%s" % item["id"], {}) or {}

        meta = {"metadata_source": "tmdb", "tmdb_id": item["id"],
                "tmdb_score": round(best)}
        if item.get("title"):
            meta["title"] = item["title"]
        if cyear:
            meta["year"] = cyear
        if item.get("vote_average"):
            meta["rating"] = round(float(item["vote_average"]), 1)
        if item.get("vote_count"):
            meta["rating_votes"] = item["vote_count"]
        genres = [g["name"] for g in detail.get("genres", [])]
        if not genres and item.get("genre_ids"):
            genres = self.genre_names(item["genre_ids"])
        if genres:
            meta["genres"] = genres
        if detail.get("runtime"):
            meta["expected_runtime"] = int(detail["runtime"]) * 60
        if item.get("overview"):
            meta["plot"] = item["overview"][:600]
        if item.get("poster_path"):
            meta["poster_url"] = self.IMG + item["poster_path"]
        return meta, None


# --- discovery -------------------------------------------------------------

def discover(library, args):
    """Every candidate film, with the obvious non-films already gone.

    Returns (films, episodic) -- the second list is reported and written into
    the index's "excluded" section rather than dropped on the floor, because
    "where did my box set go" needs an answer that is not "read the source".
    """
    found = []
    episodic = []
    skip_re = re.compile(args.skip_pattern, re.I) if args.skip_pattern else None
    lower_skip = {s.lower() for s in SKIP_DIRS}
    for dirpath, dirnames, filenames in os.walk(library):
        rel_dir = os.path.relpath(dirpath, library)
        keep = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in SKIP_DIRS or name.lower() in lower_skip:
                continue
            if args.skip_episodes and EPISODE_DIR_RE.match(name):
                rel = "." if rel_dir == "." else rel_dir
                episodic.append(os.path.join("" if rel == "." else rel, name) + os.sep)
                log("  skip (season folder): %s", name, level="verbose")
                continue
            keep.append(name)
        dirnames[:] = keep
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= args.max_depth:
            dirnames[:] = []
        for name in sorted(filenames):
            if name.startswith(".") or not name.lower().endswith(VIDEO_EXT):
                continue
            if skip_re and skip_re.search(name):
                log("  skip (name): %s", name, level="verbose")
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, library)
            # The whole relative path, not just the filename: an episode whose
            # file is called "03.mkv" is only recognisable by the folder it
            # sits in.
            if args.skip_episodes and EPISODE_RE.search(rel):
                episodic.append(rel)
                log("  skip (episodic): %s", rel, level="verbose")
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < args.min_size_mb * 1024 * 1024:
                log("  skip (%s): %s", human_size(size), name, level="verbose")
                continue
            found.append(path)
    return found, episodic


def quick_signature(path, size):
    """Cheap identity: size plus the head and tail of the file. Two files that
    agree are the same file for every practical purpose here; hashing 20 GB
    over a network mount to be certain would cost more than the whole run."""
    chunk = 1024 * 1024
    digest = hashlib.sha1()
    digest.update(str(size).encode())
    try:
        with open(path, "rb") as handle:
            digest.update(handle.read(chunk))
            if size > chunk * 2:
                handle.seek(-chunk, os.SEEK_END)
                digest.update(handle.read(chunk))
    except OSError:
        return None
    return digest.hexdigest()[:20]


# --- de-duplication --------------------------------------------------------

def quality_score(movie):
    """Which copy of a film to keep.

    Ordered by what actually matters on this hardware rather than by what looks
    biggest: a native 720p H.264 file plays untouched, a 4K HEVC one has to be
    re-encoded before it can play at all, and the re-encode cannot recover
    detail that 720p output has nowhere to put.
    """
    score = 0.0
    if movie.get("verified_ok"):
        score += 10000
    height = movie.get("height") or 0
    if 700 <= height <= 1120:
        score += 400                      # 720p or 1080p: the sweet spot
    elif height > 1120:
        score += 250                      # more pixels than the display has
    else:
        score += 400 * (height / 720.0)   # below the display's own resolution
    if movie.get("vcodec") == "h264" and movie.get("eight_bit"):
        score += 200                      # no transcode needed
    score += min(to_float(movie.get("bitrate")) / 1e6, 25.0) * 6
    score += 40 * min(len(movie.get("subtitle_streams") or []), 3)
    if movie.get("external_subs"):
        score += 40
    if os.path.splitext(movie["source"])[1].lower() in (".mkv", ".mp4", ".m4v"):
        score += 30
    if movie.get("nfo"):
        score += 25
    return score


def group_duplicates(movies):
    """Bucket by normalised title and year.

    Year is only a divider when both copies know theirs: a file named
    "Ponyo.mkv" next to "Ponyo (2008).mkv" is one film with one copy missing a
    year, not two films, and treating it as two puts both in the grid.
    """
    by_title = {}
    for movie in movies:
        by_title.setdefault(movie["norm_title"], []).append(movie)

    groups = []
    for _title, bucket in by_title.items():
        years = {m["year"] for m in bucket if m.get("year")}
        if len(years) <= 1:
            groups.append(bucket)
            continue
        by_year = {}
        undated = []
        for movie in bucket:
            if movie.get("year"):
                by_year.setdefault(movie["year"], []).append(movie)
            else:
                undated.append(movie)
        # An undated copy joins the largest dated group -- a remake is the rare
        # case, a missing year is the common one.
        biggest = max(by_year.values(), key=len)
        biggest.extend(undated)
        groups.extend(by_year.values())
    return groups


def pick_duplicates(movies, args):
    """Marks losers in place and returns (kept, dropped)."""
    kept, dropped = [], []

    # Byte-identical copies first: those are unambiguous whatever the titles
    # parsed to, including the case where the same file is hard-linked or
    # copied into two folders with different names.
    by_signature = {}
    for movie in movies:
        sig = movie.get("signature")
        if sig:
            by_signature.setdefault(sig, []).append(movie)
    for sig, same in by_signature.items():
        if len(same) < 2:
            continue
        same.sort(key=lambda m: m["source_rel"])
        for loser in same[1:]:
            loser["duplicate_of"] = same[0]["source_rel"]
            loser["duplicate_reason"] = "identical file (%s)" % sig

    for group in group_duplicates([m for m in movies if not m.get("duplicate_of")]):
        if len(group) == 1:
            kept.append(group[0])
            continue
        # A copy running much shorter than its siblings is a different cut, or
        # is not the whole film -- either way it loses to the longer one, and
        # saying so is more useful than silently preferring the bigger file.
        durations = sorted(m.get("duration") or 0 for m in group)
        median = durations[len(durations) // 2]
        for movie in group:
            dur = movie.get("duration") or 0
            if median and dur < median * 0.85:
                movie["short_cut"] = True
        ranked = sorted(group, key=lambda m: (m.get("short_cut", False),
                                              -quality_score(m),
                                              m["source_rel"]))
        winner = ranked[0]
        kept.append(winner)
        for loser in ranked[1:]:
            loser["duplicate_of"] = winner["source_rel"]
            loser["duplicate_reason"] = describe_loss(winner, loser)

    dropped = [m for m in movies if m.get("duplicate_of")]
    return kept, dropped


def move_duplicates(dropped, args, library):
    """Opt-in, and the only thing in this script that takes a film away from
    where it was. It moves; nothing here has an unlink in it, so a mistake is
    recoverable by moving the directory back.
    """
    dest_root = os.path.abspath(os.path.expanduser(args.duplicates_dir))
    if dest_root == library or dest_root.startswith(library + os.sep):
        die("--duplicates-dir %s is inside the library, which would just move "
            "the duplicates somewhere else for the next run to find",
            dest_root)
    moved = 0
    for movie in dropped:
        if not movie.get("duplicate_of"):
            continue                      # rejected, not duplicated: leave it
        dest = os.path.join(dest_root, movie["source_rel"])
        if os.path.exists(dest):
            warn("not moving %s: %s already exists", movie["source_rel"], dest)
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(movie["source"], dest)
            moved += 1
            log("  moved %s -> %s", movie["source_rel"], dest)
        except OSError as exc:
            warn("could not move %s: %s", movie["source_rel"], exc)
    log("moved %d duplicate(s) into %s", moved, dest_root)


def describe_loss(winner, loser):
    bits = []
    if loser.get("short_cut"):
        bits.append("%s vs %s" % (hms(loser.get("duration")), hms(winner.get("duration"))))
    if (loser.get("height") or 0) != (winner.get("height") or 0):
        bits.append("%sp vs %sp" % (loser.get("height"), winner.get("height")))
    if loser.get("vcodec") != winner.get("vcodec"):
        bits.append("%s vs %s" % (loser.get("vcodec"), winner.get("vcodec")))
    if not bits:
        bits.append("lower score")
    return ", ".join(bits)


# --- preparation -----------------------------------------------------------

def transcode_plan(movie, args):
    """Why this file does or does not need an encode.

    Returns (needed, reasons). The reasons are kept because "it re-encoded
    everything and took nine hours" is the failure mode of a tool like this,
    and being able to see that one file tripped --max-bitrate by 3% is the
    difference between trusting it and not.
    """
    if args.transcode == "never":
        return False, []
    if args.transcode == "always":
        return True, ["--transcode always"]

    reasons = []
    if movie.get("vcodec") != "h264":
        reasons.append("%s is not H.264" % (movie.get("vcodec") or "unknown codec"))
    if not movie.get("eight_bit"):
        reasons.append("%s is not 8-bit 4:2:0" % movie.get("pix_fmt"))
    height, width = movie.get("height") or 0, movie.get("width") or 0
    if height > args.height + 8 or width > args.width + 8:
        reasons.append("%dx%d is larger than the display's %dx%d"
                       % (width, height, args.width, args.height))
    fps = movie.get("fps") or 0
    if fps > args.max_fps + 0.5:
        reasons.append("%.1f fps is above %d" % (fps, args.max_fps))
    rate = movie.get("bitrate") or 0
    if args.max_bitrate and rate > args.max_bitrate * 1.05:
        reasons.append("%.1f Mbps is above --max-bitrate %.1f"
                       % (rate / 1e6, args.max_bitrate / 1e6))
    profile = (movie.get("profile") or "").lower()
    if profile in ("high 10", "high 4:2:2", "high 4:4:4 predictive"):
        reasons.append("H.264 %s profile" % profile)
    return bool(reasons), reasons


def build_transcode_argv(movie, dest, args):
    src = movie["source"]
    argv = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", src, "-map", "0:v:0"]

    audio = movie.get("audio_index")
    if audio is not None:
        argv += ["-map", "0:%d" % audio]

    filters = []
    # force_original_aspect_ratio=decrease keeps anamorphic and 2.35:1 films in
    # proportion; the second scale rounds to even dimensions, which libx264
    # requires for 4:2:0 and which a decrease-fit will otherwise hand it odd.
    filters.append("scale=w=%d:h=%d:force_original_aspect_ratio=decrease:flags=bicubic"
                   % (args.width, args.height))
    filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    if (movie.get("fps") or 0) > args.max_fps + 0.5:
        filters.append("fps=%d" % args.max_fps)
    argv += ["-vf", ",".join(filters)]

    gop = args.max_fps * 2
    argv += [
        "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
        "-profile:v", "high", "-level", "4.0", "-pix_fmt", "yuv420p",
        # A cap as well as a CRF: CRF alone will happily spend 20 Mbps on
        # grain, and the ceiling here is the CIFS read over Wi-Fi, not the
        # decoder.
        "-maxrate", str(int(args.max_bitrate)), "-bufsize", str(int(args.max_bitrate * 2)),
        "-x264-params", "keyint=%d:min-keyint=%d:scenecut=40" % (gop, gop),
    ]
    if audio is not None:
        argv += ["-c:a", "aac", "-b:a", args.audio_bitrate, "-ac", "2"]
    else:
        argv += ["-an"]
    # Subtitles are extracted to sidecars instead: burning them in is
    # irreversible and muxing them into mp4 costs a codec conversion for no
    # gain, since the daemon points mpv at the SRT explicitly.
    # -f mp4 explicitly, because the encode writes to a ".part" file first and
    # ffmpeg picks the muxer from the extension: without this it stops with
    # "Unable to choose an output format", having already done the work.
    argv += ["-sn", "-dn", "-map_chapters", "-1", "-movflags", "+faststart",
             "-metadata", "title=" + (movie.get("title") or ""),
             "-f", "mp4", dest]
    return argv


def encode_name(ident):
    """The one name an encode of this film is allowed to have.

    Not the title. The name used to carry a slug of it, and the title is
    derived afresh every run -- from the filename, then container tags, then an
    .nfo, then TMDb -- so an .nfo appearing, a match arriving, or an edit to
    normalize_title() in this file renamed the encode. The isfile() check in
    do_transcode() then looked for a name that no longer existed and encoded the
    whole film again, next to the copy it already had. The id is a sha1 of the
    source's path and does not move.
    """
    return "%s.mp4" % ident


def existing_encodes(media_dir, ident):
    """Every finished encode belonging to this film, newest first.

    Claims "<id>.mp4" and "<id>-<anything>.mp4" -- the hyphen is required, so a
    name that merely starts with the same 16 characters is somebody else's file.
    A ".part" is a staging file, not an encode; reconcile_encodes() deals with
    those separately.
    """
    try:
        names = os.listdir(media_dir)
    except OSError:
        return []                      # no media directory yet, which is fine
    found = []
    for name in names:
        if not name.endswith(".mp4"):
            continue
        stem = name[:-len(".mp4")]
        if stem != ident and not stem.startswith(ident + "-"):
            continue
        path = os.path.join(media_dir, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue                   # vanished between listdir and stat
        found.append((mtime, name, path))
    # Newest first, and then two tie-breaks so that a directory whose files
    # share an mtime -- which a copy onto a NAS produces easily -- resolves the
    # same way on every run rather than by whatever order the filesystem
    # happened to hand back.
    found.sort(key=lambda e: (-e[0], e[1] != encode_name(ident), e[1]))
    return [(path, name) for _, name, path in found]


def staging_files(media_dir, ident):
    """Half-written encodes of this film, under any name it has ever had."""
    try:
        names = os.listdir(media_dir)
    except OSError:
        return []
    return [(os.path.join(media_dir, name), name) for name in names
            if name.endswith(".part")
            and (name.startswith(ident + ".") or name.startswith(ident + "-"))]


def reconcile_encodes(movie, paths):
    """Collapse every encode of this film onto one file, newest wins.

    Returns the surviving basename, or None if there was nothing there.

    This is the only unlink in the script that removes something somebody might
    want, and move_duplicates() makes a point of not having one. The difference
    is that everything under .playstick/ is DERIVED: the worst a wrong answer
    here costs is one re-encode, and the film it was made from has not been
    touched. A duplicate encode costs a few gigabytes of the share and hours of
    a machine, silently, because every step of producing it succeeded.
    """
    ident = movie["id"]
    media_dir = os.path.join(paths["output"], WORK_DIR, "media")
    canonical = encode_name(ident)

    # Leftovers from a run that was killed rather than interrupted -- an
    # interrupted one discards its own. Nothing ever reads these, and this runs
    # before the new staging file is created, so it cannot race one.
    for path, name in staging_files(media_dir, ident):
        discard(path)
        log("  - leftover staging file: %s", name, level="verbose")

    found = existing_encodes(media_dir, ident)
    for path, name in found[1:]:
        try:
            os.unlink(path)
            log("  - superseded encode: %s", name)
        except OSError as exc:
            warn("could not remove the superseded encode %s: %s", name, exc)

    if not found:
        return None
    keep_path, keep_name = found[0]
    if keep_name == canonical:
        return keep_name
    try:
        # Same directory, so this is atomic, and a descriptor the daemon
        # already has open on it stays valid across the rename.
        os.replace(keep_path, os.path.join(media_dir, canonical))
    except OSError as exc:
        # A read-only mount, or a share that will not rename a file in use.
        # Carrying on under the old name is strictly better than encoding it
        # again, which is the whole bug this function exists for.
        warn("could not rename %s to %s: %s", keep_name, canonical, exc)
        return keep_name
    log("  ~ %s -> %s", keep_name, canonical)
    return canonical


def do_transcode(movie, args, paths):
    dest_rel = os.path.join(WORK_DIR, "media", encode_name(movie["id"]))
    dest = os.path.join(paths["output"], dest_rel)
    survivor = reconcile_encodes(movie, paths)
    if survivor and not args.force:
        movie["media_rel"] = os.path.join(WORK_DIR, "media", survivor)
        movie["prepared"] = True
        log("  = transcode already present: %s", survivor, level="verbose")
        return True

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    staging = dest + ".part"
    argv = build_transcode_argv(movie, staging, args)
    log("  > encoding %s (%s)", movie["title"], ", ".join(movie["transcode_reasons"]))
    log("    %s", " ".join(argv), level="verbose")
    started = time.time()
    argv, watching = with_progress(argv, "encoding", movie.get("duration"))
    res = run(argv, timeout=args.transcode_timeout, progress=watching)
    if res.returncode != 0:
        if cancelled():
            log("  - encode of %s stopped part way; nothing kept", movie["title"])
        else:
            warn("encode failed for %s: %s", movie["title"],
                 first_line(res.stderr or ""))
        discard(staging)
        return False
    os.replace(staging, dest)
    # --force, on a film whose old encode could not be renamed above. The new
    # one has landed, so the old name is now genuinely spare -- and the reason
    # it is removed here rather than before the encode is that a --force run
    # which fails, or is stopped, must leave the library exactly as it found it.
    if survivor and survivor != os.path.basename(dest):
        discard(os.path.join(paths["output"], WORK_DIR, "media", survivor))
        log("  - superseded encode: %s", survivor)
    movie["media_rel"] = dest_rel
    movie["prepared"] = True
    movie["transcoded_in"] = round(time.time() - started, 1)
    try:
        movie["media_size"] = os.path.getsize(dest)
    except OSError:
        pass
    log("  + %s in %s (%s)", os.path.basename(dest), hms(time.time() - started),
        human_size(movie.get("media_size", 0)))
    return True


def extract_subtitles(movie, args, paths):
    """Text subtitles, as UTF-8 SRT, one file per language.

    Image-based subtitles (PGS on Blu-ray, VobSub on DVD) are skipped: turning
    those into text needs OCR, which is a dependency and a quality argument
    this tool should not be having. mpv can render them from the original file,
    but only when the original is what gets played.
    """
    out = []
    subs_dir = os.path.join(paths["output"], WORK_DIR, "subs")
    wanted = [lang.strip().lower() for lang in args.sub_langs.split(",") if lang.strip()]

    for path in movie.get("external_subs", []):
        if cancelled():
            break
        lang = language_from_name(path) or "und"
        if wanted and lang not in wanted and lang != "und":
            continue
        rel = os.path.join(WORK_DIR, "subs", "%s.%s.srt" % (movie["id"], lang))
        dest = os.path.join(paths["output"], rel)
        if os.path.isfile(dest) and not args.force:
            out.append({"lang": lang, "rel": rel, "origin": "external"})
            continue
        os.makedirs(subs_dir, exist_ok=True)
        # Staged and renamed rather than written in place: the share is mounted
        # on the stick with actimeo=60, so a file rewritten under a name the
        # daemon may already be reading can be seen at a stale size for a
        # minute afterwards. os.replace is atomic and sidesteps the question.
        # -f srt explicitly, for the reason build_transcode_argv() spells out:
        # the staging name ends in .part, and ffmpeg picks its muxer from the
        # extension, so without this it stops with "Unable to choose an output
        # format" having already done the work.
        staging = dest + ".part"
        res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                   "-sub_charenc", args.sub_charenc, "-i", path, "-c:s", "srt",
                   "-f", "srt", staging],
                  timeout=args.subtitle_timeout)
        if res.returncode == 0 and os.path.isfile(staging):
            os.replace(staging, dest)
            out.append({"lang": lang, "rel": rel, "origin": "external"})
        else:
            if not cancelled():
                warn("subtitle conversion failed for %s: %s",
                     os.path.basename(path), first_line(res.stderr or ""))
            discard(staging)

    seen_langs = {s["lang"] for s in out}
    for stream in movie.get("subtitle_streams", []):
        if len(out) >= args.max_subs or cancelled():
            break
        codec = (stream.get("codec_name") or "").lower()
        if codec not in TEXT_SUB_CODECS:
            log("  skip image subtitles (%s) in %s", codec, movie["title"], level="verbose")
            continue
        lang = ((stream.get("tags") or {}).get("language") or "und").lower()
        if wanted and lang not in wanted and lang != "und":
            continue
        if lang in seen_langs:
            continue
        rel = os.path.join(WORK_DIR, "subs", "%s.%s.srt" % (movie["id"], lang))
        dest = os.path.join(paths["output"], rel)
        if os.path.isfile(dest) and not args.force:
            out.append({"lang": lang, "rel": rel, "origin": "embedded"})
            seen_langs.add(lang)
            continue
        os.makedirs(subs_dir, exist_ok=True)
        staging = dest + ".part"
        res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", movie["source"], "-map", "0:%d" % stream["index"],
                   "-c:s", "srt", "-f", "srt", staging],
                  timeout=args.subtitle_timeout)
        if res.returncode == 0 and os.path.isfile(staging) and os.path.getsize(staging) > 0:
            os.replace(staging, dest)
            out.append({"lang": lang, "rel": rel, "origin": "embedded"})
            seen_langs.add(lang)
        else:
            discard(staging)
    movie["subtitles"] = out
    return out


def build_phone_audio_argv(movie, stream, lang, dest, args):
    """One audio track, decoded and re-encoded to something a phone can play.

    Three of these flags are load-bearing and the rest are housekeeping.

    -map 0:a:<n> uses the position among the audio streams rather than the
    absolute stream index, because <n> is what the phone's URL carries and what
    the daemon indexes its list by. An ffmpeg stream index never leaves this
    file.

    NO -copyts. Without it ffmpeg shifts every stream in the input by the
    file's own start_time, which is exactly what mpv does when it reports
    time-pos -- so both sides put the same instant at the same number. With it,
    an MPEG-TS source's 4000-second origin would survive into the output and
    Safari would announce a four-thousand-second track.

    aresample=first_pts=0 is the one that decides whether this feature works at
    all. ffmpeg's shift is per FILE, so an audio stream that starts 120 ms
    after the video keeps that 120 ms afterwards; first_pts=0 pads or trims the
    head so that output sample zero really is the film's zero. Without it the
    phone is permanently 120 ms out of lip sync and nothing on the page can
    discover that -- it is a constant, and constants are invisible to a drift
    correction that only ever measures change.
    """
    channels = stream.get("channels") or 0
    chain = []
    if channels >= 6 and args.phone_audio_downmix == "dialogue":
        chain.append(PHONE_AUDIO_PAN)
    chain.append("aresample=async=1:first_pts=0")

    argv = [FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
            "-i", movie["source"],
            "-map", "0:a:%d" % stream["n"],
            "-vn", "-sn", "-dn", "-map_chapters", "-1",
            "-af", ",".join(chain),
            "-c:a", PHONE_AUDIO_CODEC, "-profile:a", "aac_low",
            "-b:a", args.phone_audio_bitrate,
            "-ar", "48000"]
    # The pan filter already outputs stereo; asking for it twice is how you get
    # ffmpeg to insert a second downmix after the one that was the whole point.
    if PHONE_AUDIO_PAN not in chain:
        argv += ["-ac", "2"]
    argv += ["-movflags", "+faststart",
             "-metadata", "title=" + (movie.get("title") or ""),
             "-metadata:s:a:0", "language=" + lang,
             "-f", "mp4", dest]
    return argv


def check_phone_audio(dest, movie, args):
    """Prove the track that was just written shares the film's timeline.

    Encoding it is the easy half. This is the half that catches the failures
    that would otherwise surface as "the sound goes wrong somewhere in the
    middle", weeks later, on somebody's phone, in the dark.

    Two questions, and they catch different things. A non-zero start_time means
    first_pts=0 did not do its job and every listener will be out by that much
    for the whole film. A duration that disagrees with the film's means the
    timelines diverge somewhere in the middle -- ordered chapters, a TS
    discontinuity, a concatenated source -- which no per-listener trim can fix,
    because the error is not constant.

    Returns (offset, suspect) or None if the file cannot be read back.
    """
    info, error = ffprobe_json(dest, timeout=args.probe_timeout)
    if info is None:
        if not cancelled():
            warn("cannot read back %s: %s", os.path.basename(dest), error)
        return None
    got = (audio_streams(info) or [{}])[0]
    offset = round(to_float(got.get("start_time")), 3)
    out_dur = duration_of(info)
    film_dur = movie.get("duration") or 0

    if abs(offset) > 0.01:
        warn("%s starts at %+.3f s rather than 0 -- listeners will be out by "
             "that much for the whole film", os.path.basename(dest), offset)
    suspect = bool(film_dur and abs(out_dur - film_dur) > 1.0)
    if suspect:
        warn("%s runs %s against the film's %s -- the timelines do not line "
             "up and phones will drift out of sync mid-film",
             os.path.basename(dest), hms(out_dur), hms(film_dur))
    return offset, suspect


def extract_phone_audio(movie, args, paths):
    """One AAC-LC m4a per language, so that everybody can hear the film.

    The projector has no speakers and the stick has no working audio output, so
    this is not an alternative way to listen -- it is the only one. Each person
    opens the web UI on their own phone, picks a language, and hears it in
    their own headphones while everybody watches the same silent picture.

    Encoded here rather than on the stick for the reason everything else in
    this file is: the stick is an Atom with about enough headroom to decode the
    film it is already decoding. It never transcodes audio, and ffmpeg is not
    installed on it.
    """
    out = []
    wanted = [lang.strip().lower() for lang in args.audio_langs.split(",")
              if lang.strip()]
    audio_rel_dir = os.path.join(WORK_DIR, "audio", movie["id"])
    seen = set()

    for stream in movie.get("audio_streams_info") or []:
        if len(out) >= args.max_audio_tracks or cancelled():
            break
        if not stream.get("channels"):
            continue                     # a data stream wearing an audio hat
        lang = (stream["tags"].get("language") or "und").lower()
        title = (stream["tags"].get("title") or "").strip()
        if wanted and lang not in wanted and lang != "und":
            continue
        # Deduplicated by language AND title, not by language alone. A film
        # with two English tracks usually has a reason -- the second one is the
        # director talking over it -- and dropping it would remove the one
        # thing somebody specifically wanted to listen to.
        key = (lang, title.lower())
        if key in seen:
            continue
        seen.add(key)

        rel = os.path.join(audio_rel_dir, "%d.%s.m4a" % (stream["n"], lang))
        dest = os.path.join(paths["output"], rel)
        record = {"n": stream["n"], "rel": rel, "lang": lang, "title": title,
                  "channels": stream.get("channels"),
                  "default": bool(stream.get("default"))}

        if os.path.isfile(dest) and not args.force:
            checked = check_phone_audio(dest, movie, args)
            if checked is not None:
                record["offset"], record["suspect"] = checked
                out.append(record)
                log("  = audio already present: %s", os.path.basename(dest),
                    level="verbose")
                continue

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        staging = dest + ".part"
        argv = build_phone_audio_argv(movie, stream, lang, staging, args)
        log("  > audio for %s: track %d (%s%s)", movie["title"], stream["n"],
            lang, ", " + title if title else "")
        log("    %s", " ".join(argv), level="verbose")
        argv, watching = with_progress(argv, "audio (%s)" % lang,
                                       movie.get("duration"))
        res = run(argv, timeout=args.phone_audio_timeout, progress=watching)
        if res.returncode != 0 or not os.path.isfile(staging):
            if not cancelled():
                warn("phone audio failed for %s track %d: %s", movie["title"],
                     stream["n"], first_line(res.stderr or ""))
            discard(staging)
            continue
        # Staged and renamed, like the transcode: the share is mounted on the
        # stick with actimeo=60, so a file written in place can be served at a
        # stale size for a minute afterwards -- and a wrong Content-Length is a
        # phone that stops halfway through a film.
        os.replace(staging, dest)

        checked = check_phone_audio(dest, movie, args)
        if checked is None:
            continue
        record["offset"], record["suspect"] = checked
        try:
            record["bytes"] = os.path.getsize(dest)
        except OSError:
            pass
        out.append(record)
        log("  + %s (%s)", os.path.basename(dest),
            human_size(record.get("bytes", 0)))

    movie["phone_audio"] = out
    return out


def language_from_name(path):
    """"film.en.srt" -> "en", "film.eng.forced.srt" -> "eng"."""
    parts = os.path.splitext(os.path.basename(path))[0].split(".")
    for part in reversed(parts[1:]):
        if part.lower() in ("forced", "sdh", "cc", "hi", "default"):
            continue
        if 2 <= len(part) <= 3 and part.isalpha():
            return part.lower()
    return None


def make_poster(movie, args, paths, tmdb=None):
    """One JPEG per film, in this order of preference: a poster the library
    already has, cover art embedded in the container, TMDb if it is switched
    on, and failing all of those a frame from a fifth of the way in.

    Doing it here rather than on the stick is most of the point of the script:
    playstick-web.py extracts these with mpv, one at a time, only while nothing
    is playing, over CIFS. A hundred films is an afternoon.
    """
    rel = os.path.join(WORK_DIR, "posters", "%s.jpg" % movie["id"])
    dest = os.path.join(paths["output"], rel)
    # A poster on disk is normally the end of the matter -- but a poster
    # downloaded for the wrong film is indistinguishable from the right one
    # here, and where the poster came from is not remembered between runs
    # (collect() snapshots the movie before this ever runs). So when a TMDb
    # match is available, --refresh-posters says to fetch it again rather than
    # trust what is already there.
    stale = args.refresh_posters and movie.get("poster_url")
    if os.path.isfile(dest) and not args.force and not stale:
        movie["poster_rel"] = rel
        log("  = poster already present: %s", os.path.basename(dest),
                            level="verbose")
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    scale = "scale=%d:-2:flags=lanczos" % args.poster_width
    
    # First try TMDB (official movie posters)
    url = movie.get("poster_url")
    if url and tmdb is not None:
        try:
            tmp = dest + ".part"
            log("  = downloading poster: %s", os.path.basename(tmp),
                                                    level="verbose")
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = resp.read()
            with open(tmp, "wb") as handle:
                handle.write(data)
            res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", tmp, "-vf", scale, "-q:v", "3", dest], timeout=60)
            os.unlink(tmp)
            if res.returncode == 0 and os.path.getsize(dest) > 0:
                movie["poster_rel"] = rel
                movie["poster_source"] = "tmdb"
                return True
        except (urllib.error.URLError, OSError) as exc:
            warn("poster download failed for %s: %s", movie["title"], exc)
    
    sidecar = find_poster_sidecar(movie["source"])
    if sidecar:
        res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", sidecar, "-vf", scale, "-q:v", "3", dest], timeout=60)
        if res.returncode == 0 and os.path.getsize(dest) > 0:
            movie["poster_rel"] = rel
            movie["poster_source"] = "sidecar"
            return True

    pic = movie.get("attached_pic")
    if pic is not None:
        res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
                   "-i", movie["source"], "-map", "0:%d" % pic, "-vf", scale,
                   "-q:v", "3", "-frames:v", "1", dest], timeout=90)
        if res.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            movie["poster_rel"] = rel
            movie["poster_source"] = "embedded"
            return True

    # A frame, from a fifth of the way in so the grid shows the film rather
    # than a distributor's logo. Same reasoning as PLAYSTICK_THUMB_AT.
    duration = movie.get("duration") or 0
    at = duration * (args.poster_at / 100.0) if duration else 60
    source = os.path.join(paths["output"], movie["media_rel"]) if movie.get("media_rel") else movie["source"]
    res = run([FFMPEG, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", "%.3f" % at, "-i", source, "-frames:v", "1",
               "-vf", scale, "-q:v", "3", dest], timeout=args.poster_timeout)
    if res.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
        movie["poster_rel"] = rel
        movie["poster_source"] = "frame"
        return True
    if not cancelled():
        warn("no poster for %s", movie["title"])
    # Unlinked either way: a zero-byte or half-written JPEG in the cache is
    # worse than none, because the next run finds it there and skips the film.
    discard(dest)
    return False


def find_poster_sidecar(path):
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    candidates = [os.path.join(directory, stem + ext) for ext in IMAGE_EXT]
    candidates += [os.path.join(directory, stem + "-poster" + ext) for ext in IMAGE_EXT]
    candidates += [os.path.join(directory, name) for name in SIDECAR_POSTERS]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return None


def find_external_subs(path):
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    out = []
    try:
        entries = os.listdir(directory)
    except OSError:
        return out
    for name in sorted(entries):
        if not name.lower().endswith(SUB_EXT):
            continue
        if name.lower().startswith(stem):
            out.append(os.path.join(directory, name))
    return out


# --- index -----------------------------------------------------------------

def write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False, sort_keys=False)
        handle.write("\n")
    os.replace(tmp, path)


def build_index(kept, dropped, args, paths):
    movies = []
    for movie in sorted(kept, key=lambda m: (m["sort_title"], m.get("year") or 0)):
        entry = {
            "id": movie["id"],
            "title": movie["title"],
            "sort_title": movie["sort_title"],
            # Relative to the library root, always. The daemon joins this onto
            # its own PLAYSTICK_LIBRARY and refuses anything that resolves
            # outside it -- the paths on this machine mean nothing on the stick,
            # where the same share is mounted at /srv/movies.
            "rel": (movie.get("media_rel") or movie["source_rel"]).replace(os.sep, "/"),
            "source_rel": movie["source_rel"].replace(os.sep, "/"),
        }
        for key, out_key in (("year", "year"), ("rating", "rating"),
                             ("rating_source", "rating_source"),
                             ("genres", "genres"), ("plot", "plot"),
                             ("duration", "duration"), ("width", "width"),
                             ("height", "height"), ("vcodec", "vcodec"),
                             ("acodec", "acodec"), ("metadata_source", "metadata_source"),
                             # Carried so a wrong match can be traced from the
                             # index straight back to the TMDb page it came
                             # from. Additive, and the daemon ignores it -- see
                             # the note on "audio" below.
                             ("tmdb_id", "tmdb_id"), ("tmdb_score", "tmdb_score"),
                             ("poster_source", "poster_source"),
                             ("verified", "verified"), ("prepared", "prepared")):
            if movie.get(key) not in (None, "", []):
                entry[out_key] = movie[key]
        if movie.get("poster_rel"):
            entry["poster"] = movie["poster_rel"].replace(os.sep, "/")
        if movie.get("subtitles"):
            entry["subtitles"] = [
                {"lang": s["lang"], "rel": s["rel"].replace(os.sep, "/")}
                for s in movie["subtitles"]
            ]
        if movie.get("phone_audio"):
            # Deliberately NOT behind a SCHEMA bump. playstick-web.py refuses an
            # index whose schema is newer than it understands and falls back to
            # walking the share -- which would cost posters, subtitles,
            # de-duplication and shelf ordering in order to deliver a feature
            # that daemon does not have anyway. An additive key is simply
            # ignored by an older daemon, so the worst case degrades to "no
            # sound on the phones" and nothing else.
            entry["audio"] = [
                {"n": t["n"], "rel": t["rel"].replace(os.sep, "/"),
                 "lang": t["lang"], "title": t.get("title") or "",
                 "channels": t.get("channels"),
                 "default": t.get("default", False),
                 "offset": t.get("offset", 0.0)}
                for t in movie["phone_audio"]
            ]
        if movie.get("verify_notes"):
            entry["notes"] = movie["verify_notes"]
        movies.append(entry)

    return {
        "schema": SCHEMA,
        "generator": "playstick-prep %s" % VERSION,
        "generated_at": int(time.time()),
        "generated_from": paths["library"],
        "count": len(movies),
        "excluded": [
            {
                "source_rel": m["source_rel"].replace(os.sep, "/"),
                "reason": m.get("duplicate_reason") or "; ".join(m.get("verify_notes") or []),
                "duplicate_of": m.get("duplicate_of"),
            }
            for m in dropped
        ],
        "movies": movies,
    }


def publish_index(kept, excluded, args, paths, last=None):
    """Write the index out, and return (index, payload-to-compare-next-time).

    Called after every film rather than once at the end. A first run over a few
    hundred films is most of a day, and until the index exists the daemon has
    nothing to read but the share itself -- so the whole library stays slow, and
    posterless, until the last encode finishes. Written as it goes, the grid
    fills in film by film while the run continues, and a machine that loses
    power at hour six has an index for everything up to hour six rather than
    none at all.

    A partial index is not a broken one. A film that has not been reached yet is
    listed against its original file with no poster and no phone audio, which is
    exactly what an unprepared film looks like anyway -- the same state the run
    leaves behind when it is stopped with Ctrl-C. The daemon re-reads the whole
    file every scan interval regardless of mtime, so each rewrite costs it
    nothing and is picked up without being asked.

    The comparison against the previous write is what keeps this cheap: a re-run
    over an already-prepared library would otherwise rewrite an identical index
    once per film, over CIFS. generated_at is excluded from it, because it
    changes on every build and would defeat the whole check.
    """
    index = build_index(kept, excluded, args, paths)
    body = {key: value for key, value in index.items() if key != "generated_at"}
    if body == last:
        return index, last
    write_json_atomic(os.path.join(paths["output"], INDEX_NAME), index)
    return index, body


# --- the run ---------------------------------------------------------------

def collect(path, library, args, state):
    """Everything that can be learned about one file without encoding it."""
    rel = os.path.relpath(path, library)
    try:
        stat = os.stat(path)
    except OSError as exc:
        warn("cannot stat %s: %s", rel, exc)
        return None

    cached = state.get(rel)
    fingerprint = [int(stat.st_size), int(stat.st_mtime)]
    if (cached and not args.force and cached.get("fingerprint") == fingerprint
            and cached.get("verify_mode") == args.verify
            # A cache written before this tool learned about phone audio has no
            # audio_streams_info, and every one of those entries would sail
            # past the fingerprint check and extract nothing -- silently, on a
            # library that looks fully prepared. Re-probe instead. It costs one
            # ffprobe per film, once.
            and "audio_streams_info" in (cached.get("movie") or {})
            # And the same argument for the rules that decide what a film is
            # called and when it came out. Those live in this file, not in the
            # library, so a fix to them reaches a prepared library only if the
            # entries written under the old rules are re-derived.
            and cached.get("meta_version") == META_VERSION):
        movie = dict(cached["movie"])
        movie["source"] = path
        log("  . cached: %s", rel, level="verbose")
        return movie

    log("  probing %s", rel, level="verbose")
    info, error = ffprobe_json(path, timeout=args.probe_timeout)
    if info is None:
        warn("unreadable: %s (%s)", rel, error)
        return {"source": path, "source_rel": rel, "unreadable": True,
                "verify_notes": ["ffprobe could not read it: %s" % error],
                "title": clean_title(os.path.basename(path)),
                "norm_title": normalize_title(clean_title(os.path.basename(path))),
                "sort_title": "", "id": file_id(rel), "size": stat.st_size}

    vid = video_stream(info) or {}
    audio = audio_streams(info)
    filename = os.path.basename(path)

    movie = {
        "source": path,
        "source_rel": rel,
        "id": file_id(rel),
        "size": stat.st_size,
        "duration": round(duration_of(info), 3),
        "width": int(to_float(vid.get("width"))) or None,
        "height": int(to_float(vid.get("height"))) or None,
        "vcodec": vid.get("codec_name"),
        "profile": vid.get("profile"),
        "pix_fmt": vid.get("pix_fmt"),
        "eight_bit": is_8bit_420(vid),
        "fps": round(frame_rate(vid), 3),
        "bitrate": round(bit_rate(info, vid)),
        "acodec": audio[0].get("codec_name") if audio else None,
        "audio_index": audio[0].get("index") if audio else None,
        # Trimmed rather than kept whole: this ends up in the state cache, and
        # a full ffprobe stream object is a few hundred bytes of side data per
        # subtitle track that nothing here ever reads.
        "subtitle_streams": [
            {"index": s.get("index"),
             "codec_name": s.get("codec_name"),
             "tags": {"language": (s.get("tags") or {}).get("language", "und")}}
            for s in subtitle_streams(info)
        ],
        # Trimmed for the same reason, and note the "n": it is the position
        # among the AUDIO streams, not the absolute stream index. That is what
        # -map 0:a:<n> takes, what the URL a phone requests carries, and what
        # the daemon indexes its list by -- so an ffmpeg stream index never
        # leaves this file.
        "audio_streams_info": [
            {"n": n,
             "index": s.get("index"),
             "codec_name": s.get("codec_name"),
             "channels": int(to_float(s.get("channels"))) or None,
             "start_time": round(to_float(s.get("start_time")), 3),
             "default": bool((s.get("disposition") or {}).get("default")),
             "tags": {"language": (s.get("tags") or {}).get("language", "und"),
                      "title": (s.get("tags") or {}).get("title", "")}}
            for n, s in enumerate(audio)
        ],
        "attached_pic": attached_pic_index(info),
        "external_subs": find_external_subs(path),
        "nfo": find_nfo(path),
    }

    ok, level, reasons = verify_file(path, info, args)
    movie["verified_ok"] = ok
    movie["verified"] = level
    movie["verify_notes"] = reasons

    # Title and year: the filename first because it is what the daemon would
    # have used, then anything better that the library actually knows.
    movie["title"] = clean_title(filename)
    movie["year"] = year_from_path(rel)
    tags_meta = metadata_from_tags(info)
    apply_metadata(movie, tags_meta, args)
    if movie["nfo"]:
        apply_metadata(movie, parse_nfo(movie["nfo"]), args)

    movie["norm_title"] = normalize_title(movie["title"])
    movie["sort_title"] = movie["norm_title"]
    movie["signature"] = quick_signature(path, stat.st_size) if args.dedupe else None

    state[rel] = {"fingerprint": fingerprint, "verify_mode": args.verify,
                  "meta_version": META_VERSION,
                  "movie": {k: v for k, v in movie.items() if k != "source"}}
    return movie


def apply_metadata(movie, meta, args):
    """Later sources win, with two deliberate exceptions -- see below."""
    if not meta:
        return
    source = meta.get("metadata_source")
    for key in ("rating", "genres", "plot", "expected_runtime",
                "poster_url", "tmdb_id", "tmdb_score", "rating_votes"):
        if meta.get(key) not in (None, "", []):
            movie[key] = meta[key]

    # Exception one: a container tag may not overwrite a year the filename
    # already gave. "Later sources win" is right for an .nfo, which somebody
    # curated, and for a TMDb match, which is now verified -- but a muxer's
    # idea of a year is whatever it happened to write, and the folder a human
    # named "Hook (1991)" is better evidence than that. This only fills a gap.
    year = meta.get("year")
    if year and not (source == "tags" and movie.get("year")):
        movie["year"] = year

    if meta.get("rating") is not None:
        movie["rating_source"] = source
    if source:
        movie["metadata_source"] = source

    # Exception two: the title. An .nfo title is curated and a TMDb title has
    # been checked against this film before it got here, so both simply win. A
    # container tag has to earn it -- see title_is_release_string() for what
    # "Re-Encode by Bsgr13. Enjoy with SPIDERMAN 3 2007 !" did with the old
    # rule, which was that the longer string wins.
    title = meta.get("title")
    if not title:
        return
    if args.prefer_metadata_titles or source in ("nfo", "tmdb"):
        movie["title"] = title
    elif (len(title) > len(movie.get("title", ""))
            and not title_is_release_string(title)):
        movie["title"] = title


def file_id(rel):
    """The same identifier playstick-web.py computes when it walks the share,
    so a thumbnail cached under one scheme is still the right thumbnail under
    the other -- and so switching the index on and off does not renumber
    everything a phone has open."""
    return hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:16]


def prepare_one(movie, args, paths, tmdb):
    """Everything expensive for one film, in the order that leaves the most
    behind if it is stopped: the encode first, then the audio people need to
    hear it, then subtitles, then the poster."""
    needed, reasons = transcode_plan(movie, args)
    movie["transcode_reasons"] = reasons
    if needed and not args.dry_run:
        if not do_transcode(movie, args, paths) and not cancelled():
            movie["prepare_failed"] = True
    elif needed:
        log("  would encode %s (%s)", movie["title"], ", ".join(reasons))
    if args.dry_run:
        return movie
    if not args.no_phone_audio and not cancelled():
        extract_phone_audio(movie, args, paths)
    if not cancelled():
        extract_subtitles(movie, args, paths)
    if not args.no_posters and not cancelled():
        make_poster(movie, args, paths, tmdb)
    return movie


def main(argv=None):
    global _verbosity, _progress_mode
    args = parse_args(argv)
    _verbosity = 0 if args.quiet else (2 if args.verbose else 1)
    _progress_mode = args.progress
    install_signal_handlers()

    for tool in (FFMPEG, FFPROBE):
        if shutil.which(tool) is None:
            die("%s not found on PATH. This script runs on the developer "
                "machine, where ffmpeg is expected; the stick deliberately "
                "does not have it.", tool)

    library = os.path.abspath(os.path.expanduser(args.library))
    if not os.path.isdir(library):
        die("--library %s is not a directory", library)
    output = os.path.abspath(os.path.expanduser(args.output or library))
    paths = {"library": library, "output": output}

    if not args.dry_run:
        os.makedirs(os.path.join(output, WORK_DIR), exist_ok=True)

    state_path = os.path.join(output, WORK_DIR, STATE_NAME)
    state = {}
    if os.path.isfile(state_path) and not args.force:
        try:
            state = json.load(open(state_path, "r", encoding="utf-8"))
        except (OSError, ValueError):
            state = {}

    log("playstick-prep %s", VERSION)
    log("library %s", library)
    if output != library:
        log("output  %s", output)

    started = time.time()
    files, episodic = discover(library, args)
    if episodic:
        log("skipped %d episodic file(s) or season folder(s) -- "
            "--allow-episodes keeps them", len(episodic))
        for rel in episodic:
            log("  tv: %s", rel, level="verbose")
    if not files:
        die("no video files found under %s", library)
    log("found %d candidate file(s)", len(files))

    movies = []
    for index, path in enumerate(files, 1):
        if cancelled():
            break
        log("[%d/%d] %s", index, len(files), os.path.relpath(path, library))
        movie = collect(path, library, args, state)
        if movie:
            movies.append(movie)
    # Whatever was probed is worth keeping even on the way out: probing is the
    # slow part of a re-run, and the cache is what makes stopping cheap.
    probed_all = not cancelled()
    if not args.dry_run:
        try:
            write_json_atomic(state_path, state)
        except OSError as exc:
            warn("could not write the state cache: %s", exc)
    if not probed_all:
        log("")
        log("stopped after probing %d of %d file(s); the index was left alone",
            len(movies), len(files))
        log("nothing was lost -- run the same command again to carry on")
        return 130

    rejected = [m for m in movies if m.get("unreadable") or not m.get("verified_ok", True)]
    rejected_rels = {m["source_rel"] for m in rejected}
    usable = [m for m in movies if m["source_rel"] not in rejected_rels]
    for movie in rejected:
        warn("rejected %s -- %s", movie["source_rel"],
             "; ".join(movie.get("verify_notes") or ["unknown"]))

    if args.dedupe:
        kept, dropped = pick_duplicates(usable, args)
    else:
        kept, dropped = usable, []
    for movie in dropped:
        log("duplicate: %s\n           -> keeping %s (%s)", movie["source_rel"],
            movie.get("duplicate_of"), movie.get("duplicate_reason"))
    if args.duplicates_dir and dropped and not args.dry_run:
        move_duplicates(dropped, args, library)

    tmdb = None
    if args.tmdb_key:
        tmdb = Tmdb(args.tmdb_key, os.path.join(output, WORK_DIR, "tmdb-cache.json"),
                    args.tmdb_language)
        log("looking up %d film(s) on TMDb", len(kept))
        unmatched = []
        for movie in kept:
            if cancelled():
                break
            meta, why = tmdb.lookup(movie["title"], movie.get("year"),
                                    movie.get("duration"))
            if why:
                unmatched.append((movie, why))
                log("  ? %s (%s): %s", movie["title"], movie.get("year") or "?",
                    why, level="verbose")
            apply_metadata(movie, meta, args)
            # TMDb knows how long the film should be, which is the one
            # genuinely external check on "is this the whole thing". Both
            # directions matter: short means cut or incomplete, and long means
            # an extended edition -- or, before the match was verified, simply
            # a different film.
            expected = movie.get("expected_runtime")
            duration = movie.get("duration")
            if expected and duration and duration < expected * 0.85:
                note = ("runs %s against TMDb's %s -- cut, or incomplete"
                        % (hms(duration), hms(expected)))
                movie.setdefault("verify_notes", []).append(note)
                warn("%s: %s", movie["title"], note)
            elif expected and duration and duration > expected * 1.15:
                note = ("runs %s against TMDb's %s -- an extended edition, or "
                        "the wrong match" % (hms(duration), hms(expected)))
                movie.setdefault("verify_notes", []).append(note)
                warn("%s: %s", movie["title"], note)
        tmdb.save()
        # Said once, together, at the end. A film with no confident match keeps
        # its filename title and year and gets a frame for a poster, which is
        # what an unmatched film has always looked like -- but it is worth
        # knowing which ones they are, because the usual cause is a typo in the
        # filename ("Contac.1997...") and renaming the file fixes it.
        log("TMDb: %d matched, %d not confident enough to use",
            len(kept) - len(unmatched), len(unmatched))
        for movie, why in unmatched:
            log("  no match: %s -- %s", movie["source_rel"], why)

    # One film at a time, deliberately. libx264 already spreads a single encode
    # across every core, so a second concurrent one mostly costs cache and
    # makes both slower; two encodes reading 20 GB sources over the same NAS
    # mount contend for the network rather than the CPU; and the log becomes
    # two films interleaved line by line, which is unreadable exactly when you
    # are trying to work out which one went wrong. Serial also means Ctrl-C
    # stops at a film boundary you can name, instead of abandoning a pool of
    # worker threads that Python will wait for anyway.
    excluded = dropped + rejected + [
        {"source_rel": rel, "verify_notes": ["episodic television"]}
        for rel in episodic
    ]
    index_path = os.path.join(output, INDEX_NAME)

    log("preparing %d film(s), one at a time", len(kept))
    prepared = 0
    published = None
    for number, movie in enumerate(kept, 1):
        if cancelled():
            break
        log("[%d/%d] %s", number, len(kept), movie["title"])
        prepare_one(movie, args, paths, tmdb)
        if cancelled():
            break       # this one was stopped part way; it does not count
        prepared += 1
        if not args.dry_run:
            # Republished here, with this film's poster, audio and transcode in
            # it, so the shelf grows while the rest of the run carries on.
            try:
                _, published = publish_index(kept, excluded, args, paths, published)
            except OSError as exc:
                # Not fatal. The films already prepared are on disk either way,
                # and the write at the end of the run gets another go at it.
                warn("could not write the index: %s", exc)
            else:
                log("   published index")

    stopped = cancelled()
    if args.dry_run:
        index = build_index(kept, excluded, args, paths)
        log("dry run: would write %s with %d film(s)", index_path, len(index["movies"]))
    else:
        index, published = publish_index(kept, excluded, args, paths, published)
        try:
            write_json_atomic(state_path, state)
        except OSError:
            pass

    failed = [m for m in kept if m.get("prepare_failed")]
    log("")
    log("%d film(s) indexed, %d duplicate(s) dropped, %d rejected, "
        "%d episodic, %d encode(s) failed",
        len(index["movies"]), len(dropped), len(rejected), len(episodic),
        len(failed))
    log("%s in %s", index_path if not args.dry_run else "(dry run)",
        hms(time.time() - started))
    if stopped:
        # The index is still written and still correct: every film that was
        # collected is in it, and the ones that were not prepared simply have
        # no poster, no phone audio and their original file as "rel" -- which
        # is what an unprepared film looks like anyway. The daemon can serve
        # this. Re-running fills in the rest and skips everything already done.
        log("stopped after preparing %d of %d film(s); run again to finish "
            "the remaining %d", prepared, len(kept), len(kept) - prepared)
        return 130
    if failed:
        return 1
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="playstick-prep",
        description="Prepare a movie library for the playstick appliance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog="Runs on the developer machine. Never modifies the source films.")

    parser.add_argument("--library", required=True,
                        help="the movie library to read (the NAS share)")
    parser.add_argument("--output",
                        help="where to write the index and .playstick/ "
                             "(default: the library itself)")

    scan = parser.add_argument_group("what counts as a film")
    scan.add_argument("--max-depth", type=int, default=3,
                      help="directories to descend into")
    scan.add_argument("--min-size-mb", type=int, default=100,
                      help="ignore files smaller than this")
    scan.add_argument("--min-duration", type=float, default=1200,
                      help="seconds; below this it is a trailer or an extra, "
                           "not a film")
    scan.add_argument("--skip-pattern",
                      default=r"sample|trailer|extras?\b|behind[ ._-]the[ ._-]scenes",
                      help="filenames matching this are not films")
    scan.add_argument("--allow-episodes", dest="skip_episodes",
                      action="store_false",
                      help="index episodic television too. By default S01E02, "
                           "1x02, 'Season 2' anywhere in the path and "
                           "'Part 1 of 6' are skipped: --min-duration does not "
                           "catch a 45-minute episode, and a season turns the "
                           "grid into a wall of near-identical tiles")

    check = parser.add_argument_group("verification")
    check.add_argument("--verify", choices=("none", "probe", "quick", "full"),
                       default="quick",
                       help="probe: header only. quick: decode the first and "
                            "last few seconds, which is what catches a partial "
                            "download. full: decode everything, slowly")
    check.add_argument("--allow-truncated", action="store_true",
                       help="index files that are missing their ending anyway")
    check.add_argument("--probe-timeout", type=int, default=120)
    check.add_argument("--decode-timeout", type=int, default=300)
    check.add_argument("--full-decode-timeout", type=int, default=7200)

    dedup = parser.add_argument_group("de-duplication")
    dedup.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                       help="index every copy")
    dedup.add_argument("--duplicates-dir",
                       help="MOVE losing copies here instead of only reporting "
                            "them. Never deletes")

    enc = parser.add_argument_group("transcoding")
    enc.add_argument("--transcode", choices=("auto", "never", "always"),
                     default="auto",
                     help="auto re-encodes only what the stick cannot play well")
    enc.add_argument("--width", type=int, default=1280)
    enc.add_argument("--height", type=int, default=720)
    enc.add_argument("--max-fps", type=int, default=30)
    enc.add_argument("--max-bitrate", type=float, default=6e6,
                     help="bits per second; the ceiling is the CIFS read over "
                          "Wi-Fi, not the decoder")
    enc.add_argument("--crf", type=int, default=20)
    enc.add_argument("--preset", default="medium",
                     help="libx264 preset: slower is smaller, not better here")
    enc.add_argument("--audio-bitrate", default="160k")
    enc.add_argument("--transcode-timeout", type=int, default=6 * 3600)
    # There is no --jobs. Films are prepared one at a time; see the comment on
    # the loop in main() for why more is slower here rather than faster.

    extra = parser.add_argument_group("posters, subtitles and metadata")
    extra.add_argument("--no-posters", action="store_true", default=False)
    extra.add_argument("--refresh-posters", action="store_true",
                       help="re-download the poster for every film that has a "
                            "TMDb match, instead of keeping the one already on "
                            "disk. Run this once after upgrading: a poster "
                            "fetched for a wrong match looks exactly like a "
                            "right one from here")
    extra.add_argument("--poster-width", type=int, default=400)
    extra.add_argument("--poster-at", type=float, default=20,
                       help="percent into the film to grab a frame from")
    extra.add_argument("--poster-timeout", type=int, default=180)
    extra.add_argument("--sub-langs", default="eng,en,und",
                       help="comma-separated language codes to keep")
    extra.add_argument("--max-subs", type=int, default=4)
    extra.add_argument("--sub-charenc", default="UTF-8",
                       help="encoding to assume for external subtitle files")
    extra.add_argument("--subtitle-timeout", type=int, default=600)
    # The projector is silent and the stick has no working audio output, so
    # these are not an extra: they are how anybody hears the film at all.
    extra.add_argument("--no-phone-audio", action="store_true",
                       help="do not extract per-language audio for phones")
    extra.add_argument("--audio-langs", default="",
                       help="comma-separated language codes to keep; empty "
                            "means every track the film has")
    extra.add_argument("--max-audio-tracks", type=int, default=4)
    extra.add_argument("--phone-audio-bitrate", default="96k",
                       help="AAC-LC stereo. The ceiling here is the stick's "
                            "one Wi-Fi radio, which is already carrying the "
                            "film in the other direction")
    extra.add_argument("--phone-audio-downmix", choices=("dialogue", "ffmpeg"),
                       default="dialogue",
                       help="dialogue lifts the centre channel; ffmpeg's "
                            "default matrix loses speech on headphones")
    extra.add_argument("--phone-audio-timeout", type=int, default=1800)
    extra.add_argument("--prefer-metadata-titles", action="store_true",
                       help="trust .nfo and container titles over the filename")
    extra.add_argument("--tmdb-key",
                       help="enable TMDb lookups for ratings, genres and "
                            "posters. THIS SENDS YOUR FILM TITLES TO "
                            "themoviedb.org")
    extra.add_argument("--tmdb-language", default="en-US")

    run_group = parser.add_argument_group("how to run")
    run_group.add_argument("--force", action="store_true",
                           help="ignore the cache and redo everything")
    run_group.add_argument("--dry-run", action="store_true",
                           help="say what would happen and write nothing")
    run_group.add_argument("--progress", choices=("auto", "always", "never"),
                           default="auto",
                           help="show how far through each encode ffmpeg is. "
                                "auto redraws one line on a terminal and prints "
                                "a line a minute when this is redirected to a "
                                "log; always redraws either way")
    run_group.add_argument("-q", "--quiet", action="store_true")
    run_group.add_argument("-v", "--verbose", action="store_true")

    return parser.parse_args(argv)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # Only reachable before install_signal_handlers() runs, or after a
        # second Ctrl-C has restored the default handler.
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
