"""Calibration profiles: one per phone, written by the wizard.

A profile is what the calibration wizard (calibrate.ts on the phone,
calibrate.py on the projector) measured for one iPhone: how far the phone
turns and tilts to cross the picture, how hard to tilt it for the d-pad, and
which on-screen button is which. The phone applies it to the pointer and the
gamepad. This end only keeps it, so it survives Safari clearing its storage
and can be picked again from a list.

The phone makes up its own id, once, and keeps it in localStorage. Nothing
here learns who holds the phone; the name is whatever the person typed.

EVERY FIELD IS CHECKED, AND NOTHING ELSE IS KEPT

validate() takes the body the phone sent and returns the profile that will be
stored, or raises ValueError with a sentence. Unknown keys are refused rather
than dropped, so a page from a newer build talking to an older daemon says
so, instead of saving half a profile. The ranges are wide enough for any real
hand and narrow enough that a stored profile cannot make the pointer useless.
"""

import json
import math
import re
import time

from .calibrate import clean_name


ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_PROFILES = 64

BITS = (16, 32, 64, 128)            # A, B, START, SELECT in tilt-pad.ts

# key -> (lo, hi), or "sign" for -1/1.
POINTER = {
    "yaw_across": (10.0, 120.0),
    "pitch_across": (5.0, 90.0),
    "yaw_sign": "sign",
    "pitch_sign": "sign",
    "smoothing": (0.0, 0.9),
    "deadzone": (0.0, 5.0),
}
TILT_PRESS = ("left", "right", "up", "down")
TILT = {
    "release_ratio": (0.3, 0.95),
    "roll_sign": "sign",
    "pitch_sign": "sign",
}
BUTTONS = ("A", "B", "START", "SELECT")


def _number(where, value, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError("%s must be a number from %g to %g." % (where, lo, hi))
    return float(value)


def _sign(where, value):
    if value not in (1, -1) or isinstance(value, bool):
        raise ValueError("%s must be 1 or -1." % where)
    return int(value)


def _object(where, value, allowed):
    if not isinstance(value, dict):
        raise ValueError("%s must be an object." % where)
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise ValueError("%s has keys this stick does not know: %s."
                         % (where, ", ".join(extra)))
    missing = sorted(set(allowed) - set(value))
    if missing:
        raise ValueError("%s is missing %s." % (where, ", ".join(missing)))
    return value


def _fields(where, value, spec):
    out = {}
    for key, rule in spec.items():
        name = "%s.%s" % (where, key)
        out[key] = (_sign(name, value[key]) if rule == "sign"
                    else _number(name, value[key], *rule))
    return out


def validate(body):
    """The profile to store, from a request body. Raises ValueError."""
    if not isinstance(body, dict):
        raise ValueError("The profile must be an object.")
    _object("profile", body, ("name", "pointer", "tilt", "buttons", "checks"))
    name = clean_name(body["name"])
    if not name:
        raise ValueError("The profile needs a name.")

    pointer = _fields("pointer", _object("pointer", body["pointer"], POINTER),
                      POINTER)

    tilt_in = _object("tilt", body["tilt"], ("press",) + tuple(TILT))
    tilt = _fields("tilt", tilt_in, TILT)
    press = _object("tilt.press", tilt_in["press"], TILT_PRESS)
    tilt["press"] = {k: _number("tilt.press." + k, press[k], 4.0, 45.0)
                     for k in TILT_PRESS}

    buttons_in = _object("buttons", body["buttons"], BUTTONS)
    buttons = {}
    for key in BUTTONS:
        bit = buttons_in[key]
        if isinstance(bit, bool) or bit not in BITS:
            raise ValueError("buttons.%s must be one of %s."
                             % (key, ", ".join(map(str, BITS))))
        buttons[key] = int(bit)
    if len(set(buttons.values())) != len(BUTTONS):
        raise ValueError("Two buttons cannot do the same thing.")

    checks_in = _object("checks", body["checks"], ("targets", "buttons_ok"))
    targets = checks_in["targets"]
    if not isinstance(targets, list) or len(targets) > 9:
        raise ValueError("checks.targets must be a list of up to 9 numbers.")
    checks = {
        "targets": [_number("checks.targets", t, 0.0, 2.0) for t in targets],
        "buttons_ok": bool(checks_in["buttons_ok"]),
    }
    return {"name": name, "pointer": pointer, "tilt": tilt,
            "buttons": buttons, "checks": checks}


class Profiles:
    """The phone_profile table. Thin, like everything that sits on Database."""

    def __init__(self, db, clock=time.time):
        self._db = db
        self._clock = clock

    def list(self):
        rows = self._db.query(
            "SELECT id, name, updated_at FROM phone_profile "
            "ORDER BY updated_at DESC")
        return [{"id": r["id"], "name": r["name"],
                 "updated_at": r["updated_at"]} for r in rows]

    def get(self, ident):
        row = self._db.one(
            "SELECT id, name, data, updated_at FROM phone_profile WHERE id = ?",
            (ident,))
        if row is None:
            return None
        profile = json.loads(row["data"])
        profile.update(id=row["id"], name=row["name"],
                       updated_at=row["updated_at"])
        return profile

    def save(self, ident, profile):
        """Store a validated profile. Returns it as get() would. Raises
        ValueError if this would be one profile too many."""
        conn = self._db.write()
        with conn:
            have = conn.execute(
                "SELECT COUNT(*) FROM phone_profile WHERE id != ?",
                (ident,)).fetchone()[0]
            if have >= MAX_PROFILES:
                raise ValueError("This stick already keeps %d profiles."
                                 % MAX_PROFILES)
            data = {k: v for k, v in profile.items() if k != "name"}
            conn.execute(
                "INSERT INTO phone_profile (id, name, data, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "name = excluded.name, data = excluded.data, "
                "updated_at = excluded.updated_at",
                (ident, profile["name"], json.dumps(data), self._clock()))
        return self.get(ident)
