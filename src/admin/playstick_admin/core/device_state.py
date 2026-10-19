"""Whether the stick is answering, and whether we are mid-conversation with it.

Every tab that touches the device has, until now, worked this out for itself
and kept the answer to itself. The Device tab polled and wrote "Device: idle"
into its own label; the Library tab discovered the same fact by asking for the
film table and, when that failed, said so in a line about a count; the queue
worker learned it at the end of a two-hour encode and put it in a log. Three
answers to one question, each visible on a different tab, none of them visible
where somebody is looking.

So the answer lives here instead, in one object the tabs share. It is a plain
object with no Qt in it, for the reason the rest of core/ has none: the
interesting parts are a small state machine and the sentence it produces, and
both are worth testing without a display.

WHAT "SYNCING" MEANS, AND WHY IT IS COUNTED RATHER THAN SET

A sync is any moment this machine is WRITING to the stick -- a film being
pushed at the end of a preparation, a recovery push of what was waiting, a
metadata edit, a removal. Reads are not syncs; asking for the film table is how
a tab draws itself, and a spinner every four seconds because something polled
would say nothing.

They nest and they overlap: a queue pushing one film while somebody saves an
edit in the Library tab is two writes at once, from two threads, and the second
one finishing does not mean the first one has. So it is a depth and not a flag.
A flag would clear on the first `end` and leave the indicator claiming quiet
while an upload was still running -- which is exactly the lie the indicator
exists to prevent.

WHAT COUNTS AS EVIDENCE

Anything that talked to the stick. A poll answering is evidence it is there; so
is a film pushing, a poster uploading, an edit saving. A poll is only the
regular source, not the privileged one -- an indicator that stayed green for
fifteen seconds after a push failed, because the last poll was fine, would be
worse than no indicator at all.
"""

import time

UNSET = "unset"         # nobody has said where the stick is
UNKNOWN = "unknown"     # we have not asked yet
ONLINE = "online"       # it answered
OFFLINE = "offline"     # it did not

# How stale an answer may be before "connected" stops being a claim worth
# making. Nothing polls this out of the state itself -- it is only used to
# decide whether to say how long ago, and to keep a very old success from
# reading as a current one.
STALE_AFTER = 60.0


class DeviceState:
    """The shared answer to "is the stick there". Not thread-safe, and not
    meant to be: every caller is a UI-thread slot, because a worker touching it
    would be a widget repainted off the main thread by another name."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._url = ""
        self._state = UNSET
        self._error = ""
        self._last_ok = None
        self._depth = 0
        self._what = ""

    # --- what it is -------------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def url(self):
        return self._url

    @property
    def syncing(self):
        return self._depth > 0

    @property
    def error(self):
        return self._error

    @property
    def last_ok(self):
        return self._last_ok

    def snapshot(self):
        """Everything that shows. Callers compare two of these to decide
        whether anybody needs repainting -- a poll that says the same thing as
        the last poll should not make a label flicker four times a minute."""
        return (self._url, self._state, self._error, self._depth, self._what,
                self._last_ok is not None)

    # --- what happened ----------------------------------------------------

    def set_url(self, url):
        """Point at a different stick, or at none.

        The verdict is dropped with the address, rather than carried over: an
        indicator that stays green for four seconds after somebody changes the
        URL is reporting on a machine they have stopped caring about.
        """
        url = (url or "").rstrip("/")
        if url == self._url:
            return
        self._url = url
        self._state = UNSET if not url else UNKNOWN
        self._error = ""
        self._last_ok = None

    def reached(self):
        """Something got an answer out of it. Any call will do -- see the
        module docstring on evidence."""
        if not self._url:
            return
        self._state = ONLINE
        self._error = ""
        self._last_ok = self._clock()

    def unreachable(self, error=""):
        """Something did not. The message is kept because "not answering" and
        "not answering: HTTP 500" send somebody to different places."""
        if not self._url:
            return
        self._state = OFFLINE
        self._error = str(error or "")

    # --- talking to it ----------------------------------------------------

    def sync_begin(self, what=""):
        self._depth += 1
        if what:
            self._what = what

    def sync_say(self, what):
        """Where a sync in flight has got to. Ignored once it has ended, so a
        progress signal arriving late cannot leave a stale line behind."""
        if self._depth > 0 and what:
            self._what = what

    def sync_end(self, error=None):
        """One write finished. `error` is a shortcut for the caller that would
        otherwise have to remember to report the failure separately."""
        self._depth = max(0, self._depth - 1)
        if error:
            self.unreachable(error)
        if self._depth == 0:
            self._what = ""

    # --- what to show -----------------------------------------------------

    def stale(self):
        """True when the last good answer is old enough that "connected" is a
        guess. Only ever used to decide whether to say when."""
        return (self._last_ok is not None
                and self._clock() - self._last_ok > STALE_AFTER)

    def describe(self):
        """(colour, headline, detail) -- the three things the indicator draws.

        Syncing wins over reachability in the headline on purpose. "Connected"
        and "sending Ponyo" are both true during a push, and only one of them
        is the answer to why the window is busy.
        """
        if self._state == UNSET:
            return ("idle", "No device",
                    "Set the stick's address in Config, Settings.")
        if self.syncing:
            return ("busy", "Syncing with the device",
                    self._what or self._url)
        if self._state == UNKNOWN:
            return ("idle", "Checking the device...", self._url)
        if self._state == ONLINE:
            # How old the answer is, but only once it is old enough to matter.
            # A poll every few seconds saying "as of 4s ago" is noise; an hour
            # -- a laptop that was shut, a monitor that was stopped -- is the
            # difference between a fact and a memory.
            if self.stale():
                return ("ok", "Device reached",
                        "%s (as of %s ago)"
                        % (self._url, ago(self._clock() - self._last_ok)))
            return ("ok", "Device reached", self._url)
        detail = "Not answering"
        if self._error:
            detail += ": %s" % self._error
        if self._last_ok is not None:
            detail += " (last answered %s ago)" % ago(self._clock() - self._last_ok)
        return ("bad", "Device unreachable", detail)

    def line(self):
        """The whole thing on one line, for somewhere with no room for two."""
        _colour, headline, detail = self.describe()
        return "%s -- %s" % (headline, detail) if detail else headline


def ago(seconds):
    """How long, said the way somebody would say it. Seconds up to a minute,
    then minutes, then hours -- nobody reads "4823s"."""
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
