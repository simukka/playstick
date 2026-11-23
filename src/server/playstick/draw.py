"""Drawing on the film: one canvas over the picture, shared by every phone.

The now-playing view's Draw button opens a sheet that works like the pointer:
a touchpad, or tilting while a button is held, moves a cursor on the screen,
and with the pen down the cursor leaves a line. The phone shows no picture --
everybody looks at the screen. The phone posts to POST /api/draw, and this
keeps the drawing and puts it over the film with mpv's osd-overlay -- the same
mechanism as the pointer's dot (pointer.py), one overlay id along.

ONE CANVAS, MANY PAINTERS

The drawing lives here. Every phone with the sheet open is a painter with a
cursor of its own -- a ring the size of what it draws, in its colour -- and
all of them ink the same canvas. A painter is a phone heard from in the last
PAINTER_SECONDS; every reply says how many there are, so a phone can say "3
drawing" and nobody wonders whose line that was. A cursor nobody moves goes
after CURSOR_SECONDS, as the pointer's dot does.

STROKES ARE POINTS, AND THE ERASER TAKES POINTS AWAY

A stroke is a tool, a colour, a size and a list of points in 0..1 screen space.
Points are filled in on arrival so that none is further than STEP from the
last -- a fast finger posts points far apart, and without this the eraser
would pass between two of them and miss the line joining them. The eraser is
not stored: each eraser point removes the points of every stroke within its
radius, and a stroke with a gap in it becomes two. That is a real eraser to
look at, and it needs no compositing, which an ASS overlay cannot do.

WHAT BOUNDS IT

MAX_POINTS for the whole drawing, because every draw re-sends all of it to
libass. A phone told "full" says so; Clear empties it. The drawing belongs to
the film it was drawn on and goes when that film does.
"""

import threading
import time

from .config import log
from .pointer import circle


# Fixed tables, for the same reason as the pointer's: these strings are spliced
# into an ASS drawing, and a route that only accepts keys of a table cannot be
# handed anything else.
COLORS = {                      # ASS is BGR
    "red": "3B30FF",
    "orange": "0A9FFF",
    "yellow": "0AD6FF",
    "green": "59C734",
    "blue": "FF7A0A",
    "purple": "DE52AF",
    "pink": "8A47FF",
    "brown": "40618C",
    "white": "FFFFFF",
    "black": "000000",
}
TOOLS = ("brush", "pencil", "eraser")
# Radius, as a fraction of the screen's height. The page offers these three
# per tool, and remembers which one each tool was last given.
SIZES = {
    "pencil": {"s": 0.002, "m": 0.004, "l": 0.007},
    "brush": {"s": 0.008, "m": 0.016, "l": 0.03},
    "eraser": {"s": 0.015, "m": 0.035, "l": 0.07},
}

ASPECT = 16 / 9             # the stick drives the projector at 16:9
STEP = 0.006                # the furthest apart two stored points may be
MAX_POINTS = 30000          # in the whole drawing
MAX_BATCH = 400             # in one request
PAINTER_SECONDS = 3.0       # how recently a phone was heard from to count
CURSOR_SECONDS = 1.5        # how long a cursor outlives its last point
DRAW_INTERVAL = 0.04        # the shortest gap between two redraws
OVERLAY_ID = 9              # the pointer is 7, calibration 8
Z = -1                      # under the pointer's dot, which is z 0


def _dist(a, b):
    """Between two points in 0..1 screen space, in screen heights."""
    return ((a[0] - b[0]) * ASPECT) ** 2 + (a[1] - b[1]) ** 2


def densify(last, points):
    """`points`, with points added so that none is more than STEP from the one
    before it (starting from `last`, which is not repeated)."""
    out = []
    prev = last
    for p in points:
        if prev is not None:
            d = _dist(prev, p) ** 0.5
            n = int(d / STEP)
            for i in range(1, n + 1):
                t = i / (n + 1)
                out.append((prev[0] + (p[0] - prev[0]) * t,
                            prev[1] + (p[1] - prev[1]) * t))
        out.append(p)
        prev = p
    return out


class Stroke:
    __slots__ = ("tool", "color", "size", "pts")

    def __init__(self, tool, color, size, pts=None):
        self.tool, self.color, self.size = tool, color, size
        self.pts = pts if pts is not None else []


