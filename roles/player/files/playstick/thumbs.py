"""Poster extraction, and the placeholder for films that have none.

A background worker rather than an endpoint: pulling a frame out of a film over
CIFS costs seconds, and it must never happen while somebody is watching one --
hence the gate on Player.state() and the nice/ionice wrapper.
"""

import hashlib
import os
import shutil
import subprocess
import threading
import time

from .config import MPV, THUMB_ARGS, THUMB_AT, THUMB_DIR, THUMB_TIMEOUT, log


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
            # item["poster"] means the developer machine already did this, with
            # cores and without a film competing for them.
            if not item or item.get("poster") or self.have(ident):
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
