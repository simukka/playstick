"""What the daemon needs from a projector, and deliberately nothing else.

Five verbs and one question. Everything a projector can do that this appliance
does not need -- lens memories, gamma curves, colour management, the whole menu
tree -- is absent, because a driver for the next projector should be a short
file rather than a translation of somebody else's manual.

THE ERROR TYPES ARE THE INTERESTING PART

They exist because the caller reacts to them differently, not because the
distinction is tidy. `Refused` means the projector heard the command and said
no, which is a fact about this model -- an input it does not have, a command it
does not know -- and repeating it will not help. `NoReply` and `Unreachable`
mean the cable or the projector is not there, which may well be temporary and
is worth retrying on the next tick. All three are ProjectorError, because the
sequence in projectionist.py steps over every one of them and starts the film
anyway.
"""


# The three answers to "is the lamp lit". UNKNOWN is not a failure -- it is
# what a NullProjector always says, and what a real one says when it answers
# with something the manual does not list. The caller has to handle it either
# way, so making it a state rather than an exception keeps that path honest.
STANDBY = "standby"
ON = "on"
UNKNOWN = "unknown"


class ProjectorError(Exception):
    """The projector could not be reached, or refused a command."""


class Unreachable(ProjectorError):
    """The serial port could not be opened, or failed mid-exchange."""


class NoReply(ProjectorError):
    """Bytes went out; no ETX-terminated frame came back in time."""


class Refused(ProjectorError):
    """The projector answered ER401: it does not accept that command."""


class BadReply(ProjectorError):
    """A frame arrived, but not one this command can make sense of."""


class Projector:
    """The interface. Subclasses implement it; NullProjector opts out of it.

    Every method here raises rather than returning a plausible-looking default,
    so a half-written driver fails loudly during development instead of quietly
    reporting UNKNOWN forever on somebody's ceiling.
    """

    # Shown in /api/status so the page and the journal can say which driver is
    # loaded without the caller having to know the class names.
    model = "none"

    def power_state(self):
        """STANDBY, ON, or UNKNOWN. Never raises for an unrecognised answer."""
        raise NotImplementedError

    def power_on(self):
        raise NotImplementedError

    def power_off(self):
        raise NotImplementedError

    def set_input(self, code):
        raise NotImplementedError

    def current_input(self):
        """The input code the projector says it is on, or "" if it will not
        say. Named for `Player.current_title` rather than for the protocol, and
        not `input` because that is a builtin worth not shadowing."""
        raise NotImplementedError

    def close(self):
        """Release the port. A no-op by default: a driver with nothing to
        release should not have to say so."""
