"""Run a TMDb search on a worker thread so the UI never blocks on the network."""

from PySide6.QtCore import QObject, QThread, Signal

from ..core.tmdb_client import TmdbSearch, TmdbError


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
        try:
            client = TmdbSearch(self._key, self._language)
            rows = client.search(self._query, self._year)
        except TmdbError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:                    # pragma: no cover - defensive
            self.failed.emit(repr(exc))
        else:
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
    return job, thread