def erase(strokes, points, radius):
    """`strokes` with every point within `radius` of any of `points` taken
    out, and each stroke split where that leaves a gap. Returns the new list
    and, for each stroke that survives, the stroke it came from -- so whoever
    was still drawing it can carry on drawing onto the right piece."""
    r2 = radius * radius
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    rx = radius / ASPECT
    box = (min(xs) - rx, max(xs) + rx, min(ys) - radius, max(ys) + radius)
    out = []
    for s in strokes:
        run = []
        pieces = []
        hit = False
        for p in s.pts:
            near = (box[0] <= p[0] <= box[1] and box[2] <= p[1] <= box[3]
                    and any(_dist(p, q) <= r2 for q in points))
            if near:
                hit = True
                if run:
                    pieces.append(run)
                    run = []
            else:
                run.append(p)
        if not hit:
            out.append((s, s))
            continue
        if run:
            pieces.append(run)
        for piece in pieces:
            out.append((Stroke(s.tool, s.color, s.size, piece), s))
    return out


def stroke_event(s, width, height):
    """One ASS event for a stroke. The path goes out along the points and back,
    so it has no area, and the line is its border: \\bord is the radius, and
    libass rounds the joins and the two ends. Placed with \\an7\\pos(0,0),
    which puts the drawing's origin at the screen's (see calibrate._shape),
    so the points are screen pixels as they stand."""
    r = SIZES[s.tool][s.size] * height
    pts = [(round(x * width), round(y * height)) for x, y in s.pts]
    if len(pts) == 1:
        pts.append((pts[0][0] + 1, pts[0][1]))
    path = "m %d %d l " % pts[0] + " ".join(
        "%d %d" % p for p in pts[1:] + pts[-2::-1])
    if s.tool == "brush":
        soft = "\\blur%.1f\\3a&H18&" % max(0.6, r * 0.3)
    else:
        soft = "\\blur0.6\\3a&H00&"
    return ("{\\an7\\pos(0,0)\\bord%.1f\\shad0%s\\1a&HFF&\\3c&H%s&\\p1}%s{\\p0}"
            % (max(1.0, r), soft, COLORS[s.color], path))


def cursor_event(x, y, tool, size, color, width, height):
    """A ring where a painter's finger is: the size of what it will draw, or
    of what it will rub out."""
    r = max(4, SIZES[tool][size] * height + 2)
    edge = "FFFFFF" if tool == "eraser" else COLORS[color]
    return ("{\\an7\\pos(%d,%d)\\bord1.5\\shad0\\1a&HFF&\\3c&H%s&\\p1}%s{\\p0}"
            % (round(x * width), round(y * height), edge, circle(0, 0, r)))


