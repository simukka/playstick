"""A dot over the film, moved by a phone: the pointer in the page's hidden menu.

The phone sends where the dot should be, up to twenty-five times a second, to
POST /api/pointer. This draws it with mpv's osd-overlay -- an ASS drawing, so a
round dot in any colour, on top of the picture and the letterbox alike, with no
second process and nothing to composite. It only exists while a film does: no
mpv, no dot, and the overlay dies with mpv anyway.

ONLY THE NEWEST POSITION IS EVER DRAWN

Requests arrive on HTTP threads and each mpv command waits for its reply under
the player's IPC lock, which status polls and the seek bar also take. Drawing
from the request thread would queue a burst of samples behind each other and
behind everything else, and the dot would trail the finger by however long the
queue had grown. So a request only replaces `_want`; one worker thread draws
whatever is newest when it gets there and skips the rest.

THE DOT GOES AWAY BY ITSELF

A phone that locks its screen mid-gesture sends no "off". After HIDE_SECONDS
without a sample the worker hides the dot, so nobody has to walk over to the
stick to get rid of a red spot on a film.
"""

import threading
import time

from .config import log


# The page offers exactly these, and anything else is refused at the route: an
# ASS colour is a string spliced into a drawing command, and a fixed table is
# the simplest way to make sure nothing else is.
SIZES = {"s": 0.012, "m": 0.02, "l": 0.032}         # radius, as a fraction of height
COLORS = {"red": "0000FF", "green": "00FF00", "white": "FFFFFF"}  # ASS is BGR

HIDE_SECONDS = 1.5
OVERLAY_ID = 7          # any id; one overlay, reused
# Bezier control distance for a quarter circle, as a fraction of the radius.
_K = 0.5523


def circle(cx, cy, r):
    """An ASS \\p1 drawing of a circle centred on (cx, cy), radius r."""
    k = r * _K

    def p(x, y):
        return "%d %d" % (round(cx + x), round(cy + y))
    return ("m %s b %s %s %s b %s %s %s b %s %s %s b %s %s %s" % (
        p(0, -r),
        p(k, -r), p(r, -k), p(r, 0),
        p(r, k), p(k, r), p(0, r),
        p(-k, r), p(-r, k), p(-r, 0),
        p(-r, -k), p(-k, -r), p(0, -r)))


def drawing(x, y, size, color, width, height):
    """The ass-events payload for one dot at (x, y) in 0..1 screen space."""
    # The circle is drawn around its own origin and placed with \an5\pos: a
    # centred alignment puts the middle of the drawing's bounding box -- the
    # circle's centre -- on the point, whatever libass does with the origin.
    r = SIZES[size] * height
    outline = "000000"
    return ("{\\an5\\pos(%d,%d)\\bord%d\\shad0\\1c&H%s&\\3c&H%s&\\1a&H20&\\p1}%s{\\p0}"
            % (round(x * width), round(y * height), max(1, round(r * 0.15)),
               COLORS[color], outline, circle(0, 0, r)))


class PointerOverlay:
    """Draws the newest pointer sample over the film. One worker thread."""

    def __init__(self, player, clock=time.monotonic):
        self._player = player
        self._clock = clock
        self._cond = threading.Condition()
        self._want = None           # (x, y, size, color) or None for hidden
        self._dirty = False
        self._last_sample = None    # when the newest sample arrived
        self._shown = False
        self._dims = (None, None)   # (film id, (w, h)) the drawing is sized for
        self.draws = 0              # for tests and the journal, nothing else

    # -- from HTTP threads

    def update(self, x, y, on, size="m", color="red"):
        """Record where the dot should be. Returns whether a film is up to
        draw it over, so the page can say so instead of pointing at nothing."""
        playing = self._player.state() in ("playing", "paused")
        with self._cond:
            self._want = (x, y, size, color) if (on and playing) else None
            self._last_sample = self._clock()
            self._dirty = True
            self._cond.notify()
        return playing

    # -- the worker

    def run(self, stopping):
        while not stopping.is_set():
            self.step(wait=0.05)

    def step(self, wait=0.0):
        """Draw the newest sample, or hide a stale dot. One pass; the unit the
        tests drive."""
        with self._cond:
            if not self._dirty and wait:
                self._cond.wait(wait)
            want, dirty = self._want, self._dirty
            stale = (self._shown and self._last_sample is not None
                     and self._clock() - self._last_sample >= HIDE_SECONDS)
            if stale:
                want, dirty = None, True
                self._want = None
            self._dirty = False
        if not dirty:
            return
        if want is None:
            if self._shown:
                self._hide()
            return
        self._draw(*want)

    def _size(self):
        item = self._player.current_item() or {}
        ident = item.get("id", "")
        if self._dims[0] != ident or self._dims[1] is None:
            dims = self._player.get_property("osd-dimensions") or {}
            w, h = dims.get("w"), dims.get("h")
            self._dims = (ident, (int(w), int(h)) if w and h else None)
        return self._dims[1] or (1920, 1080)

    def _draw(self, x, y, size, color):
        w, h = self._size()
        reply = self._player.command(
            ["osd-overlay", OVERLAY_ID, "ass-events",
             drawing(x, y, size, color, w, h), w, h, 0, False, False],
            quiet=True)
        if reply is None:
            return
        if reply.get("error") not in (None, "success"):
            log("pointer: mpv refused the overlay: %s", reply.get("error"))
            return
        self._shown = True
        self.draws += 1

    def _hide(self):
        self._player.command(["osd-overlay", OVERLAY_ID, "none", ""], quiet=True)
        self._shown = False
