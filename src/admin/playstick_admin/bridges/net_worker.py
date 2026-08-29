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
"""

from PySide6.QtCore import QObject, QThread, Signal


class _CallJob(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    finished = Signal()

    def __init__(self, fn, wants_progress=False):
        super().__init__()
        self._fn = fn
        self._wants_progress = wants_progress

    def run(self):
        try:
            result = (self._fn(self.progress.emit) if self._wants_progress
                      else self._fn())
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.done.emit(result)
        finally:
            self.finished.emit()


def _spawn(job):
    thread = QThread()
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.finished.connect(thread.quit)
    job.finished.connect(job.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return job, thread


def call(fn):
    """Returns (job, thread); connect job.done/job.failed then thread.start()."""
    return _spawn(_CallJob(fn))


def call_with_progress(fn):
    """The same, for a callable taking one argument: something to report to.

    `fn(say)` is handed a `say(text)` it may call as often as it likes; each
    call arrives on the UI thread as job.progress.
    """
    return _spawn(_CallJob(fn, wants_progress=True))
