"""Wiring: build the three workers, hand them to the handler, serve."""

import os
import signal
import threading
import time
from http.server import ThreadingHTTPServer

from .config import BIND, LIBRARY, PORT, THUMB_DIR, log
from .library import Library
from .thumbs import Thumbs
from .player import Player
from .http import Handler


def main():
    if not os.path.isdir(THUMB_DIR):
        os.makedirs(THUMB_DIR, exist_ok=True)

    library = Library()
    player = Player()
    thumbs = Thumbs(library, player)

    # Clean up after whatever killed the last run before serving anything.
    player.reconcile()

    stopping = threading.Event()

    def shutdown(_sig, _frm):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=library.run, args=(stopping,), daemon=True).start()
    threading.Thread(target=thumbs.run, args=(stopping,), daemon=True).start()

    Handler.library = library
    Handler.thumbs = thumbs
    Handler.player = player

    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    log("listening on %s:%d, library %s", BIND, PORT, LIBRARY)

    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                     daemon=True).start()

    while not stopping.is_set():
        time.sleep(0.25)

    log("shutting down")
    httpd.shutdown()
    # Stopping the daemon must not leave the projector showing a film with
    # nothing able to stop it, and must not leave AirPlay switched off.
    player.stop()