class Drawing:
    """The shared canvas and the overlay that shows it. One worker thread."""

    def __init__(self, player, clock=time.monotonic):
        self._player = player
        self._clock = clock
        self._cond = threading.Condition()
        self._film = None
        self._strokes = []
        self._live = {}             # (phone, stroke id) -> the Stroke it feeds
        self._erasing = {}          # (phone, stroke id) -> last eraser point
        self._cursors = {}          # phone -> (x, y, tool, size, color, when)
        self._seen = {}             # phone -> when it was last heard from
        self._points = 0
        self._dirty = False
        self._shown = False
        self._dims = (None, None)
        self._last_draw = None
        self.draws = 0

    # -- from HTTP threads

    def _playing(self):
        """The film's id when one is up to draw on, else None. A different
        film from last time drops the drawing: it was drawn on that one."""
        if self._player.state() not in ("playing", "paused"):
            return None
        item = self._player.current_item() or {}
        return item.get("id") or "?"

    def _sync_film(self, film):
        if film != self._film:
            self._film = film
            if self._strokes or self._cursors:
                self._reset()

    def _reset(self):
        self._strokes = []
        self._live = {}
        self._erasing = {}
        self._cursors = {}
        self._points = 0
        self._dirty = True
        self._cond.notify()

    def _reply(self, phone, now):
        self._seen[phone] = now
        for p, t in list(self._seen.items()):
            if now - t > PAINTER_SECONDS:
                del self._seen[p]
        return {"shown": self._film is not None, "painters": len(self._seen),
                "full": self._points >= MAX_POINTS}

    def sync(self, phone):
        """Nothing drawn: a painter checking in."""
        film = self._playing()
        with self._cond:
            self._sync_film(film)
            return self._reply(phone, self._clock())

    def move(self, phone, x, y, tool, color, size):
        """A painter's cursor moved with the pen up."""
        film = self._playing()
        now = self._clock()
        with self._cond:
            self._sync_film(film)
            if film is not None:
                self._cursors[phone] = (x, y, tool, size, color, now)
                self._dirty = True
                self._cond.notify()
            return self._reply(phone, now)

    def stroke(self, phone, sid, tool, color, size, points, end):
        """More of stroke `sid` from `phone`: the points since the last
        request, and whether the pen has come up. The cursor stays where the
        line ended, and goes by itself like any other."""
        film = self._playing()
        now = self._clock()
        with self._cond:
            self._sync_film(film)
            if film is None:
                return self._reply(phone, now)
            key = (phone, sid)
            if points:
                if tool == "eraser":
                    self._rub(key, points, SIZES["eraser"][size])
                else:
                    self._ink(key, tool, color, size, points)
                x, y = points[-1]
                self._cursors[phone] = (x, y, tool, size, color, now)
            if end:
                self._live.pop(key, None)
                self._erasing.pop(key, None)
            if self._dirty:
                self._cond.notify()
            return self._reply(phone, now)

    def _ink(self, key, tool, color, size, points):
        s = self._live.get(key)
        if s is None:
            if self._points >= MAX_POINTS:
                return
            s = Stroke(tool, color, size)
            self._strokes.append(s)
            self._live[key] = s
        added = densify(s.pts[-1] if s.pts else None, points)
        added = added[:max(0, MAX_POINTS - self._points)]
        if not added:
            return
        s.pts.extend(added)
        self._points += len(added)
        self._dirty = True

    def _rub(self, key, points, radius):
        last = self._erasing.get(key)
        path = densify(last, points)
        self._erasing[key] = points[-1]
        pieces = erase(self._strokes, path, radius)
        self._strokes = [s for s, _ in pieces]
        self._points = sum(len(s.pts) for s in self._strokes)
        # A stroke somebody is still drawing carries on from its last piece --
        # or, if the eraser took its end, starts afresh at their next point.
        for k, live in list(self._live.items()):
            tails = [s for s, was in pieces if was is live]
            if tails and tails[-1].pts[-1] == live.pts[-1]:
                self._live[k] = tails[-1]
            else:
                del self._live[k]
        self._dirty = True

    def clear(self, phone):
        film = self._playing()
        with self._cond:
            self._sync_film(film)
            if self._strokes:
                self._reset()
            return self._reply(phone, self._clock())

    def leave(self, phone):
        """A phone closed its sheet: it is not painting any more."""
        with self._cond:
            self._seen.pop(phone, None)
            if self._cursors.pop(phone, None) is not None:
                self._dirty = True
                self._cond.notify()

    # -- the worker

    def run(self, stopping):
        while not stopping.is_set():
            self.step(wait=0.05)

    def step(self, wait=0.0):
        """Draw the canvas if it changed, or drop a cursor that went quiet."""
        with self._cond:
            if not self._dirty and wait:
                self._cond.wait(wait)
            now = self._clock()
            # Every redraw hands libass the whole drawing again, so a burst of
            # points from four phones is drawn at most this often, newest only.
            if (self._dirty and wait and self._last_draw is not None
                    and now - self._last_draw < DRAW_INTERVAL):
                self._cond.wait(DRAW_INTERVAL - (now - self._last_draw))
                now = self._clock()
            for p, c in list(self._cursors.items()):
                if now - c[5] >= CURSOR_SECONDS:
                    del self._cursors[p]
                    self._dirty = True
            if not self._dirty:
                return
            self._dirty = False
            film = self._film
            strokes = [Stroke(s.tool, s.color, s.size, list(s.pts))
                       for s in self._strokes]
            cursors = list(self._cursors.values())
        if film is None or (not strokes and not cursors):
            if self._shown:
                self._hide()
            return
        self._draw(strokes, cursors)

    def _size(self):
        item = self._player.current_item() or {}
        ident = item.get("id", "")
        if self._dims[0] != ident or self._dims[1] is None:
            dims = self._player.get_property("osd-dimensions") or {}
            w, h = dims.get("w"), dims.get("h")
            self._dims = (ident, (int(w), int(h)) if w and h else None)
        return self._dims[1] or (1920, 1080)

    def _draw(self, strokes, cursors):
        w, h = self._size()
        events = [stroke_event(s, w, h) for s in strokes if s.pts]
        events += [cursor_event(c[0], c[1], c[2], c[3], c[4], w, h)
                   for c in cursors]
        reply = self._player.command(
            ["osd-overlay", OVERLAY_ID, "ass-events", "\n".join(events),
             w, h, Z, False, False],
            quiet=True)
        self._last_draw = self._clock()
        if reply is None:
            return
        if reply.get("error") not in (None, "success"):
            log("draw: mpv refused the overlay: %s", reply.get("error"))
            return
        self._shown = True
        self.draws += 1

    def _hide(self):
        self._player.command(["osd-overlay", OVERLAY_ID, "none", ""],
                             quiet=True)
        self._shown = False
