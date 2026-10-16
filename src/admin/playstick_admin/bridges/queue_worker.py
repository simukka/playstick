"""The queue, drained on one worker thread, and reported as Qt signals.

ONE AT A TIME, AND WHY IT IS NOT A POOL

The preparer keeps its run state in module globals -- one reporter, one cancel
flag, one pair of ffmpeg paths -- so two runs in this process would share a
cancel flag and interleave their logs. That constraint is real, but it is not
the reason for the design: libx264 already spreads a single encode across every
core, so a second concurrent one mostly costs cache and makes both slower; two
encodes reading twenty gigabytes each over the same mount contend for the
network rather than the CPU; and a log of two films interleaved line by line is
unreadable exactly when somebody is trying to work out which one went wrong.

The claim is taken in the database rather than guarded by a flag here, so that
the constraint survives a worker that was started twice.

WHAT SURVIVES A CLOSED WINDOW

Everything. The queue is rows, the running job is a row, and its log is a
column. Closing the app mid-encode loses the encode in progress and nothing
else -- the next start puts that job back on the queue and carries on. That is
the whole reason the queue is persisted, and it is what makes a queue of twenty
films something somebody will actually leave running.
"""

import logging
import time
import traceback

from PySide6.QtCore import QObject, QThread, Signal

from . import lifetime
from .. import paths
from ..core import catalog, nas_store, pipeline, queue
from ..core.device_client import DeviceClient

paths.ensure_prep_on_path()
import playstick_prep as prep  # noqa: E402

# How much of a job's log to keep. A failure is worth reading tomorrow; an
# ffmpeg run that printed a warning per frame is not worth a megabyte a row.
LOG_LIMIT = 64_000

LOG = logging.getLogger(__name__)


