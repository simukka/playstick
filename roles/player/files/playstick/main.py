"""Wiring: build the workers, hand them to the handler, serve."""

import os
import signal
import threading
import time
from http.server import ThreadingHTTPServer

from .config import (
    BIND, LIBRARY, PORT, PROJECTOR_DEVICE, PROJECTOR_MODEL, PROJECTOR_TIMEOUT,
    THUMB_DIR, log
)
from .library import Library
from .thumbs import Thumbs
from .player import Player
from .projectionist import Projectionist
from .projector import open_projector
from .http import Handler


def main():
    if not os.path.isdir(THUMB_DIR):
        os.makedirs(THUMB_DIR, exist_ok=True)

    library = Library()
    player = Player()
    # Nothing is opened here: the serial port is opened on the first command,
    # so a projector that is unplugged at boot cannot delay the web UI coming
    # up -- or, with a cable that hangs on open(), prevent it.
    projector = open_projector(PROJECTOR_MODEL, PROJECTOR_DEVICE,
                               PROJECTOR_TIMEOUT)
    projectionist = Projectionist(projector, player)
    # The projectionist rather than the player, so that posters stop being
    # extracted over CIFS while a lamp is warming up as well as while a film
    # is running. Thumbs only ever asks for a state, and this answers with the
    # composite one.
    thumbs = Thumbs(library, projectionist)

    # Clean up after whatever killed the last run before serving anything.
    player.reconcile()

    stopping = threading.Event()

    def shutdown(_sig, _frm):
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=library.run, args=(stopping,), daemon=True).start()
    threading.Thread(target=thumbs.run, args=(stopping,), daemon=True).start()
    threading.Thread(target=projectionist.run, args=(stopping,),
                     daemon=True).start()

    Handler.library = library
    Handler.thumbs = thumbs
    Handler.player = player
    Handler.projectionist = projectionist

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
    # Through the projectionist so that a preparation caught mid-warm-up is
    # abandoned too, rather than a detached thread carrying on to start a film
    # nothing is left to stop.
    projectionist.stop()
    # The lamp is deliberately left alone. A re-provision restarts this unit,
    # and switching the projector off because its daemon was upgraded would be
    # this process inventing a decision nobody asked for -- the keeper will do
    # it in half an hour if the room really is empty.
    projectionist.close()
