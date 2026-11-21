"""What is plugged in, for the admin app to fill its projector settings from.

The three projector variables -- model, device, input -- are typed into the
admin app by somebody standing in front of a projector with a manual, and every
way of getting one wrong fails quietly by design (see __init__.py). This answers
the two of them that the stick can find out for itself, so they are picked
rather than typed:

  * WHICH SERIAL PORT. Every USB serial adapter under /dev/serial/by-id, FTDI
    first -- the same order the Ansible discovery task and
    scripts/projector-probe.py use, so "the suggestion" and "what an empty
    player_projector_device would have found" are the same port.
  * WHICH MODEL. Read from the EDID the projector sends down the HDMI cable,
    which names it ("AE-4000") without a byte going over the serial line. If
    that says nothing useful, a port that answers the Panasonic framing at all
    is enough to suggest the driver, since both names are the same driver.

ONLY QUESTIONS GO OUT

QPW and QIN, nothing else. A probe that sent PON, POF or IIS would switch a
projector on in an empty room, or change its input in the middle of somebody
else's film, because a settings form was opened. Neither inquiry changes
anything on the projector.

THE PORT THE DAEMON ALREADY DRIVES IS ASKED THROUGH THE DAEMON'S OWN DRIVER

The keeper in projectionist.py polls it every tick. Opening a second fd on the
same tty and exchanging frames on both would interleave replies, and each side
would read the other's answer. The live PanasonicSerial serialises exchanges
under its own lock, so going through it is what keeps the probe and the keeper
from corrupting each other.

Nothing here raises. A port that will not open, a projector in standby that
ignores QIN, a missing /sys/class/drm -- each becomes a field in the answer,
because the caller is a form that wants to show what it could find.
"""

import glob
import os
import re

from .base import ProjectorError
from .panasonic import PanasonicSerial


# Real models only: "fake" is for the development GUI and is never written by
# a template, so offering it in a settings form would be offering a mistake.
MODEL_LABELS = (
    ("pt-ae4000", "Panasonic PT-AE4000"),
    ("pt-ae3000u", "Panasonic PT-AE3000U"),
)

# PanasonicSerial.INPUTS, in the order somebody looking for "HDMI 3" expects.
INPUT_LABELS = (
    ("HD1", "HDMI 1"),
    ("HD2", "HDMI 2"),
    ("HD3", "HDMI 3"),
    ("CP1", "Component 1"),
    ("CP2", "Component 2 (PT-AE3000U only)"),
    ("VID", "Composite video"),
    ("SVD", "S-video"),
    ("RG1", "Computer (PT-AE4000 only)"),
)

# What the EDID monitor name says -> the driver name. Matched loosely: the
# AE4000 sends "AE-4000", and nothing guarantees a sibling uses the same dash.
_EDID_MODELS = (
    (re.compile(r"AE-?4000", re.I), "pt-ae4000"),
    (re.compile(r"AE-?3000", re.I), "pt-ae3000u"),
)


def serial_ports(by_id="/dev/serial/by-id", dev="/dev"):
    """Every USB serial port, stable by-id names first, FTDI first among them.

    A ttyUSB/ttyACM with no by-id link is listed too, after the rest: an empty
    by-id directory usually means udev is not populating it rather than that
    the adapter is absent, and a port the form cannot offer is a port nobody
    can pick.
    """
    linked = sorted(glob.glob(os.path.join(by_id, "*")))
    linked.sort(key=lambda p: "FTDI" not in os.path.basename(p))
    ports, seen = [], set()
    for path in linked:
        target = os.path.realpath(path)
        seen.add(target)
        ports.append({"path": path, "target": os.path.basename(target),
                      "ftdi": "FTDI" in os.path.basename(path)})
    for pattern in ("ttyUSB*", "ttyACM*"):
        for path in sorted(glob.glob(os.path.join(dev, pattern))):
            if os.path.realpath(path) in seen:
                continue
            ports.append({"path": path, "target": os.path.basename(path),
                          "ftdi": False})
    return ports


