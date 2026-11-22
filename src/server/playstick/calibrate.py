"""The calibration wizard's projector side: a black screen, drawn by a phone.

The phone runs the wizard. It decides which step it is on, does the arithmetic
and saves the profile (see src/player/src/calibrate.ts). This end only puts
what the phone is doing on the projector, so the person holding the phone can
see where they are pointing, what has been hit and which buttons have arrived.

WHERE IT IS DRAWN

mpv, started with no file (Player.start_blank). It gives the same DRM handover
a film gets and the same osd-overlay that pointer.py draws the dot with, so the
wizard needs no new process and no font. The screen is one overlay of ASS
events, rebuilt from scratch on every change by scene_ass(). That function is
pure, so the tests can read what would be drawn without an mpv.

THE PHONE SENDS NUMBERS AND NAMES, NEVER TEXT

Every sentence on the screen is written here, chosen from a fixed table by the
step the phone names. The phone sends values: angles, positions, a mask, one
edge or button from a fixed set. ASS override tags are text spliced into a
drawing, and the same rule pointer.py applies to its colours applies here: a
string from the network never reaches the overlay unless it has been reduced
to characters that cannot open a tag. The profile name is the one exception
shown, and it is filtered to letters, digits, spaces and a little punctuation.

ONLY THE NEWEST STATE IS EVER DRAWN

The phone sends telemetry about twenty-five times a second. The socket thread
only replaces `_scene` and `_tel`. One worker draws whatever is newest, no
more often than DRAW_INTERVAL, for the same reason PointerOverlay does: mpv
commands queue behind the player's IPC lock.

IT ENDS BY ITSELF

A phone that locks mid-wizard sends nothing. After GRACE_SECONDS with no
socket open, the worker stops the session, so the lamp is not left lit on a
black screen. A phone that comes back sooner reconnects and carries on.
"""

import collections
import json
import math
import threading
import time

from .config import log
from .pointer import circle
from . import ws


OVERLAY_ID = 8          # not the pointer's 7; one overlay, reused
DRAW_INTERVAL = 0.04    # at most 25 redraws a second
GRACE_SECONDS = 15.0    # no phone for this long ends the session
TEL_STALE = 1.0         # no telemetry for this long reads as "waiting"
SOCKET_TIMEOUT = 30.0
MAX_SOCKETS = 4
MAX_TARGETS = 9
NAME_MAX = 32

STEPS = ("intro", "sweep", "targets", "tilt", "buttons", "summary")
EDGES = ("left", "right", "top", "bottom")
BUTTONS = ("A", "B", "START", "SELECT")
TARGET_STATES = ("pending", "active", "hit")
SUMMARY_KEYS = ("yaw_across", "pitch_across", "error", "press", "jitter")

# The mask bits, as tilt-pad.ts numbers them.
BIT = {"up": 1, "down": 2, "left": 4, "right": 8,
       "A": 16, "B": 32, "START": 64, "SELECT": 128}

# ASS colours are BGR.
WHITE = "FFFFFF"
GREY = "8C8C8C"
DIM = "3A3A3A"
YELLOW = "00D7FF"
GREEN = "78C850"
RED = "0000FF"

TITLES = {
    "intro": "Calibrating %(name)s",
    "sweep": "Gyro: the %(edge)s edge",
    "targets": "Pointer: hit the targets",
    "tilt": "Controller: tilt %(tilt)s",
    "buttons": "Controller: press %(button)s",
    "summary": "Calibration results",
}
TEXTS = {
    "intro": "Follow the steps on your phone.",
    "sweep": "Point the top of the phone at the %(edge)s edge of the picture "
             "and hold still.",
    "targets": "Aim the dot at the yellow circle and hold it there.",
    "tilt": "Hold the phone like a controller and tilt it %(tilt)s as far as "
            "is comfortable.",
    "buttons": "Press the button on your phone that should be %(button)s.",
    "summary": "Save them on the phone to use them from now on.",
}
# The tilt step names directions the way a person holding a controller would.
TILT_WORDS = {"left": "left", "right": "right", "top": "away from you",
              "bottom": "towards you"}
SUMMARY_LABELS = {
    "yaw_across": "Turn across the picture: %.0f°",
    "pitch_across": "Tilt down the picture: %.0f°",
    "error": "Average miss: %.1f%% of the width",
    "press": "Controller tilt to press: %.0f°",
    "jitter": "Hand shake: %.2f°",
}


# -- the messages -----------------------------------------------------------


