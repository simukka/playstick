"""What the daemon needs from a projector, and the settings a grown-up may change.

Five verbs and one question, which is what starting a film needs. Beside them,
two optional methods -- settings() and change() -- for the projector settings
view behind the logo's menu. They are not part of what the daemon relies on:
the base class answers "no settings", so a driver for the next projector can
still be a short file and add its settings later, or never.

Settings are DATA, not methods. A driver describes each one -- a name, the
words to show, and the values it takes -- and the view draws whatever list it
gets. Adding a projector's colour mode is then a row in that driver's table
rather than a route, a client method and a widget.

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

    # -- the optional settings, for the settings view

    def settings(self):
        """Every setting this driver offers, each with its current value:

            {"name": "picture", "label": "Picture mode", "value": "CN1",
             "choices": [{"value": "NOR", "label": "Normal"}, ...]}

        "value" is "" when the projector would not say. Empty by default: not
        offering settings is a complete answer, unlike the power verbs above,
        which a driver has to implement to be a driver at all.
        """
        return []

    def change(self, name, value):
        """Set one setting, and return only once the projector confirms it.

        Raises Refused for a name or value the driver does not offer, and for
        a value the projector did not take."""
        raise Refused("this projector has no setting %r" % (name,))