def edid_name(edid):
    """The monitor name (descriptor tag 0xFC) from an EDID base block, or "".

    Four 18-byte descriptors at offset 54. A display descriptor starts with two
    zero bytes; the name is up to thirteen bytes after the five-byte header,
    ended by 0x0A and padded with spaces.
    """
    if len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return ""
    for offset in (54, 72, 90, 108):
        block = edid[offset:offset + 18]
        if block[0:2] == b"\x00\x00" and block[3] == 0xFC:
            text = block[5:18].split(b"\x0a", 1)[0]
            return text.decode("ascii", "replace").strip()
    return ""


def displays(drm="/sys/class/drm"):
    """Every DRM connector with an EDID, and the name it gives."""
    found = []
    for edid_path in sorted(glob.glob(os.path.join(drm, "card*-*", "edid"))):
        connector = os.path.basename(os.path.dirname(edid_path))
        try:
            with open(edid_path, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if not data:
            continue
        status = ""
        try:
            with open(os.path.join(os.path.dirname(edid_path), "status")) as fh:
                status = fh.read().strip()
        except OSError:
            pass
        found.append({"connector": connector, "name": edid_name(data),
                      "connected": status == "connected"})
    return found


def guess_model(names):
    """A driver name for the first monitor name that matches one, or ""."""
    for name in names:
        for pattern, model in _EDID_MODELS:
            if name and pattern.search(name):
                return model
    return ""


def _same_port(a, b):
    return bool(a and b) and os.path.realpath(a) == os.path.realpath(b)


def ask(path, live=None, timeout=1.5, open_projector=PanasonicSerial):
    """QPW, then QIN if that answered. Never anything that changes state.

    `live` is the daemon's own projector; it is used instead of opening the
    port when it is a Panasonic driver on this same path -- see the module
    docstring for why. `open_projector` is the seam the tests use.
    """
    result = {"answers": False, "power": "", "input": "", "error": ""}
    shared = (isinstance(live, PanasonicSerial)
              and _same_port(getattr(live, "_device", ""), path))
    projector = live if shared else None
    try:
        if projector is None:
            projector = open_projector(path, timeout=timeout)
        result["power"] = projector.power_state()
        result["answers"] = True
        # In standby the projector ignores everything but PON, QIN included;
        # current_input() turns that silence into "" rather than a fault.
        result["input"] = projector.current_input()
    except ProjectorError as exc:
        result["error"] = str(exc)
    finally:
        if projector is not None and not shared:
            projector.close()
    return result


def probe(live=None, configured=None, timeout=1.5, by_id="/dev/serial/by-id",
          dev="/dev", drm="/sys/class/drm", ask_port=ask):
    """Everything the admin app's projector settings can be filled from."""
    configured = dict(configured or {})
    ports = []
    for port in serial_ports(by_id, dev):
        port.update(ask_port(port["path"], live=live, timeout=timeout))
        ports.append(port)
    screens = displays(drm)

    answered = [p for p in ports if p["answers"]]
    if answered:
        device = answered[0]["path"]
    else:
        device = next((p["path"] for p in ports if p["ftdi"]), "")
    model = guess_model([d["name"] for d in screens if d["connected"]]) \
        or guess_model([d["name"] for d in screens])
    if not model and answered:
        # Both names are one driver; the framing answering is the evidence.
        model = MODEL_LABELS[0][0]
    # The input the projector is showing right now, if its lamp is lit. It is
    # the stick's input only if the stick is what is on screen, which is why
    # the admin app reports it rather than selecting it.
    lit = next((p for p in answered if p["power"] == "on" and p["input"]), None)

    return {
        "configured": {"model": configured.get("model", ""),
                       "device": configured.get("device", ""),
                       "input": configured.get("input", "")},
        "models": [{"id": m, "label": label} for m, label in MODEL_LABELS],
        "inputs": [{"id": c, "label": label} for c, label in INPUT_LABELS],
        "ports": ports,
        "displays": screens,
        "suggested": {"model": model, "device": device,
                      "input": lit["input"] if lit else ""},
    }