def _num(value, lo, hi, default=None):
    """A finite number clamped to [lo, hi], or `default`. json.loads accepts
    NaN and Infinity, and neither is an angle."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if not math.isfinite(value):
        return default
    return min(hi, max(lo, float(value)))


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    x, y = _num(value[0], 0.0, 1.0), _num(value[1], 0.0, 1.0)
    if x is None or y is None:
        return None
    return (x, y)


def _mask(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value & 0xFF


def clean_name(value):
    """The profile name, reduced to characters that cannot open an ASS tag."""
    if not isinstance(value, str):
        return ""
    keep = "".join(c for c in value if c.isalnum() or c in " '-_.,!?()")
    return " ".join(keep.split())[:NAME_MAX]


def parse_tel(raw):
    """A telemetry sample, or None if it is not one."""
    if not isinstance(raw, dict):
        return None
    return {
        "a": _num(raw.get("a"), -360.0, 360.0),
        "b": _num(raw.get("b"), -180.0, 180.0),
        "g": _num(raw.get("g"), -90.0, 90.0),
        "angle": int(_num(raw.get("angle"), -270, 270, 0)),
        "touch": _point(raw.get("touch")),
        "mask": _mask(raw.get("mask")),
        "dot": _point(raw.get("dot")),
    }


def parse_scene(raw):
    """The wizard's state as the phone describes it, or None if it is not one.
    Anything outside the fixed vocabulary is dropped rather than drawn."""
    if not isinstance(raw, dict) or raw.get("step") not in STEPS:
        return None
    targets = []
    for t in raw.get("targets") or []:
        if len(targets) >= MAX_TARGETS:
            break
        if not isinstance(t, dict):
            continue
        p = _point([t.get("x"), t.get("y")])
        r = _num(t.get("r"), 0.005, 0.2)
        if p is None or r is None or t.get("state") not in TARGET_STATES:
            continue
        targets.append({"x": p[0], "y": p[1], "r": r, "state": t["state"]})
    summary = {}
    for key in SUMMARY_KEYS:
        v = _num((raw.get("summary") or {}).get(key)
                 if isinstance(raw.get("summary"), dict) else None, 0.0, 1000.0)
        if v is not None:
            summary[key] = v
    edge = raw.get("edge")
    button = raw.get("button")
    return {
        "step": raw["step"],
        "name": clean_name(raw.get("name")),
        "edge": edge if edge in EDGES else None,
        "button": button if button in BUTTONS else None,
        "hold": _num(raw.get("hold"), 0.0, 1.0, 0.0),
        "targets": targets,
        "lights": _mask(raw.get("lights")),
        "summary": summary,
    }


# -- drawing ----------------------------------------------------------------


def _shape(cx, cy, path, fill, alpha="00", border=0, outline="000000",
           border_alpha="00"):
    """One ASS drawing event, centred on (cx, cy). The path is drawn around
    its own origin and placed with \\an7\\pos: libass puts the drawing's
    origin on the point. \\an5 does not centre it -- libass sizes the box from
    the origin, so a centred alignment lands it half a shape up and to the
    left. Checked by rendering a frame with ffmpeg's ass filter."""
    return ("{\\an7\\pos(%d,%d)\\bord%d\\shad0\\1c&H%s&\\3c&H%s&\\1a&H%s&"
            "\\3a&H%s&\\p1}%s{\\p0}"
            % (round(cx), round(cy), border, fill, outline, alpha,
               border_alpha, path))


def _rect(w, h):
    x, y = w / 2.0, h / 2.0
    return "m %d %d l %d %d l %d %d l %d %d" % (
        round(-x), round(-y), round(x), round(-y),
        round(x), round(y), round(-x), round(y))


def _text(x, y, size, text, color=WHITE, align=7):
    return ("{\\an%d\\pos(%d,%d)\\fs%d\\bord2\\shad0\\1c&H%s&\\3c&H000000&}%s"
            % (align, round(x), round(y), max(8, round(size)), color, text))


def _fmt(value, places=1):
    return "—" if value is None else ("%." + str(places) + "f") % value


def telemetry_lines(tel, hz, fresh):
    """The panel's lines, as plain text. Separate so the tests can read it."""
    if tel is None or not fresh:
        return ["Waiting for the phone…"]
    touch = ("%.2f, %.2f" % tel["touch"]) if tel["touch"] else "—"
    return [
        "α %s°   β %s°   γ %s°   screen %d°" % (
            _fmt(tel["a"]), _fmt(tel["b"]), _fmt(tel["g"]), tel["angle"]),
        "touch %s   buttons %s   %d Hz" % (touch, format(tel["mask"], "08b"), hz),
    ]


def _words(scene):
    edge = scene["edge"] or "left"
    return {
        "name": scene["name"] or "this phone",
        "edge": edge,
        "tilt": TILT_WORDS[edge],
        "button": scene["button"] or "A",
    }


