"""Posters: the ones the admin app uploaded, the ones this box extracted, and
the placeholder for films that have neither.

All three used to live in different places -- a JPEG on the share, a JPEG in
/var/cache, and an SVG generated per request. The first two are rows in the
`thumb` table now, which is what makes a poster survive a re-provision (the
cache directory was not backed up by anything and was fair game for a cleaner)
and what lets /api/thumb answer with one query instead of two open() attempts.

The extractor is still a background worker rather than an endpoint: pulling a
frame out of a film over CIFS costs seconds, and it must never happen while
somebody is watching one -- hence the gate on the projectionist's state and the
nice/ionice wrapper.
"""

import hashlib
import os
import shutil
import subprocess
import threading
import time

from .config import MPV, THUMB_ARGS, THUMB_AT, THUMB_TIMEOUT, log


def etag_for(data):
    """The identity of a poster's bytes. Short because it goes in a header on
    every tile in the grid, and a collision costs a stale poster rather than
    anything that matters."""
    return hashlib.sha1(data).hexdigest()[:16]


class Thumbs:
    """One worker, one film at a time, and nothing at all while a film plays.

    mpv does the frame extraction, so ffmpeg never has to be installed: this
    SoC has no CPU to spare and the eMMC has no room for a second decoder.
    """

    # Where a frame is staged before it becomes a row. Under the database's own
    # directory rather than /tmp, so it is on storage the unit can write and
    # goes away with the rest of the state if anybody ever removes it.
    WORK = "thumb-work"

    def __init__(self, db, library, player):
        self._db = db
        self._library = library
        self._player = player
        self._lock = threading.Lock()
        self._queue = []
        self._queued = set()
        self._failed = set()
        self._cond = threading.Condition(self._lock)

    # -- the store ----------------------------------------------------------

    def get(self, ident):
        """(bytes, mime, etag) for one film's poster, or None."""
        row = self._db.one(
            "SELECT bytes, mime, etag FROM thumb WHERE film_id = ?", (ident,))
        if row is None:
            return None
        return bytes(row["bytes"]), row["mime"], row["etag"]

    def etag(self, ident):
        row = self._db.one("SELECT etag FROM thumb WHERE film_id = ?", (ident,))
        return row["etag"] if row else ""

    def have(self, ident):
        return self._db.one(
            "SELECT 1 FROM thumb WHERE film_id = ?", (ident,)) is not None

    def haves(self):
        """Every id that has a poster, as a set.

        /api/library asks once per request rather than once per film: a
        hundred-film grid was a hundred os.path.isfile calls and is now one
        query.
        """
        return {r["film_id"] for r in self._db.query("SELECT film_id FROM thumb")}

    def store(self, ident, data, mime="image/jpeg", source="admin"):
        """One poster's bytes into the table. Returns its etag.

        Foreign-keyed to `film`, so storing one for an id that is not in the
        library raises rather than accumulating orphans -- the HTTP layer
        checks first and answers 404, and this is the second line.
        """
        tag = etag_for(data)
        conn = self._db.write()
        with conn:
            conn.execute(
                "INSERT INTO thumb (film_id, mime, bytes, etag, source, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (film_id) DO UPDATE SET mime = excluded.mime, "
                "bytes = excluded.bytes, etag = excluded.etag, "
                "source = excluded.source, updated_at = excluded.updated_at",
                (ident, mime, data, tag, source, time.time()))
        with self._cond:
            # An uploaded poster is a reason to stop refusing to extract one:
            # whoever cleared it may want the frame back.
            self._failed.discard(ident)
        return tag

    def clear(self, ident):
        """Drop a film's poster, so the extractor may try again. Returns
        whether there was one."""
        conn = self._db.write()
        with conn:
            cur = conn.execute("DELETE FROM thumb WHERE film_id = ?", (ident,))
        with self._cond:
            self._failed.discard(ident)
        return cur.rowcount > 0

    # -- the extractor ------------------------------------------------------

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
            # A poster already in the table means the admin app uploaded one --
            # made on a machine with cores and without a film competing for
            # them -- or a previous pass already did this.
            if not item or self.have(ident):
                continue
            if not self._extract(ident, item):
                with self._cond:
                    self._failed.add(ident)

    def _extract(self, ident, item):
        source = item["sidecar"] or item["path"]
        is_still = source is item["sidecar"]
        staging = os.path.join(os.path.dirname(self._db.path),
                               self.WORK + "-" + ident)
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
            with open(os.path.join(staging, produced[0]), "rb") as handle:
                data = handle.read()
            if not data:
                log("thumbnail empty for %s", item["title"])
                return False
            self.store(ident, data, "image/jpeg", source="extracted")
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
