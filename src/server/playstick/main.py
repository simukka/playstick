"""Wiring: build the workers, hand them to the handler, serve."""

import signal
import threading
import time
from http.server import ThreadingHTTPServer

from .config import (
    BIND, LIBRARY, PORT, PROJECTOR_DEVICE, PROJECTOR_MODEL, PROJECTOR_TIMEOUT,
    TLS_CERT, TLS_KEY, TLS_PORT, log
)
from .db import Database
from .library import Library, share_present
from .nas import NasWatcher
from .thumbs import Thumbs
from .player import Player
from .game import GameRunner
from .stage import Stage
from .projectionist import Projectionist
from .projector import open_projector
from .http import Handler
from .draw import Drawing
from .pointer import PointerOverlay
from .calibrate import Calibration
from .profiles import Profiles
from . import tls


def main():
    # Before anything else and before any thread: every worker below takes a
    # connection from this, and a worker that opened one against a file with no
    # tables in it would fail on its first query rather than at start-up where
    # somebody is watching.
    db = Database()
    db.migrate()

    library = Library(db)
    # The library so the watcher can ask for a rescan the moment the NAS
    # answers again: without it the grid stays empty until the next
    # scheduled scan, which to whoever pressed the button looks exactly
    # like the button not having worked.
    nas = NasWatcher(library=library)
    player = Player()
    # Nothing is opened here: the serial port is opened on the first command,
    # so a projector that is unplugged at boot cannot delay the web UI coming
    # up -- or, with a cable that hangs on open(), prevent it.
    projector = open_projector(PROJECTOR_MODEL, PROJECTOR_DEVICE,
                               PROJECTOR_TIMEOUT)
    game = GameRunner()
    # The projectionist is handed both players behind one interface, and the
    # item's kind decides which of them starts. See stage.py.
    #
    # The NAS watcher and the share probe so that a film picked while the NAS
    # sleeps wakes it and waits for the films, rather than failing on them.
    projectionist = Projectionist(projector, Stage(player, game), nas=nas,
                                  share=share_present)
    # The projectionist rather than the player, so that posters stop being
    # extracted over CIFS while a lamp is warming up as well as while a film
    # is running. Thumbs only ever asks for a state, and this answers with the
    # composite one.
    thumbs = Thumbs(db, library, projectionist)

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
    # Returns immediately when no NAS is configured, so this costs an
    # unconfigured install one thread that exits.
    threading.Thread(target=nas.run, args=(stopping,), daemon=True).start()

    Handler.db = db
    Handler.library = library
    Handler.nas = nas
    Handler.thumbs = thumbs
    Handler.player = player
    Handler.game = game
    Handler.projectionist = projectionist
    pointer = PointerOverlay(player)
    threading.Thread(target=pointer.run, args=(stopping,), daemon=True).start()
    Handler.pointer = pointer
    drawing = Drawing(player)
    threading.Thread(target=drawing.run, args=(stopping,), daemon=True).start()
    Handler.drawing = drawing
    # The calibration wizard's black screen, drawn on the same mpv. A session
    # nobody is holding any more is ended through the projectionist, the same
    # way the page's STOP ends a film.
    calibration = Calibration(player, stop=projectionist.stop)
    threading.Thread(target=calibration.run, args=(stopping,),
                     daemon=True).start()
    Handler.calibration = calibration
    Handler.profiles = Profiles(db)

    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    httpd.daemon_threads = True
    log("listening on %s:%d, library %s", BIND, PORT, LIBRARY)

    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5},
                     daemon=True).start()

    # After the plain listener, so that nothing about a certificate -- missing,
    # mismatched, mid-renewal -- can stand between a phone and the page. A
    # thread of its own because the certificate may not exist yet: on a first
    # deploy it is issued after a person adds a DNS record, long after this
    # process started, and HTTPS should come up then without a restart.
    https = tls.Listener(BIND, TLS_PORT, TLS_CERT, TLS_KEY, Handler)
    threading.Thread(target=https.run, args=(stopping,), daemon=True).start()

    while not stopping.is_set():
        time.sleep(0.25)

    log("shutting down")
    httpd.shutdown()
    https.shutdown()
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
