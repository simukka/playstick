"""mpv, and the arbitration around starting it.

The class that actually puts a film on the projector, and the one place that
stops and restarts the AirPlay receiver. It also publishes the TIMECODE -- the
master clock every listening phone pins its headphone audio to. See _advance()
for what that is and why it is shaped the way it is.
"""

import errno
import json
import os
import shlex
import socket
import subprocess
import threading
import time

from .config import (
    AIRPLAY_UNIT, BUSY_FILE, LIBRARY, MPV, MPV_ARGS, MPV_SOCKET,
    MPV_TTY, RESTORE_FILE, SETTLE_SECONDS, SUBTITLES, log
)
from .airplay import airplay_confirmed, systemctl, unit_active


class Busy(Exception):
    """Refused: something else owns the projector."""


# How far a fresh time-pos may sit from where the published timecode said it
# would be before it counts as a jump rather than as playback. Half a second is
# an order of magnitude above the frame quantisation below and an order of
# magnitude below any seek a person would make.
TIMECODE_JUMP = 0.5

# Readings an anchor is picked from: at most this many, and none older than
# this. Both bounds are needed and they bound different things.
#
# The COUNT is what the maximum in _advance() has to work with, and eight of
# them at the half-second sampling below is four seconds.
#
# The AGE is what stops that from becoming an hour. Samples are only taken when
# somebody asks for a status, so with one poller every few seconds -- a curl, a
# dashboard, nobody at all -- eight samples can span minutes. mpv's time-pos is
# paced by the audio clock rather than by this machine's monotonic one, and the
# two differ by tens of ppm, so a winner held for ten minutes is tens of
# milliseconds stale by the end of it. Four seconds keeps that under a tenth of
# the frame quantisation the maximum is there to remove in the first place.
TIMECODE_WINDOW = 8
TIMECODE_MAX_AGE = 4.0


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
        # The published timeline: the last timecode, the samples an anchor is
        # chosen from, and a counter that only ever goes up. The counter
        # deliberately survives a film ending, because "the timeline you were
        # following is gone" and "here is a different one" are the same message
        # to a phone and it must not be possible to send it twice by accident.
        self._timecode = None
        self._window = []
        self._epoch = 0

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

    def current_item(self):
        """The whole library entry, for the caller that needs more of it than a
        title -- /api/status has to name the film's id and its audio tracks so
        that a phone can ask for one."""
        with self._lock:
            return self._item

    # -- lifecycle

    def start(self, item, progress=None):
        """Put a film on the screen.

        `progress` is an optional callable taking a step name, called at the
        two points inside here that a person waiting can perceive: taking the
        display away from the AirPlay receiver, which can take twenty seconds,
        and launching mpv. It exists so that projectionist.py can report those
        steps without this method being split apart -- the ordering below is
        load-bearing in several places and is worth keeping in one piece.
        """
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

            if progress:
                progress("display")

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

            argv = [MPV, *MPV_ARGS]
            # Passed explicitly rather than relying on --sub-auto: the prep
            # tool keeps extracted subtitles under .playstick/subs/, which is
            # nowhere near the film, precisely so that it never has to write
            # into a library it was given read-only.
            if SUBTITLES:
                argv += ["--sub-file=" + sub for sub in item.get("subs") or []]
            argv += ["--input-ipc-server=" + MPV_SOCKET, "--", item["path"]]
            if progress:
                progress("starting")
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
            # A different film is a different timeline even if the last one
            # ended at the same number of seconds in.
            self._timecode = None
            self._window = []

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
            # The timeline is over. Dropping the anchor rather than the counter
            # is the point: the next film gets a new epoch, and no phone can be
            # handed the one it was already following.
            self._timecode = None
            self._window = []
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
        # Dropping the cache is also what bumps the epoch: the next status()
        # re-samples, sees a rate that is not the published one, and _advance()
        # does the rest. mpv has already applied the pause by the time this
        # returns -- set_property waits for its reply -- so that sample is not
        # a race.
        self._status_cache = (0.0, {})

    def nudge_volume(self, delta):
        current = self.get_property("volume")
        if current is None:
            return None
        target = max(0, min(130, int(current) + int(delta)))
        self.command(["set_property", "volume", target])
        self._status_cache = (0.0, {})
        return target

    def _advance(self, position, at, paused, buffering):
        """Fit one reading onto the published timeline, or begin a new one.

        THE TIMECODE is what a phone follows: where the film was, the instant
        on this machine's clock when it was there, whether it is moving, and
        which timeline it belongs to.

            {"tc": 1421.834, "at": 918273.4551, "rate": 1.0, "epoch": 7}

        Nothing is carried forward here. A phone extrapolates it against its
        own clock, which is the only clock the answer is going to be used
        against, and `at` is what lets it. This payload used to hand back a
        position already moved to "now" precisely because it had no way to say
        when "now" was; that guess is gone.

        THE EPOCH is the whole contract. While it holds, the line a phone is
        extrapolating is still the right line and a fresh anchor only sharpens
        it -- so a pause, a buffering blink and a film change cost one seek
        each instead of tearing down a clock model that took eight polls to
        build. When it changes, whatever a phone had is wrong. It therefore
        moves for every discontinuity there is: a film starting or ending, a
        pause in either direction, the demuxer stalling, and -- the one there
        is no other way to see -- a time-pos that is not where the last
        timecode said it would be. That last rule covers whatever mpv does
        that this process did not ask it to.

        THE ANCHOR is the least-late reading in a short window, not the newest
        one. mpv reports time-pos on frame boundaries, so a reading is up to a
        frame behind the truth and never ahead of it -- 42 ms at 24 fps, and
        one-sided, which is the kind of error a maximum removes. Over eight
        samples what is left is a few milliseconds against a perception
        threshold of forty-five, and it is done once here rather than once per
        listener over samples the network has already added noise to. The
        window is bounded by age as well as by count; see TIMECODE_MAX_AGE for
        why counting alone is not enough.
        """
        if position is None:
            return None
        rate = 0.0 if (paused or buffering) else 1.0
        with self._lock:
            prev = self._timecode
            if prev is None or prev["rate"] != rate:
                broke = True
            else:
                predicted = prev["tc"] + prev["rate"] * (at - prev["at"])
                broke = abs(position - predicted) > TIMECODE_JUMP
            if broke:
                self._epoch += 1
                self._window = []
            if rate:
                self._window.append((position - at, position, at))
                self._window = [s for s in self._window[-TIMECODE_WINDOW:]
                                if at - s[2] <= TIMECODE_MAX_AGE]
                # Whichever reading's film clock ran furthest ahead of this
                # machine's is the one that was taken closest to the frame it
                # reported, so it is the one with the least to apologise for.
                _lead, tc, anchor = max(self._window)
            else:
                # A stopped timeline does not advance between readings, so
                # there is nothing for a maximum to be the maximum of -- and a
                # winner held over from before the stop would be extrapolated
                # by a phone at a rate the film is no longer running at.
                self._window = []
                tc, anchor = position, at
            self._timecode = {"tc": round(tc, 4), "at": round(anchor, 4),
                              "rate": rate, "epoch": self._epoch}
            return dict(self._timecode)

    @staticmethod
    def _at_now(data):
        """The sample, with `position` moved to now off the timecode.

        The progress bar is the only thing that reads `position`, and one that
        stepped half a second at a time -- the cache window below -- would be
        visibly worse than one that does not. So this arithmetic stays, for
        that one consumer, on the one clock it is honest on: this machine's.
        Every phone does it for itself against its own.
        """
        tc = data.get("timecode")
        if not tc:
            return data
        out = dict(data)
        out["position"] = tc["tc"] + tc["rate"] * (time.monotonic() - tc["at"])
        return out

    def status(self):
        """Cached for half a second: several phones may have the page open and
        each polls once a second, and there is no reason to ask mpv 20 times.

        Position stays None until mpv has loaded the file, rather than
        collapsing to 0. During the first second of a film "I do not know yet"
        and "the beginning" are different answers, and a phone that believes
        the second one seeks to the start of the film.
        """
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return {}
            cached_at, cached = self._status_cache
            if time.monotonic() - cached_at < 0.5:
                return self._at_now(cached)
        # Stamped AROUND the one call that reads it, and no others. The reading
        # is true somewhere inside [before, after] and the midpoint is the
        # least-wrong point in that interval; taking the stamp at the end of
        # the four calls below instead -- which is what this used to do --
        # dated every sample by however long mpv took to answer them, and every
        # listener inherited the bias.
        before = time.monotonic()
        position = self.get_property("time-pos")
        after = time.monotonic()
        data = {
            "position": position,
            "duration": self.get_property("duration") or 0,
            "paused": bool(self.get_property("pause")),
            # mpv freezes the picture and stops advancing time-pos while the
            # demuxer cache refills, which over CIFS on this box's Wi-Fi is a
            # thing that happens. A phone that keeps playing through it is
            # permanently ahead afterwards, so the page has to know.
            "buffering": bool(self.get_property("paused-for-cache")),
            "volume": self.get_property("volume"),
        }
        data["timecode"] = self._advance(
            position, (before + after) / 2.0, data["paused"], data["buffering"])
        with self._lock:
            self._status_cache = (time.monotonic(), data)
            data = self._at_now(data)
        return data
