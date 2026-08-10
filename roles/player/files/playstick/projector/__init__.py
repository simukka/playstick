"""Choosing a projector driver, and refusing to fail while doing it.

Adding a projector is a file in this directory and a line in MODELS. Nothing
else in the daemon names a model, an input code or a baud rate.

EVERY WAY THIS CAN GO WRONG RETURNS A NullProjector

An unknown model, a missing device path, a port that will not open -- all of
them log one line and hand back the projector that does nothing. That is the
same judgement library.py makes about a corrupt index: a stale index must never
be the reason a child cannot watch a film, and neither must a serial cable.
The daemon is a movie player that can also switch a projector on, not a
projector controller that also plays films, and the failure modes should say
so.

The one thing that is NOT decided here is whether a projector is wanted at all.
An empty model means "none", which is the default everywhere until somebody
sets player_projector_model in Ansible -- so an appliance with no cable behaves
exactly as it did before this feature existed, and so does the development GUI.
"""

import time

from ..config import log
from .base import (
    ON, STANDBY, UNKNOWN, BadReply, NoReply, Projector, ProjectorError,
    Refused, Unreachable,
)
from .fake import FakeProjector
from .null import NullProjector
from .panasonic import PanasonicSerial


# Model name -> (factory, needs a device path). The two Panasonic names are the
# same driver: the AE3000U and the AE4000 differ only in which inputs answer
# ER401, and the driver offers both sets and lets the projector decide.
MODELS = {
    "pt-ae4000": (PanasonicSerial, True),
    "pt-ae3000u": (PanasonicSerial, True),
    "fake": (FakeProjector, False),
}


def open_projector(model, device="", timeout=1.5, clock=time.monotonic):
    """A Projector for `model`, or a NullProjector if there cannot be one.

    Note that nothing is opened here. The serial port is opened lazily on the
    first command, because this runs during daemon startup and a projector that
    is unplugged at boot must not delay -- or prevent -- the web UI coming up.
    """
    name = (model or "").strip().lower()
    if not name or name == "none":
        return NullProjector()

    entry = MODELS.get(name)
    if entry is None:
        log("unknown projector model %r; the daemon knows %s. Carrying on "
            "without one.", model, ", ".join(sorted(MODELS)))
        return NullProjector()

    factory, needs_device = entry
    if needs_device and not device:
        log("projector model %r needs a device path and none is set; carrying "
            "on without one.", name)
        return NullProjector()

    if not needs_device:
        return factory(clock=clock)
    projector = factory(device, timeout=timeout, clock=clock)
    log("projector %s on %s", projector.model, device)
    return projector


__all__ = [
    "BadReply", "FakeProjector", "MODELS", "NoReply", "NullProjector", "ON",
    "PanasonicSerial", "Projector", "ProjectorError", "Refused", "STANDBY",
    "UNKNOWN", "Unreachable", "open_projector",
]