def _edge_bar(edge, hold, w, h):
    """A bar along one edge of the picture, filling as the hold completes."""
    t = round(h * 0.03)
    out = []
    if edge in ("left", "right"):
        cx = t / 2.0 if edge == "left" else w - t / 2.0
        out.append(_shape(cx, h / 2.0, _rect(t, h), YELLOW, alpha="A0"))
        if hold > 0:
            out.append(_shape(cx, h / 2.0, _rect(t, h * hold), YELLOW))
    else:
        cy = t / 2.0 if edge == "top" else h - t / 2.0
        out.append(_shape(w / 2.0, cy, _rect(w, t), YELLOW, alpha="A0"))
        if hold > 0:
            out.append(_shape(w / 2.0, cy, _rect(w * hold, t), YELLOW))
    return out


def _targets(targets, hold, w, h):
    out = []
    for t in targets:
        r = t["r"] * h
        x, y = t["x"] * w, t["y"] * h
        if t["state"] == "hit":
            out.append(_shape(x, y, circle(0, 0, r), GREEN, alpha="40"))
        elif t["state"] == "active":
            out.append(_shape(x, y, circle(0, 0, r), YELLOW, alpha="C0",
                              border=max(2, round(r * 0.12)), outline=YELLOW))
            if hold > 0:
                inner = r * hold
                out.append(_shape(x, y, circle(0, 0, inner), YELLOW, alpha="40"))
        else:
            out.append(_shape(x, y, circle(0, 0, r), GREY, alpha="E0",
                              border=2, outline=GREY))
    return out


def _controller(lights, want, w, h):
    """A d-pad on the left, A and B on the right, SELECT and START between.
    `lights` is what the phone is pressing now; `want` names the part the
    wizard is asking for, which is ringed in yellow."""
    out = []
    cy = h * 0.55
    s = h * 0.09                       # one d-pad arm
    dx = w * 0.30
    parts = {
        "up": (dx, cy - s, _rect(s, s)),
        "down": (dx, cy + s, _rect(s, s)),
        "left": (dx - s, cy, _rect(s, s)),
        "right": (dx + s, cy, _rect(s, s)),
        "B": (w * 0.64, cy + s * 0.4, circle(0, 0, s * 0.6)),
        "A": (w * 0.74, cy - s * 0.4, circle(0, 0, s * 0.6)),
        "SELECT": (w * 0.45, cy + s * 1.6, _rect(s * 1.2, s * 0.4)),
        "START": (w * 0.55, cy + s * 1.6, _rect(s * 1.2, s * 0.4)),
    }
    out.append(_shape(dx, cy, _rect(s, s), DIM))
    for name, (x, y, path) in parts.items():
        lit = bool(lights & BIT[name])
        fill = GREEN if lit else DIM
        if name == want:
            out.append(_shape(x, y, path, fill, border=max(3, round(s * 0.08)),
                              outline=YELLOW))
        else:
            out.append(_shape(x, y, path, fill))
    for name in ("A", "B", "SELECT", "START"):
        x, y, _path = parts[name]
        label_y = y + (s * 0.75 if name in ("A", "B") else s * 0.45)
        out.append(_text(x, label_y, h * 0.028, name, GREY, align=8))
    return out


def scene_ass(scene, tel, w, h, hz=0, fresh=True):
    """Everything on the screen, as one ass-events payload."""
    words = _words(scene)
    step = scene["step"]
    events = [
        _text(w * 0.05, h * 0.06, h * 0.055, TITLES[step] % words),
        _text(w * 0.05, h * 0.14, h * 0.034, TEXTS[step] % words, GREY),
    ]
    if step == "sweep" and scene["edge"]:
        events += _edge_bar(scene["edge"], scene["hold"], w, h)
    if step == "targets":
        events += _targets(scene["targets"], scene["hold"], w, h)
    if step == "tilt":
        want = {"left": "left", "right": "right", "top": "up",
                "bottom": "down"}.get(scene["edge"])
        events += _controller(scene["lights"], want, w, h)
    if step == "buttons":
        events += _controller(scene["lights"], scene["button"], w, h)
    if step == "summary":
        y = h * 0.30
        for key in SUMMARY_KEYS:
            if key in scene["summary"]:
                events.append(_text(w * 0.08, y, h * 0.045,
                                    SUMMARY_LABELS[key] % scene["summary"][key]))
                y += h * 0.07
    if tel is not None and fresh and tel["dot"] and step == "targets":
        x, y = tel["dot"]
        r = h * 0.018
        events.append(_shape(x * w, y * h, circle(0, 0, r), RED,
                             border=max(1, round(r * 0.15))))
    y = h * 0.86
    for line in telemetry_lines(tel, hz, fresh):
        events.append(_text(w * 0.05, y, h * 0.03, line, GREY))
        y += h * 0.045
    return "\n".join(events)


# -- the session ------------------------------------------------------------


