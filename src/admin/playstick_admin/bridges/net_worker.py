"""Run a blocking callable on a worker thread and deliver its result as a signal.

Used for device HTTP calls so a slow or unreachable stick never freezes the UI.
"""

from PySide6.QtCore import QObject, QThread, Signal


class _CallJob(QObject):
    done = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.done.emit(result)
        finally:
            self.finished.emit()


def call(fn):
    """Returns (job, thread); connect job.done/job.failed then thread.start()."""
    thread = QThread()
    job = _CallJob(fn)
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.finished.connect(thread.quit)
    job.finished.connect(job.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return job, thread
