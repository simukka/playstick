"""Run the preparer on a worker thread and surface its progress as Qt signals.

The preparer keeps its run state in module globals -- one reporter, one cancel
flag -- so only one run may be in flight at a time. PrepWorker owns that: it
installs the reporter for the duration of a run and clears it at the end, and it
never starts a second run while one is going.
"""

from PySide6.QtCore import QObject, QThread, Signal

from .. import paths

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402


class PrepWorker(QObject):
    logLine = Signal(str)
    runStarted = Signal(int)        # total films to prepare
    filmStarted = Signal(dict)      # {number, total, id, title}
    filmDone = Signal(dict)         # {number, total, id, title, failed}
    runFinished = Signal(dict)      # {indexed, prepared, ...} or {"error": msg}
    finished = Signal()             # thread may be torn down after this

    def __init__(self, argv, ffmpeg="", ffprobe=""):
        super().__init__()
        self._argv = list(argv)
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    def run(self):
        prep.reset_cancel()
        if self._ffmpeg:
            prep.FFMPEG = self._ffmpeg
        if self._ffprobe:
            prep.FFPROBE = self._ffprobe
        prep.set_reporter(self._on_event)
        try:
            code = prep.main(self._argv, install_handlers=False)
        except SystemExit as exc:
            # die() raises SystemExit(1) on a bad argument or a missing ffmpeg.
            self.runFinished.emit({"error": str(exc.code or "preparation failed")})
        except Exception as exc:                        # pragma: no cover - defensive
            self.runFinished.emit({"error": repr(exc)})
        else:
            if code not in (0, 130):
                self.runFinished.emit({"error": "exit code %s" % code})
        finally:
            prep.set_reporter(None)
            self.finished.emit()

    def cancel(self):
        """Ask the run to stop at the next film boundary. Safe from any thread;
        it sets a flag and ends the current ffmpeg, which the run notices."""
        prep.request_cancel()

    def _on_event(self, ev):
        kind = ev.get("event")
        if kind == "log":
            self.logLine.emit(ev.get("text", ""))
        elif kind == "run_start":
            self.runStarted.emit(int(ev.get("total", 0)))
        elif kind == "film_start":
            self.filmStarted.emit(ev)
        elif kind == "film_done":
            self.filmDone.emit(ev)
        elif kind == "run_done":
            self.runFinished.emit(ev)


def start_prep(argv, ffmpeg="", ffprobe=""):
    """Build a worker on its own thread. Returns (worker, thread); connect the
    worker's signals, then call thread.start(). The thread quits and both are
    scheduled for deletion when the run finishes."""
    thread = QThread()
    worker = PrepWorker(argv, ffmpeg, ffprobe)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    return worker, thread