class Calibration:
    """Draws the newest wizard state on the blank mpv. One worker thread.

    `stop` is called to end a session nobody is holding any more; main.py
    hands it the projectionist's, so a session ends the way a film does.
    """

    def __init__(self, player, stop=None, clock=time.monotonic):
        self._player = player
        self._stop = stop
        self._clock = clock
        self._cond = threading.Condition()
        self._scene = None
        self._tel = None
        self._tel_at = None
        self._arrivals = collections.deque()
        self._dirty = False
        self._conns = 0
        self._orphan_since = None
        self._last_draw = None
        self._shown = False
        self._dims = None
        self.draws = 0              # for tests and the journal, nothing else

    # -- from socket threads

    def active(self):
        return self._player.state() == "calibrating"

    def on_message(self, message):
        """One parsed text frame from a phone."""
        if not isinstance(message, dict):
            return
        if message.get("done"):
            self.end()
            return
        with self._cond:
            if "scene" in message:
                scene = parse_scene(message["scene"])
                if scene is not None:
                    self._scene = scene
                    self._dirty = True
            if "tel" in message:
                tel = parse_tel(message["tel"])
                if tel is not None:
                    now = self._clock()
                    self._tel, self._tel_at = tel, now
                    self._arrivals.append(now)
                    self._dirty = True
            if self._dirty:
                self._cond.notify()

    def end(self):
        """The wizard is finished: take the black screen away."""
        with self._cond:
            self._scene = None
            self._orphan_since = None
        if self.active() and self._stop is not None:
            log("calibration finished")
            self._stop()

    def serve(self, conn):
        """Run one phone's socket until it closes, on the handler's thread."""
        with self._cond:
            if self._conns >= MAX_SOCKETS:
                conn.send_text({"error": "Too many phones are connected."})
                conn.close(1013)
                conn.drain()
                return
            self._conns += 1
            self._orphan_since = None
        conn.settimeout(SOCKET_TIMEOUT)
        conn.send_text({"state": self._player.state()})
        try:
            while not conn.closed:
                try:
                    kind, payload = conn.recv()
                except (ws.Closed, OSError, ValueError):
                    break
                if kind != ws.TEXT:
                    continue
                try:
                    message = json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue
                self.on_message(message)
        finally:
            with self._cond:
                self._conns -= 1
                if self._conns == 0:
                    self._orphan_since = self._clock()
            conn.close()
            conn.drain()

    # -- the worker

    def run(self, stopping):
        while not stopping.is_set():
            self.step(wait=0.05)

    def step(self, wait=0.0):
        """One pass: end an orphaned session, then draw if anything changed.
        The unit the tests drive."""
        with self._cond:
            if not self._dirty and wait:
                self._cond.wait(wait)
        if not self.active():
            with self._cond:
                self._shown = False
                self._dims = None
            return
        now = self._clock()
        with self._cond:
            if self._conns == 0 and self._orphan_since is None:
                self._orphan_since = now
            orphaned = (self._conns == 0 and
                        now - self._orphan_since >= GRACE_SECONDS)
            while self._arrivals and now - self._arrivals[0] > 1.0:
                self._arrivals.popleft()
            fresh = self._tel_at is not None and now - self._tel_at < TEL_STALE
            # A sample going stale changes the panel, so it is a change.
            if self._shown and not fresh and self._tel is not None:
                self._tel = None
                self._dirty = True
            too_soon = (self._last_draw is not None and
                        now - self._last_draw < DRAW_INTERVAL)
            draw = self._dirty and not too_soon
            if draw:
                self._dirty = False
            scene = self._scene or parse_scene({"step": "intro"})
            tel, hz = self._tel, len(self._arrivals)
        if orphaned:
            log("calibration: no phone for %ds; stopping", GRACE_SECONDS)
            with self._cond:
                self._orphan_since = None
            if self._stop is not None:
                self._stop()
            return
        if not draw and self._shown:
            return
        self._draw(scene, tel, hz, fresh, now)

    def _size(self):
        if self._dims is None:
            dims = self._player.get_property("osd-dimensions") or {}
            w, h = dims.get("w"), dims.get("h")
            self._dims = (int(w), int(h)) if w and h else (1920, 1080)
        return self._dims

    def _draw(self, scene, tel, hz, fresh, now):
        w, h = self._size()
        reply = self._player.command(
            ["osd-overlay", OVERLAY_ID, "ass-events",
             scene_ass(scene, tel, w, h, hz=hz, fresh=fresh), w, h, 0, False,
             False],
            quiet=True)
        self._last_draw = now
        if reply is None:
            return
        if reply.get("error") not in (None, "success"):
            log("calibration: mpv refused the overlay: %s", reply.get("error"))
            return
        self._shown = True
        self.draws += 1
