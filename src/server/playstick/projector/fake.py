"""A projector made of arithmetic, for the development GUI.

Not a test double -- the tests have their own, closer to the wire. This is what
`docker compose up gui` runs so that the preparing view can be looked at and
its wording argued about on a laptop, with a lamp that "warms up" in three
seconds instead of forty.

It is deliberately not perfect. It honours the two rules that shape the
sequence -- standby accepts nothing but PON, and the lamp is not on the instant
PON returns -- and ignores everything else, because those two are what the code
above it is written around and the rest would only be scenery.

Set PLAYSTICK_PROJECTOR_MODEL=fake to get one. It is never selected on the
appliance: the Ansible default is the real model, and "fake" is not a value any
template writes.
"""

import threading
import time

from .base import ON, STANDBY, Projector, Refused


class FakeProjector(Projector):
    model = "fake"

    def __init__(self, warmup=3.0, cooldown=2.0, input_code="HD3",
                 clock=time.monotonic):
        self._warmup = warmup
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._input = input_code
        # The instant the lamp finishes striking, or None in standby. Held as a
        # deadline rather than a boolean plus a timer so that power_state is a
        # comparison and this class needs no thread of its own.
        self._on_at = None
        self._off_at = None

    def power_state(self):
        with self._lock:
            if self._on_at is None:
                return STANDBY
            return ON if self._clock() >= self._on_at else STANDBY

    def power_on(self):
        with self._lock:
            if self._off_at is not None and self._clock() < self._off_at:
                raise Refused("still cooling down")
            if self._on_at is None:
                self._on_at = self._clock() + self._warmup

    def power_off(self):
        with self._lock:
            self._on_at = None
            self._off_at = self._clock() + self._cooldown

    def set_input(self, code):
        with self._lock:
            if self._on_at is None:
                raise Refused("in standby the projector accepts only PON")
            self._input = (code or "").upper()

    def current_input(self):
        with self._lock:
            return self._input if self._on_at is not None else ""
