#!/usr/bin/env python3
"""Movie player for the UxPlay appliance: a phone-sized web UI on the LAN that
plays files from the NAS share straight to a DRM plane, with no compositor.

WHY THE DAEMON ARBITRATES THE DISPLAY INSTEAD OF SYSTEMD

Exactly one process may hold DRM master, and uxplay-kms.service takes it when
the *service* starts, not when a client connects -- see the header of
uxplay-idle-clock.py, where the same fact is what forces the idle clock to
write pixels rather than text. So mpv cannot share the card with UxPlay: the
AirPlay receiver has to be stopped for the length of a film and started again
afterwards.

That could be expressed as Conflicts= on an mpv unit, and it was written that
way first. It does not work, because systemd will happily stop uxplay-kms for
the conflict and then has no reason to ever start it again -- a film that ends
at 21:30 leaves the projector with no AirPlay receiver until somebody notices.
Restoring it is a decision, and decisions live here.

One consequence worth knowing rather than debugging: while a film is playing
the device disappears from the AirPlay list entirely, because UxPlay is what
publishes the mDNS record. It comes back a few seconds after the film stops.

The interlock in the other direction is a check, not a race: a film will not
start while a client holds a TCP connection to UxPlay's port. Mirroring wins,
because somebody is standing there holding a phone.

WHAT RUNS AS ROOT AND WHY

All of it. mpv needs to become DRM master, and the arbitration above needs to
start and stop a system unit. The alternative -- a dedicated user in video and
render plus a polkit rule for two systemctl verbs -- is more moving parts than
the thing it protects on a single-purpose appliance with one service on it.
The unit is confined with ProtectSystem=strict instead.

The control that does matter is that no filesystem path ever crosses the HTTP
boundary. The client sends an opaque id which indexes a table this process
built by walking the share; there is no endpoint that takes a path and no
endpoint that streams file bytes.

Configuration is entirely environmental -- see playstick-web.service.
"""

import errno
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

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


# --- the AirPlay interlock -------------------------------------------------

def airplay_active():
    """True while a client holds a TCP connection to UxPlay.

    The same question, asked the same way, as session_active() in
    uxplay-idle-clock.py -- and deliberately a second copy rather than a shared
    module, because the two callers do not want the same answer. The clock is
    driving a blank countdown and can afford to be wrong for one poll;
    airplay_confirmed() below cannot, so it debounces and the clock does not.
    Eight duplicated lines is a smaller thing than an import path shared
    between two units that then have to agree about sampling.

    Not 'is the DRM node open': uxplay-kms holds that for its whole lifetime
    and so can never distinguish idle from mirroring.
    """
    if not AIRPLAY_PORT:
        return False
    try:
        out = subprocess.run(
            ["ss", "-H", "-tn", "state", "established", "sport = :%s" % AIRPLAY_PORT],
            capture_output=True, text=True, timeout=5).stdout
        return bool(out.strip())
    except Exception:                                # noqa: BLE001
        return False


def airplay_confirmed(samples=2, interval=1.0):
    """Debounced, for the one caller that refuses to do something.

    iOS opens short-lived connections to UxPlay's port merely from having the
    AirPlay picker on screen -- nobody is mirroring, the list is just being
    drawn. A single ss sample would therefore refuse to start a film because
    somebody across the room glanced at a menu. Requiring the connection to
    survive consecutive samples distinguishes a session from a look.
    """
    for i in range(max(1, samples)):
        if not airplay_active():
            return False
        if i + 1 < samples:
            time.sleep(interval)
    return True


def systemctl(*args, timeout=45):
    try:
        return subprocess.run(["systemctl", *args], capture_output=True,
                              text=True, timeout=timeout)
    except Exception as exc:                         # noqa: BLE001
        log("systemctl %s failed: %s", " ".join(args), exc)
        return None


def unit_active(unit):
    res = systemctl("is-active", "--quiet", unit, timeout=10)
    return bool(res) and res.returncode == 0


