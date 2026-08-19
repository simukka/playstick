"""The projector that is not there.

This exists so that no call site in the daemon ever has to ask whether a
projector is configured. `open_projector("")` returns one of these, the
sequence in projectionist.py runs against it unchanged, every step is a no-op
that takes no time, and a film starts exactly as it did before this feature
existed.

The alternative -- a module-level PROJECTOR that may be None, and an `if` in
front of every use -- puts the same question in five places and gets it wrong
in one of them eventually. It also makes the no-projector path the one nobody
tests, when it is in fact the path the development GUI runs on and the path
every appliance runs on until somebody plugs a cable in.

power_state() answers UNKNOWN rather than STANDBY on purpose. STANDBY would be
a claim about a projector this object has never spoken to, and the keeper in
projectionist.py would then dutifully try to switch on something that is not
listening, once per idle tick, forever.
"""

from .base import UNKNOWN, Projector


class NullProjector(Projector):
    model = "none"

    def power_state(self):
        return UNKNOWN

    def power_on(self):
        pass

    def power_off(self):
        pass

    def set_input(self, code):
        pass

    def current_input(self):
        return ""