class QueueWorker(QObject):
    jobStarted = Signal(dict)       # {id, item_id, title, remaining}
    jobPhase = Signal(str, str)     # phase, detail
    jobLog = Signal(str)
    jobDone = Signal(dict)          # what pipeline.prepare returned
    jobFailed = Signal(dict)        # {item_id, title, error}
    queueChanged = Signal()
    finished = Signal()             # the thread may be torn down after this

    def __init__(self, db, settings):
        super().__init__()
        self._db = db
        self._settings = settings
        self._stopping = False
        self._log = []

    # --- the loop ----------------------------------------------------------

    def run(self):
        lifetime.name_thread("queue")
        LOG.info("the queue is draining; %d film(s) waiting",
                 len(queue.pending(self._db)))
        prep.reset_cancel()
        if self._settings.ffmpeg:
            prep.FFMPEG = self._settings.ffmpeg
        if self._settings.ffprobe:
            prep.FFPROBE = self._settings.ffprobe
        prep.set_reporter(self._on_prep_event)
        try:
            while not self._stopping:
                job = queue.claim_next(self._db)
                if job is None:
                    break
                self.queueChanged.emit()
                self._run_one(job)
        except Exception:                       # pragma: no cover - defensive
            # A crash in the loop itself must not leave a job marked running
            # forever, and must not disappear into a dead thread.
            LOG.exception("the queue loop stopped on an error")
            self.jobLog.emit(traceback.format_exc())
        finally:
            prep.set_reporter(None)
            LOG.info("the queue is empty; nothing else is being prepared")
            self.queueChanged.emit()
            self.finished.emit()

    def _run_one(self, job):
        self._log = []
        item = catalog.get_joined(self._db, job["item_id"])
        title = (item["title"] if item else None) or job["item_id"]
        remaining = len(queue.pending(self._db))
        self.jobStarted.emit({"id": job["id"], "item_id": job["item_id"],
                              "title": title, "remaining": remaining})

        if item is None:
            # The row went away between being queued and being reached -- a
            # source removed, most likely. Not worth stopping the queue for.
            LOG.warning("job %s: %s is no longer in the catalogue",
                        job["id"], job["item_id"])
            queue.finish(self._db, job["id"], catalog.MISSING,
                         error="the catalogue no longer has this film")
            self.jobFailed.emit({"item_id": job["item_id"], "title": title,
                                 "error": "no longer in the catalogue"})
            return

        client = (DeviceClient(self._settings.device_url)
                  if self._settings.device_url else None)
        started = time.time()
        try:
            result = pipeline.prepare(self._db, item, self._settings, client,
                                      report=self._report)
        except pipeline.PrepareError as exc:
            self._fail(job, title, str(exc))
        except Exception as exc:                # pragma: no cover - defensive
            LOG.exception("job %s (%s) stopped on an unexpected error",
                          job["id"], title)
            self._log.append(traceback.format_exc())
            self._fail(job, title, repr(exc))
        else:
            result["seconds"] = round(time.time() - started, 1)
            queue.finish(self._db, job["id"], catalog.READY,
                         log=self._log_text())
            self.jobDone.emit(result)
        finally:
            self.queueChanged.emit()

    def _fail(self, job, title, message):
        queue.finish(self._db, job["id"], catalog.FAILED, error=message,
                     log=self._log_text())
        self.jobLog.emit("--- %s failed: %s ---" % (title, message))
        self.jobFailed.emit({"item_id": job["item_id"], "title": title,
                             "error": message})

    # --- control -----------------------------------------------------------

    def cancel_current(self):
        """Stop the film being worked on now, at the next point the preparer
        checks. Safe from any thread: it sets a flag and ends the current
        ffmpeg, which the run notices."""
        LOG.info("cancelling the film being prepared now")
        prep.request_cancel()

    def stop(self):
        """Finish the current film and then stop. The queue keeps its place."""
        LOG.info("stopping after this film")
        self._stopping = True

    # --- reporting ---------------------------------------------------------

    def _report(self, phase, text=""):
        self.jobPhase.emit(phase, text)
        if text:
            self._append("[%s] %s" % (phase, text))

    def _on_prep_event(self, event):
        # Deliberately NOT logged. prep.log already writes every one of these
        # to stderr itself, on the same stream this app logs to, so logging
        # them again would double every line of an encode -- and the ffmpeg
        # progress line, which redraws itself in place, would be interleaved
        # with its own copy.
        if event.get("event") == "log":
            self._append(event.get("text", ""))

    def _append(self, line):
        self._log.append(line)
        self.jobLog.emit(line)

    def _log_text(self):
        text = "\n".join(self._log)
        if len(text) <= LOG_LIMIT:
            return text
        # The end, not the beginning: the error is at the bottom.
        return "...(trimmed)...\n" + text[-LOG_LIMIT:]


def start(db, settings):
    """A worker on its own thread. Returns (worker, thread); connect the
    signals, then call thread.start(). The thread quits and both are scheduled
    for deletion when the queue drains."""
    thread = QThread()
    worker = QueueWorker(db, settings)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    lifetime.hold(worker, thread)
    return worker, thread


def recover(db, settings):
    """Put the app back in a consistent state at startup. Returns a sentence,
    or None if there was nothing to say.

    Two things can be left behind by a process that died: a job marked running
    that no thread is working on, and a staging directory full of a half-made
    film. The first is put back on the queue -- every stage of the work either
    finds its output already there and skips, or redoes it into a directory
    nothing else reads, so doing it twice is safe and stalling forever is not.
    The second is deleted, except for films still queued, which are exactly the
    ones a resume would want.
    """
    resumed = queue.reset_running(db)
    keep = {row["item_id"] for row in queue.pending(db)}
    cleared = nas_store.sweep_staging(settings.nas_root, keep)
    LOG.info("recovered: %d job(s) put back, %d working director%s cleared",
             resumed, cleared, "y" if cleared == 1 else "ies")
    if not resumed and not cleared:
        return None
    bits = []
    if resumed:
        bits.append("put %d interrupted film(s) back on the queue" % resumed)
    if cleared:
        bits.append("cleared %d abandoned working director%s"
                    % (cleared, "y" if cleared == 1 else "ies"))
    return "; ".join(bits) + "."
