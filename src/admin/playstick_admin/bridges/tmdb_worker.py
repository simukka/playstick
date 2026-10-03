"""Run a TMDb search on a worker thread so the UI never blocks on the network."""

import logging

from PySide6.QtCore import QObject, QThread, Signal

from . import lifetime
from ..core.tmdb_client import TmdbSearch, TmdbError

LOG = logging.getLogger(__name__)


class _SearchJob(QObject):
    results = Signal(list)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, key, language, query, year):
        super().__init__()
        self._key = key
        self._language = language
        self._query = query
        self._year = year

    def run(self):
        lifetime.name_thread("tmdb")
        LOG.info("searching TMDb for %r%s", self._query,
                 " (%s)" % self._year if self._year else "")
        try:
            client = TmdbSearch(self._key, self._language)
            rows = client.search(self._query, self._year)
        except TmdbError as exc:
            LOG.warning("the TMDb search failed: %s", exc)
            self.failed.emit(str(exc))
        except Exception as exc:                    # pragma: no cover - defensive
            LOG.exception("the TMDb search stopped on an unexpected error")
            self.failed.emit(repr(exc))
        else:
            LOG.info("TMDb offered %d candidate(s)", len(rows))
            self.results.emit(rows)
        finally:
            self.finished.emit()


def start_search(key, language, query, year):
    """Returns (job, thread); connect job.results/job.failed then thread.start()."""
    thread = QThread()
    job = _SearchJob(key, language, query, year)
    job.moveToThread(thread)
    thread.started.connect(job.run)
    job.finished.connect(thread.quit)
    job.finished.connect(job.deleteLater)
    thread.finished.connect(thread.deleteLater)
    lifetime.hold(job, thread)
    return job, thread
