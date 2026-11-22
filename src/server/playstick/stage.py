"""The display, and who has it: a film, a game, or the AirPlay receiver.

Exactly one process may hold DRM master. mpv takes it for a film and
playstick-game takes it for a game, and before either one does, the AirPlay
receiver has to be stopped and has to be restarted afterwards if it was
running. That arbitration used to live inside Player. It lives here now so the
two can share it, because two copies of "stop uxplay, wait, settle, remember to
restore it" would drift apart.

The helpers are module functions rather than a class. Each one is a step the
caller takes in its own lock and its own order, and the order is load-bearing:
the busy flag after the display is free, the restore after the process has
gone.

STAGE

`Stage` puts the two players behind the one interface the projectionist was
written against: state(), current_item(), start(item, progress) and stop(). It
picks between them on the item's kind. A third kind, "calibrate", is the
player again with no film: mpv on a black screen for the calibration wizard
(see calibrate.py). The projectionist needs no other change, and its
existing refusals ("a film is already playing") cover a game and a
calibration too, because either makes state() something other than "idle".
"""

import os
import threading
import time

from .config import (AIRPLAY_UNIT, BUSY_FILE, MPV_TTY, RESTORE_FILE,
                     SETTLE_SECONDS, log)
from .airplay import systemctl, unit_active


def take_display(progress=None):
    """Free the display for a player about to start. Returns whether the
    AirPlay unit was stopped and so is owed a restart.

    Only ever restore what we took away. The repo installs uxplay-kms.service
    without enabling it -- which output path wins is a probe result -- so an
    operator may deliberately be running neither, and starting one afterwards
    would be this daemon inventing a configuration nobody chose.
    """
    if progress:
        progress("display")
    restore = unit_active(AIRPLAY_UNIT)
    if restore:
        log("stopping %s to take DRM master", AIRPLAY_UNIT)
        write_flag(RESTORE_FILE, AIRPLAY_UNIT)
        systemctl("stop", AIRPLAY_UNIT)
        deadline = time.time() + 20
        while unit_active(AIRPLAY_UNIT) and time.time() < deadline:
            time.sleep(0.25)
        # The card is released when the process exits and the kernel closes
        # its fd; give that a moment before the next player reaches for it.
        time.sleep(SETTLE_SECONDS)
    write_flag(BUSY_FILE, str(os.getpid()))
    return restore


def give_back_display(restore):
    """The player has gone. Clear the busy flag, and restart AirPlay if
    take_display stopped it."""
    clear_flag(BUSY_FILE)
    if restore:
        log("restoring %s", AIRPLAY_UNIT)
        systemctl("start", AIRPLAY_UNIT)
        clear_flag(RESTORE_FILE)


def open_tty():
    """An fd on tty1 for a player's stdin/stdout, or None.

    mpv's DRM backend and SDL's KMSDRM driver both set up a VT switcher so the
    console can be taken back with chvt, and they need a terminal to do it on.
    getty@tty1 is masked by claim_tty1, and the idle clock only writes pixels,
    so nothing else wants this device.
    """
    if not MPV_TTY:
        return None
    try:
        return os.open(MPV_TTY, os.O_RDWR | os.O_NOCTTY)
    except OSError as exc:
        log("no tty (%s): %s -- carrying on without one", MPV_TTY, exc)
        return None


def write_flag(path, contents):
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(contents + "\n")
    except OSError as exc:
        log("could not write %s: %s", path, exc)


def clear_flag(path):
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def reconcile():
    """Put the display back where it belongs after an unclean exit.

    /run is tmpfs, so a reboot clears these files and this does nothing --
    which is right, because a boot starts the AirPlay unit itself. What this
    catches is the daemon being SIGKILLed mid-film or mid-game: systemd
    restarts it, the player is already gone with it (KillMode=control-group),
    and the only thing left is an AirPlay receiver somebody stopped and never
    restarted.
    """
    clear_flag(BUSY_FILE)
    if not RESTORE_FILE or not os.path.exists(RESTORE_FILE):
        return
    try:
        with open(RESTORE_FILE) as fh:
            unit = fh.read().strip() or AIRPLAY_UNIT
    except OSError:
        unit = AIRPLAY_UNIT
    if not unit_active(unit):
        log("recovering from an unclean stop: restoring %s", unit)
        systemctl("start", unit)
    clear_flag(RESTORE_FILE)


class Stage:
    """A film player and a game runner, behind one interface."""

    def __init__(self, player, game):
        self.player = player
        self.game = game
        # Held across a start, so a film and a game asked for at the same
        # moment cannot both see an idle stage and both take the display.
        self._lock = threading.Lock()

    def state(self):
        if self.game.running():
            return "gaming"
        return self.player.state()

    def current_item(self):
        return self.game.current_item() or self.player.current_item()

    def current_title(self):
        item = self.current_item()
        return (item.get("full_title") or item["title"]) if item else ""

    def start(self, item, progress=None):
        from .player import Busy
        with self._lock:
            if self.state() != "idle":
                raise Busy("Something is already on the projector.")
            if item.get("kind") == "game":
                return self.game.start(item, progress=progress)
            if item.get("kind") == "calibrate":
                return self.player.start_blank(item, progress=progress)
            return self.player.start(item, progress=progress)

    def stop(self):
        self.game.stop()
        self.player.stop()

    def reconcile(self):
        reconcile()