# --- library ---------------------------------------------------------------

def clean_title(filename):
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


def find_sidecar(path):
    """A poster the collection already carries, if there is one."""
    directory = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    candidates = [os.path.join(directory, stem + ext) for ext in (".jpg", ".png")]
    candidates += [os.path.join(directory, name) for name in SIDECAR_NAMES]
    for cand in candidates:
        try:
            if os.path.isfile(cand):
                return cand
        except OSError:
            continue
    return None


class Library:
    """The movie list, cached because CIFS stat over this box's SDIO-attached
    Wi-Fi is what makes a rescan slow -- not the walk, the per-file stat."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}
        self._order = []
        self._available = False
        self._scanned_at = 0.0
        self._error = ""
        self._wake = threading.Event()

    def snapshot(self):
        with self._lock:
            return (list(self._order), dict(self._items), self._available,
                    self._scanned_at, self._error)

    def get(self, ident):
        with self._lock:
            return self._items.get(ident)

    def request_rescan(self):
        self._wake.set()

    def run(self, stopping):
        while not stopping.is_set():
            self._scan()
            self._wake.wait(SCAN_INTERVAL)
            self._wake.clear()

    def _scan(self):
        items = {}
        order = []
        error = ""
        try:
            # os.walk swallows errors by default, which would report an
            # unreachable NAS as an empty library. onerror re-raises so the
            # UI can say "unavailable" instead of "you own no films".
            def boom(exc):
                raise exc

            for dirpath, dirnames, filenames in os.walk(LIBRARY, onerror=boom):
                rel_dir = os.path.relpath(dirpath, LIBRARY)
                depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
                if depth >= SCAN_DEPTH:
                    dirnames[:] = []
                dirnames[:] = [d for d in dirnames
                               if not d.startswith(".") and d not in SKIP_DIRS]
                for name in sorted(filenames):
                    if not name.lower().endswith(EXTENSIONS) or name.startswith("."):
                        continue
                    if SKIP_RE and SKIP_RE.search(name):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(path)
                    except OSError:
                        continue
                    if MIN_SIZE and size < MIN_SIZE:
                        continue
                    rel = os.path.relpath(path, LIBRARY)
                    ident = hashlib.sha1(rel.encode("utf-8", "replace")).hexdigest()[:16]
                    items[ident] = {
                        "id": ident,
                        "title": clean_title(name),
                        "path": path,
                        "sidecar": find_sidecar(path),
                    }
                    order.append(ident)
            available = True
        except OSError as exc:
            available = False
            error = os.strerror(exc.errno) if exc.errno else str(exc)
            log("library unavailable: %s", error)
        except Exception as exc:                     # noqa: BLE001
            available = False
            error = str(exc)
            log("library scan failed: %s", error)

        if available:
            order.sort(key=lambda i: items[i]["title"].lower())

        with self._lock:
            # A failed scan keeps the previous list rather than blanking the
            # grid: the NAS being briefly unreachable should not make a child's
            # films appear to have been deleted.
            if available:
                self._items = items
                self._order = order
            self._available = available
            self._scanned_at = time.time()
            self._error = error
        if available:
            log("library: %d films under %s", len(order), LIBRARY)


# --- thumbnails ------------------------------------------------------------

class Thumbs:
    """One worker, one file at a time, and nothing at all while a film plays.

    mpv does the frame extraction, so ffmpeg never has to be installed: this
    SoC has no CPU to spare and the eMMC has no room for a second decoder.
    """

    def __init__(self, library, player):
        self._library = library
        self._player = player
        self._lock = threading.Lock()
        self._queue = []
        self._queued = set()
        self._failed = set()
        self._cond = threading.Condition(self._lock)

    @staticmethod
    def cached_path(ident):
        return os.path.join(THUMB_DIR, ident + ".jpg")

    def have(self, ident):
        return os.path.isfile(self.cached_path(ident))

    def request(self, ident):
        with self._cond:
            if ident in self._queued or ident in self._failed:
                return
            self._queued.add(ident)
            self._queue.append(ident)
            self._cond.notify()

    def pending(self):
        with self._cond:
            return len(self._queue)

    def run(self, stopping):
        while not stopping.is_set():
            with self._cond:
                while not self._queue and not stopping.is_set():
                    self._cond.wait(2.0)
                if stopping.is_set():
                    return
                ident = self._queue.pop(0)
                self._queued.discard(ident)

            # Never compete with playback for the four cores that decode it.
            while self._player.state() != "idle" and not stopping.is_set():
                time.sleep(2.0)
            if stopping.is_set():
                return

            item = self._library.get(ident)
            if not item or self.have(ident):
                continue
            if not self._extract(ident, item):
                with self._cond:
                    self._failed.add(ident)

    def _extract(self, ident, item):
        source = item["sidecar"] or item["path"]
        is_still = source is item["sidecar"]
        staging = os.path.join(THUMB_DIR, ".work-" + ident)
        shutil.rmtree(staging, ignore_errors=True)
        try:
            os.makedirs(staging, exist_ok=True)
            # nice/ionice because an extraction that was already running when a
            # film started must lose to it. The queue is gated on the player
            # being idle, but the gate is checked before the subprocess starts
            # and a seek into a 20 GB file over CIFS can outlast the check.
            argv = ["nice", "-n", "19", "ionice", "-c", "3",
                    MPV, *THUMB_ARGS, "--vo-image-outdir=" + staging]
            if not is_still:
                # A frame from a fifth of the way in, so the grid shows the
                # film rather than a distributor's logo.
                argv.append("--start=" + THUMB_AT)
            argv.append(source)
            res = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=THUMB_TIMEOUT)
            produced = sorted(os.listdir(staging)) if os.path.isdir(staging) else []
            if not produced:
                log("thumbnail failed for %s (rc=%d) %s", item["title"],
                    res.returncode, (res.stderr or "").strip()[:200])
                return False
            os.replace(os.path.join(staging, produced[0]), self.cached_path(ident))
            return True
        except subprocess.TimeoutExpired:
            log("thumbnail timed out for %s", item["title"])
            return False
        except OSError as exc:
            log("thumbnail error for %s: %s", item["title"], exc)
            return False
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def placeholder_svg(title):
    """A tile that never makes the grid wait. Served instead of a JPEG until
    the real frame has been extracted -- encoding a JPEG here would mean a
    third-party imaging library, and this process has no dependencies."""
    letter = (title.strip()[:1] or "?").upper()
    letter = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}.get(letter, letter)
    hue = int(hashlib.sha1(title.encode("utf-8", "replace")).hexdigest()[:4], 16) % 360
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300">'
        '<rect width="200" height="300" fill="hsl(%d,32%%,26%%)"/>'
        '<text x="100" y="180" font-family="sans-serif" font-size="120" '
        'font-weight="700" fill="hsl(%d,40%%,70%%)" text-anchor="middle">%s</text>'
        "</svg>" % (hue, hue, letter)
    ).encode("utf-8")


# --- playback --------------------------------------------------------------

class Busy(Exception):
    """Refused: something else owns the projector."""


class Player:
    def __init__(self):
        self._lock = threading.RLock()
        self._proc = None
        self._item = None
        self._sock = None
        self._file = None
        self._request_id = 0
        self._ipc_lock = threading.Lock()
        self._restore_airplay = False
        self._status_cache = (0.0, {})

    # -- state

    def state(self):
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return "idle"
        # Deliberately outside the lock, and deliberately via status() rather
        # than the cached dict: reading the cache directly meant a state
        # computed before the cache was refreshed, so the first poll after a
        # pause still answered "playing" and the page drew the wrong icon.
        return "paused" if self.status().get("paused") else "playing"

    def current_title(self):
        with self._lock:
            return self._item["title"] if self._item else ""

    # -- lifecycle

    def start(self, item):
        with self._lock:
            if self.state() != "idle":
                raise Busy("A film is already playing.")
            if airplay_confirmed():
                raise Busy("The projector is being used for AirPlay.")

            # A share can contain symlinks. Nothing outside the library is
            # playable, whatever the share says.
            real = os.path.realpath(item["path"])
            root = os.path.realpath(LIBRARY)
            if not (real == root or real.startswith(root + os.sep)):
                raise Busy("That file is not in the library.")

            # Only ever restore what we took away. The repo installs
            # uxplay-kms.service without enabling it -- which output path wins
            # is a probe result -- so an operator may deliberately be running
            # neither, and starting one here would be this daemon inventing a
            # configuration nobody chose.
            self._restore_airplay = unit_active(AIRPLAY_UNIT)
            if self._restore_airplay:
                log("stopping %s to take DRM master", AIRPLAY_UNIT)
                self._write_flag(RESTORE_FILE, AIRPLAY_UNIT)
                systemctl("stop", AIRPLAY_UNIT)
                deadline = time.time() + 20
                while unit_active(AIRPLAY_UNIT) and time.time() < deadline:
                    time.sleep(0.25)
                # The card is released when the process exits and the kernel
                # closes its fd; give that a moment before mpv reaches for it.
                time.sleep(SETTLE_SECONDS)

            self._write_flag(BUSY_FILE, str(os.getpid()))
            try:
                os.unlink(MPV_SOCKET)
            except OSError:
                pass

            argv = [MPV, *MPV_ARGS, "--input-ipc-server=" + MPV_SOCKET, "--", item["path"]]
            log("playing %s", item["title"])
            log("  %s", " ".join(shlex.quote(a) for a in argv))
            tty = self._open_tty()
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdin=tty if tty is not None else subprocess.DEVNULL,
                    stdout=tty if tty is not None else subprocess.DEVNULL,
                    stderr=None,
                    start_new_session=False)
            finally:
                if tty is not None:
                    os.close(tty)
            self._item = item
            self._status_cache = (0.0, {})

            if not self._connect_ipc():
                log("mpv did not open its IPC socket; stopping")
                self._teardown()
                raise Busy("The player would not start.")

            threading.Thread(target=self._reap, daemon=True).start()

    @staticmethod
    def _open_tty():
        """An fd on tty1 for mpv's stdin/stdout, or None.

        mpv's DRM backend sets up a VT switcher so the console can be taken
        back with chvt, and it needs a terminal to do it on. Without one it
        warns and carries on -- usually. `--input-terminal=no` in the argv is
        what stops it putting that terminal into raw mode and reacting to
        stray keystrokes on a console nobody is typing at.

        getty@tty1 is masked by claim_tty1, and the idle clock only writes
        pixels, so nothing else wants this device.
        """
        if not MPV_TTY:
            return None
        try:
            return os.open(MPV_TTY, os.O_RDWR | os.O_NOCTTY)
        except OSError as exc:
            log("no tty for mpv (%s): %s -- carrying on without one", MPV_TTY, exc)
            return None

    def _connect_ipc(self):
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._proc.poll() is not None:
                return False
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect(MPV_SOCKET)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    log("IPC connect: %s", exc)
                time.sleep(0.2)
                continue
            self._sock = sock
            self._file = sock.makefile("rwb")
            return True
        return False

    def _reap(self):
        proc = self._proc
        if proc is None:
            return
        proc.wait()
        with self._lock:
            if self._proc is proc:
                log("playback finished (rc=%s)", proc.returncode)
                self._teardown()

    def _teardown(self):
        """Give the display back. Idempotent -- it runs on EOF, on stop, and
        from the unit's ExecStopPost path via a fresh process."""
        with self._lock:
            if self._file is not None:
                try:
                    self._file.close()
                except OSError:
                    pass
                self._file = None
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None
            self._proc = None
            self._item = None
            self._status_cache = (0.0, {})
            try:
                os.unlink(MPV_SOCKET)
            except OSError:
                pass
            self._clear_flag(BUSY_FILE)
            if self._restore_airplay:
                self._restore_airplay = False
                log("restoring %s", AIRPLAY_UNIT)
                systemctl("start", AIRPLAY_UNIT)
                self._clear_flag(RESTORE_FILE)

    def stop(self):
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        self.command(["quit"], quiet=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("mpv ignored quit; killing")
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        with self._lock:
            if self._proc is proc:
                self._teardown()

    # -- flag files in /run: the busy flag the idle clock watches, and the
    #    restore record that survives this process being killed

    @staticmethod
    def _write_flag(path, contents):
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(contents + "\n")
        except OSError as exc:
            log("could not write %s: %s", path, exc)

    @staticmethod
    def _clear_flag(path):
        if not path:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    def reconcile(self):
        """Put the display back where it belongs after an unclean exit.

        /run is tmpfs, so a reboot clears these files and this does nothing --
        which is right, because a boot starts the AirPlay unit itself. What
        this catches is the daemon being SIGKILLed mid-film: systemd restarts
        it, mpv is already gone with it (KillMode=control-group), and the only
        thing left is an AirPlay receiver somebody stopped and never restarted.
        """
        self._clear_flag(BUSY_FILE)
        if not RESTORE_FILE or not os.path.exists(RESTORE_FILE):
            return
        try:
            with open(RESTORE_FILE) as fh:
                unit = fh.read().strip() or AIRPLAY_UNIT
        except OSError:
            unit = AIRPLAY_UNIT
        if not unit_active(unit):
            log("recovering from an unclean stop: restoring %s", unit)
            systemctl("start", unit)
        self._clear_flag(RESTORE_FILE)

    # -- mpv JSON IPC

    def command(self, cmd, quiet=False):
        """One request/response over the IPC socket.

        mpv multiplexes asynchronous events onto the same socket, so replies
        are matched by request_id and anything else on the wire is discarded.
        Safe only because every caller holds _ipc_lock.
        """
        with self._lock:
            fh = self._file
        if fh is None:
            return None
        with self._ipc_lock:
            self._request_id += 1
            rid = self._request_id
            try:
                fh.write(json.dumps({"command": cmd, "request_id": rid}).encode() + b"\n")
                fh.flush()
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    line = fh.readline()
                    if not line:
                        return None
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        continue
                    if msg.get("request_id") == rid:
                        return msg
            except (OSError, ValueError) as exc:
                if not quiet:
                    log("IPC %s: %s", cmd[0] if cmd else "?", exc)
            return None

    def get_property(self, name):
        msg = self.command(["get_property", name])
        if msg and msg.get("error") == "success":
            return msg.get("data")
        return None

    def set_pause(self, paused):
        self.command(["set_property", "pause", bool(paused)])
        self._status_cache = (0.0, {})

    def nudge_volume(self, delta):
        current = self.get_property("volume")
        if current is None:
            return None
        target = max(0, min(130, int(current) + int(delta)))
        self.command(["set_property", "volume", target])
        self._status_cache = (0.0, {})
        return target

    def status(self):
        """Cached for half a second: several phones may have the page open and
        each polls once a second, and there is no reason to ask mpv 20 times."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return {}
            cached_at, cached = self._status_cache
            if time.time() - cached_at < 0.5:
                return cached
        data = {
            "position": self.get_property("time-pos") or 0,
            "duration": self.get_property("duration") or 0,
            "paused": bool(self.get_property("pause")),
            "volume": self.get_property("volume"),
        }
        with self._lock:
            self._status_cache = (time.time(), data)
        return data


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "playstick"
    protocol_version = "HTTP/1.1"

    library = None      # set on the server before serve_forever
    thumbs = None
    player = None

    # BaseHTTPRequestHandler logs every request to stderr, i.e. into the
    # journal, and a page polling once a second per phone would be the only
    # thing in it. Errors still get through via log_error.
    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        if not ALLOW_NETWORKS:
            return True
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(addr in net for net in ALLOW_NETWORKS)

    # -- helpers

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    # -- state shared by /api/status and /api/library

    def _state(self):
        state = self.player.state()
        if state != "idle":
            return state
        if airplay_active():
            return "airplay"
        _order, _items, available, _at, _err = self.library.snapshot()
        return "idle" if available else "unavailable"

    # -- routes

    def do_GET(self):                                # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/":
            return self._serve_ui()
        if path == "/api/library":
            return self._api_library()
        if path.startswith("/api/thumb/"):
            return self._api_thumb(path[len("/api/thumb/"):])
        if path == "/api/status":
            return self._api_status()
        return self._json({"error": "not found"}, 404)

    def do_POST(self):                               # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/api/play":
            return self._api_play()
        if path in ("/api/pause", "/api/resume"):
            self.player.set_pause(path.endswith("pause"))
            return self._api_status()
        if path == "/api/stop":
            self.player.stop()
            return self._api_status()
        if path == "/api/volume":
            self.player.nudge_volume(self._body().get("delta", 0))
            return self._api_status()
        if path == "/api/rescan":
            self.library.request_rescan()
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def _serve_ui(self):
        try:
            with open(UI_FILE, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            return self._send(500, ("UI missing: %s" % exc).encode(), "text/plain")
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _api_library(self):
        order, items, available, scanned_at, error = self.library.snapshot()
        payload = {
            "available": available,
            "error": error,
            "scanned_at": scanned_at,
            "state": self._state(),
            "items": [
                {
                    "id": ident,
                    "title": items[ident]["title"],
                    "has_thumb": self.thumbs.have(ident),
                }
                for ident in order
            ],
        }
        self._json(payload)

    def _api_thumb(self, ident):
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "not found"}, 404)
        cached = Thumbs.cached_path(ident)
        try:
            with open(cached, "rb") as fh:
                body = fh.read()
        except OSError:
            self.thumbs.request(ident)
            # no-store, so the next request after the frame is extracted gets
            # the real thing without the page having to cache-bust.
            return self._send(200, placeholder_svg(item["title"]),
                              "image/svg+xml", {"Cache-Control": "no-store"})
        self._send(200, body, "image/jpeg",
                   {"Cache-Control": "public, max-age=31536000, immutable"})

    def _api_status(self):
        state = self._state()
        data = self.player.status()
        self._json({
            "state": state,
            "title": self.player.current_title(),
            "position": data.get("position", 0),
            "duration": data.get("duration", 0),
            "volume": data.get("volume"),
            "audio": HAS_AUDIO,
            "thumbs_pending": self.thumbs.pending(),
        })

    def _api_play(self):
        ident = str(self._body().get("id", ""))
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "That film is not in the library."}, 404)
        try:
            self.player.start(item)
        except Busy as exc:
            return self._json({"error": str(exc), "state": self._state()}, 409)
        except Exception as exc:                     # noqa: BLE001
            log("play failed: %s", exc)
            return self._json({"error": "The player would not start."}, 500)
        return self._api_status()


# --- main ------------------------------------------------------------------

def main():
    if not os.path.isdir(THUMB_DIR):
        os.makedirs(THUMB_DIR, exist_ok=True)

    library = Library()
    player = Player()
    thumbs = Thumbs(library, player)

    # Clean up after whatever killed the last run before serving anything.
    player.reconcile()

    stopping = threading.Event()

    def shutdown(_sig, _frm):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=library.run, args=(stopping,), daemon=True).start()
    threading.Thread(target=thumbs.run, args=(stopping,), daemon=True).start()

    Handler.library = library
    Handler.thumbs = thumbs
    Handler.player = player

    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    log("listening on %s:%d, library %s", BIND, PORT, LIBRARY)

    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                     daemon=True).start()

    while not stopping.is_set():
        time.sleep(0.25)

    log("shutting down")
    httpd.shutdown()
    # Stopping the daemon must not leave the projector showing a film with
    # nothing able to stop it, and must not leave AirPlay switched off.
    player.stop()


if __name__ == "__main__":
    main()
