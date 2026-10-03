"""Fetch the device's posters in the background, one film at a time.

The Library tab draws a poster per row, and on the device those are rows in a
database rather than files on this machine -- so drawing them means a request
each. A few hundred of those on the UI thread is a window that does not paint
until the last one lands.

So they come down on one worker thread, oldest request first, and each arrival
is a signal the model turns into one repainted row. The list is usable
immediately and fills in; nothing waits for anything.

ONE THREAD, NOT A POOL. The bottleneck is a stick with four 1.44 GHz cores
serving a page to six phones, and the reason the daemon caps concurrent audio
streams at all. Four parallel poster fetches would be this app deciding it
matters more than whatever is on the projector.
"""

import logging

from PySide6.QtCore import QObject, QThread, Signal

from . import lifetime
from ..core import posters
from ..core.device_client import DeviceError

LOG = logging.getLogger(__name__)


class PosterWorker(QObject):
    #: film id, path to the file on this machine
    ready = Signal(str, str)
    finished = Signal()

    def __init__(self, client, wanted):
        """`wanted` is [(film_id, etag)] -- everything the device says it has a
        poster for. Ones already cached are answered without a request."""
        super().__init__()
        self._client = client
        self._wanted = list(wanted)
        self._stop = False

    def run(self):
        lifetime.name_thread("posters")
        LOG.debug("fetching %d poster(s) from the stick", len(self._wanted))
        got = 0
        try:
            for film_id, etag in self._wanted:
                if self._stop:
                    break
                try:
                    path = posters.fetch(self._client, film_id, etag)
                except DeviceError as exc:
                    # A stick that went away mid-fetch stops the run rather
                    # than producing one failure per remaining film. The rows
                    # keep whatever poster they already had.
                    LOG.warning("stopped fetching posters after %d: %s",
                                got, exc)
                    break
                if path:
                    got += 1
                    self.ready.emit(film_id, path)
        finally:
            LOG.debug("%d poster(s) drawn", got)
            self.finished.emit()

    def stop(self):
        """Abandon the rest. Safe from any thread -- it sets a flag the loop
        reads between films, so a fetch already in flight still completes and
        is still cached, which is why nothing is wasted by reloading twice."""
        self._stop = True


def start(client, wanted):
    """Returns (worker, thread); connect worker.ready, then thread.start()."""
    thread = QThread()
    worker = PosterWorker(client, wanted)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    lifetime.hold(worker, thread)
    return worker, thread
