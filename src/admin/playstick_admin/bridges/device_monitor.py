"""One poll of the stick, shared by every tab that cares.

The Device tab has always polled /api/status on a timer, because it is a remote
control and a remote control has to know whether the film is paused. What is
new is that the ANSWER is worth more than that tab: whether the stick replied
at all is the fact the Library tab needs to explain an empty table, and the one
somebody wants while a queue is pushing.

So the poll moved here and the verdict goes into a shared core.DeviceState. The
Device tab still gets the whole status dict -- it is the only thing that wants
the volume and the projector -- but it no longer owns the timer, and no longer
is the only place the answer appears.

TWO CADENCES

Fast while the Device tab is showing, because playback state changes under
somebody's finger and a four-second-old "paused" is wrong in a way people
notice. Slow otherwise, because nothing else on screen changes that quickly and
a GET every four seconds forever, for a window that might be left open all
weekend, is a lot of nothing.

WHY THE POLL DOES NOT STACK

A stick that is switched off does not refuse the connection, it fails to answer,
and the client waits out its whole timeout before saying so. That is longer than
the interval, so a timer that started a call regardless would have four of them
in flight against a machine that is not there. One at a time, and the timer
skips a beat rather than queueing one.
"""

import logging

from PySide6.QtCore import QObject, QTimer, Signal

from ..core import device_state
from ..core.device_client import DeviceClient
from . import net_worker

LOG = logging.getLogger(__name__)


class DeviceMonitor(QObject):
    # Anything on the indicator changed: reached, lost, or a sync started or
    # finished. Deliberately carries nothing -- every listener reads the state.
    changed = Signal()
    # A poll came back. For the Device tab, which wants the detail.
    status = Signal(dict)

    def __init__(self, settings, active_ms=4000, idle_ms=20000, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._state = device_state.DeviceState()
        self._active_ms = active_ms
        self._idle_ms = idle_ms
        self._inflight = False
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.poll)
        self._timer.setInterval(idle_ms)
        self.refresh_url()

    @property
    def state(self):
        """The shared verdict. Read it; the mutators below are how it moves."""
        return self._state

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        if self._settings.device_url:
            self.poll()
            self._timer.start()

    def stop(self):
        self._timer.stop()

    def set_active(self, active):
        """Faster while somebody is watching the remote control."""
        self._timer.setInterval(self._active_ms if active else self._idle_ms)
        if active:
            self.poll()

    def refresh_url(self):
        """Called after Settings is closed. A changed address drops the old
        verdict with it -- see DeviceState.set_url."""
        before = self._state.snapshot()
        self._state.set_url(self._settings.device_url)
        if self._settings.device_url:
            self._timer.start()
        else:
            self._timer.stop()
        self._emit(before)

    # --- the poll ----------------------------------------------------------

    def poll(self):
        if self._inflight or not self._settings.device_url:
            return
        self._inflight = True
        url = self._settings.device_url
        job, thread = net_worker.call(lambda: DeviceClient(url).status(),
                                      label="device-poll")
        job.done.connect(self._on_status)
        job.failed.connect(self._on_failed)
        job.finished.connect(self._done)
        thread.start()

    def _done(self):
        self._inflight = False

    def _on_status(self, status):
        self.note_reached()
        self.status.emit(status if isinstance(status, dict) else {})

    def _on_failed(self, message):
        self.note_unreachable(message)

    # --- evidence from everywhere else -------------------------------------
    #
    # Every one of these is called from a UI-thread slot. A worker calling them
    # directly would be repainting a label from the wrong thread with extra
    # steps -- connect its signal here instead.

    def note_reached(self):
        before = self._state.snapshot()
        self._state.reached()
        self._emit(before)

    def note_unreachable(self, message=""):
        before = self._state.snapshot()
        self._state.unreachable(message)
        self._emit(before)

    def sync_begin(self, what=""):
        before = self._state.snapshot()
        self._state.sync_begin(what)
        self._emit(before)

    def sync_say(self, what):
        before = self._state.snapshot()
        self._state.sync_say(what)
        self._emit(before)

    def sync_end(self, error=None):
        """The other half of every sync_begin.

        Pass the error when there was one and the indicator goes red at the
        moment it happened, rather than staying green until the next poll
        notices. Ending WITHOUT an error claims nothing either way, which is
        the right answer for the caller that cannot tell -- a job that failed
        at some step after the push, say. A caller that knows it worked should
        say so: that is `sync_done`, and it is the freshest evidence there is.
        """
        before = self._state.snapshot()
        self._state.sync_end(error)
        self._emit(before)

    def sync_done(self):
        """It finished, and it worked. Green, from this moment."""
        before = self._state.snapshot()
        self._state.sync_end()
        self._state.reached()
        self._emit(before)

    def _emit(self, before):
        """Repaint, and say so once, on the edge.

        The transition is the whole line worth logging: the poll itself is
        already a DEBUG line in the client, and "still there" every four
        seconds is not news. "It stopped answering" and "it came back after
        eleven minutes" are.
        """
        if self._state.snapshot() == before:
            return
        was, now = before[1], self._state.state
        if now != was:
            self._log_transition(was, now)
        self.changed.emit()

    def _log_transition(self, was, now):
        if now == device_state.ONLINE:
            since = self._state.last_ok
            LOG.info("the stick is answering%s",
                     "" if was == device_state.UNKNOWN or since is None
                     else " again")
        elif now == device_state.OFFLINE:
            LOG.warning("the stick stopped answering: %s",
                        self._state.error or "no reason given")
        elif now == device_state.UNSET:
            LOG.info("no device address is set")
        else:
            LOG.info("checking %s", self._state.url)
