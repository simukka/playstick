"""Run a blocking callable on a worker thread and deliver its result as a signal.

Used for device HTTP calls so a slow or unreachable stick never freezes the UI.

A job may also SAY WHERE IT HAS GOT TO while it runs -- see `call_with_progress`.
That exists for the one call here that is not instant: a first sync of a few
hundred films uploads a poster each, and a button that goes grey for half a
minute with no other sign is one people press again.

The progress channel is a signal rather than a callback the worker invokes
directly, and that is the whole point of it. Qt queues a signal emitted across
a thread boundary onto the receiver's own event loop, so the widget that
displays it is only ever touched by the thread that owns it. A worker calling
`label.setText` would appear to work and would be a data race.

The job is kept alive by bridges.lifetime rather than by the caller, which is
not a detail -- see that module for what a collected job looks like from the
outside.

WHAT THE CALLER CONNECTS TO IS NOT THE JOB

It is a relay that lives on the caller's thread, and that is the difference
between this module working and the app segfaulting. PySide6 runs a LAMBDA
connected to a signal on whichever thread emitted it -- only a bound method of
a QObject is queued to that object's thread. So `job.failed.connect(lambda msg:
self._log.appendPlainText(msg))`, which reads as an ordinary handler, edited a
widget from the worker thread, and the next paint of it crashed inside the font
engine. A failed scan did that every time.

The relay's own signals are re-emitted on the UI thread, where the job's are
delivered to it as queued calls -- so every slot connected to them, a lambda
included, runs where widgets may be touched. Fixing it here rather than at each
call site means the next lambda somebody connects is safe without anybody
having to know why.
"""

import logging
import time

from PySide6.QtCore import QObject, QThread, Signal

from . import lifetime
from ..core.device_client import DeviceError

LOG = logging.getLogger(__name__)


class _CallJob(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    finished = Signal()

    def __init__(self, fn, wants_progress=False, label="net"):
        super().__init__()
        self._fn = fn
        self._wants_progress = wants_progress
        self._label = label

    def run(self):
        lifetime.name_thread(self._label)
        started = time.monotonic()
        try:
            result = (self._fn(self.progress.emit) if self._wants_progress
                      else self._fn())
        except DeviceError as exc:
            # Already logged by the client, which knows the method and the path
            # and this does not. A second line here would double every failure
            # against a stick that is switched off.
            LOG.debug("%s gave up after %.1fs: %s", self._label,
                      time.monotonic() - started, exc)
            self.failed.emit(str(exc))
        except Exception as exc:
            # Anything else got this far unlogged, and a traceback is the only
            # thing that will say where it came from.
            LOG.warning("%s failed after %.1fs: %s", self._label,
                        time.monotonic() - started, exc, exc_info=True)
            self.failed.emit(str(exc))
        else:
            LOG.debug("%s finished in %.1fs", self._label,
                      time.monotonic() - started)
            self.done.emit(result)
        finally:
            self.finished.emit()


# Relays waiting to deliver. See _Relay._finish for why they cannot simply ride
# on lifetime.hold with the job.
_RELAYS = set()


class _Relay(QObject):
    """The job's signals, re-emitted on the thread that created the relay."""

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    finished = Signal()

    def __init__(self, job):
        super().__init__()
        # Signal to signal: the receiver is this object, which lives on the
        # caller's thread, so each of these is a queued connection.
        job.done.connect(self.done)
        job.failed.connect(self.failed)
        job.progress.connect(self.progress)
        job.finished.connect(self._finish)
        _RELAYS.add(self)

    def _finish(self):
        # Held until here rather than until the thread finishes. The queued
        # result arrives on this thread AFTER the worker's thread has wound
        # down, and a relay dropped when the thread finished would be deleted
        # with the result still in its queue -- which Qt discards silently.
        # `finished` is the last thing the job emits, so by now done or failed
        # has already been delivered.
        try:
            self.finished.emit()
        finally:
            _RELAYS.discard(self)


def _spawn(job):
    relay = _Relay(job)
    thread = QThread()
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.finished.connect(thread.quit)
    job.finished.connect(job.deleteLater)
    thread.finished.connect(thread.deleteLater)
    lifetime.hold(job, thread)
    return relay, thread


def call(fn, label="net"):
    """Returns (job, thread); connect job.done/job.failed then thread.start().

    `job` is the relay: its signals arrive on the calling thread, so a lambda
    connected to them may touch widgets.

    `label` names the thread in the log and in the line this writes when the
    call fails, so a warning in the middle of an encode says which of the two
    or three things in flight produced it. It costs one word at the call site
    and is the difference between a readable log and a list of timestamps.
    """
    return _spawn(_CallJob(fn, label=label))


def call_with_progress(fn, label="net"):
    """The same, for a callable taking one argument: something to report to.

    `fn(say)` is handed a `say(text)` it may call as often as it likes; each
    call arrives on the UI thread as job.progress.
    """
    return _spawn(_CallJob(fn, wants_progress=True, label=label))
