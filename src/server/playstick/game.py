"""A game on the projector: the frontend process, the players, and the sound.

playstick-game (src/server/playstick-game.py) does the emulation and the
drawing. This class starts it on tty1 the way Player starts mpv, through
stage.py, and sits between it and the phones:

  phones --WebSocket--> GameRunner --unix socket--> playstick-game
  phones <--sound------ GameRunner <--sound-------- playstick-game

THE CONTROL SOCKET

The daemon listens on GAME_SOCKET before it starts the frontend, and the
frontend connects to it. Every message either way is one type byte, a
big-endian u16 length, and that many bytes of payload:

  to the frontend    b"I"  port u8, mask u16    the buttons held on one pad
                     b"Q"  (empty)              quit
  from the frontend  b"R"  JSON                 loaded: {"fps", "rate"}
                     b"A"  int16 LE mono PCM    about one frame of sound
                     b"E"  UTF-8 text           why it is giving up

THE PLAYERS

Two pads, port 0 and port 1. The first phone to open the gamepad gets port 0
if nobody has it; a later phone listens, and can ask to join, which gives it
the lowest free port. A phone that goes quiet for STALE_SECONDS has its pad
released -- every button let go -- so a phone that locked its screen with a
thumb on "right" does not leave the character walking into a wall. Its seat
is kept until the socket actually closes.

ONLY THE NEWEST STATE IS SENT, AND NOTHING IS LOST BY THAT

Following pointer.py, the frontend is only ever told the newest state of each
pad. That loses no presses, because the message is the whole mask of buttons
held, not a press or a release. A press and its release inside one frame are
the one case that can vanish, and a child's thumb is never that quick.
"""

import errno
import json
import os
import socket
import struct
import subprocess
import threading
import time

from . import config
from .config import log
from .airplay import airplay_confirmed
from .library import resolve_inside
from .player import Busy
from . import stage
from . import ws

# The bits of a pad's mask, in the order the page and the frontend both use.
UP, DOWN, LEFT, RIGHT, A, B, START, SELECT = (1 << i for i in range(8))
MASK_BITS = 0xFF

PORTS = (0, 1)

# A pad whose phone has said nothing for this long is let go. The page sends
# a heartbeat every 100 ms while it is open, so this is ten missed beats.
STALE_SECONDS = 1.0

# How long the frontend has to load its core and say it is ready. A core that
# takes longer than this to load a cartridge is not going to play it.
READY_SECONDS = 15.0

# How long a phone may go without sending anything before its socket is
# closed. The heartbeat makes this generous.
SOCKET_TIMEOUT = 10.0


def pack(kind, payload=b""):
    return struct.pack("!cH", kind, len(payload)) + payload


class Seat:
    """One phone on the game socket."""

    def __init__(self, conn):
        self.conn = conn
        self.port = None            # 0 or 1 while playing, None while listening
        self.mask = 0
        self.heard = time.monotonic()


class GameRunner:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._proc = None
        self._item = None
        self._ctl = None            # the frontend's end of the control socket
        self._send_lock = threading.Lock()
        self._restore_airplay = False
        self._seats = []
        self._sent = {}             # port -> last mask the frontend was told
        self._info = {}

    # -- what the projectionist and the HTTP layer ask --------------------

    def running(self):
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def current_item(self):
        with self._lock:
            return self._item if self.running() else None

    def status(self):
        """The part of /api/status that is about the game, or None."""
        with self._lock:
            if not self.running():
                return None
            taken = {s.port for s in self._seats if s.port is not None}
            return {"system": self._item.get("system"),
                    "players": self._item.get("players") or 1,
                    "seats": [p in taken for p in PORTS],
                    "listeners": len(self._seats),
                    "rate": self._info.get("rate", config.GAME_AUDIO_RATE)}

    # -- lifecycle ----------------------------------------------------------

    def start(self, item, progress=None):
        # Resolved before the lock, as Player.start does a film: these are
        # stats on the share, and status() takes the same lock.
        real = resolve_inside(item["path"])
        with self._lock:
            if not config.GAMES_ENABLED:
                raise Busy("Games are switched off on this stick.")
            if self.running():
                raise Busy("A game is already playing.")
            if airplay_confirmed():
                raise Busy("The projector is being used for AirPlay.")
            core = config.GAME_CORES.get(item.get("system") or "")
            if not core or not os.path.isfile(core):
                raise Busy("This stick cannot play %s games yet."
                           % (item.get("system") or "these"))
            if real is None:
                raise Busy("That file is not in the library.")

            self._restore_airplay = stage.take_display(progress)
            listener = self._listen()
            argv = [config.GAME_PYTHON, config.GAME_FRONTEND,
                    "--core", core, "--rom", real,
                    "--control", config.GAME_SOCKET,
                    "--rate", str(config.GAME_AUDIO_RATE),
                    "--saves", config.GAME_SAVES, *config.GAME_EXTRA_ARGS]
            if progress:
                progress("starting")
            log("playing %s (%s)", item["title"], item.get("system"))
            tty = stage.open_tty()
            try:
                self._proc = subprocess.Popen(
                    argv,
                    stdin=tty if tty is not None else subprocess.DEVNULL,
                    stdout=tty if tty is not None else subprocess.DEVNULL,
                    stderr=None)
            except OSError as exc:
                listener.close()
                self._teardown()
                raise Busy("The game would not start: %s" % exc) from exc
            finally:
                if tty is not None:
                    os.close(tty)
            self._item = item
            self._sent = {}
            try:
                self._ctl, self._info = self._handshake(listener)
            except Busy:
                self._kill()
                self._teardown()
                raise
            finally:
                listener.close()
            threading.Thread(target=self._read_loop, args=(self._proc,),
                             daemon=True).start()
            threading.Thread(target=self._reap, args=(self._proc,),
                             daemon=True).start()
            threading.Thread(target=self._watch, args=(self._proc,),
                             daemon=True).start()
            for seat in self._seats:
                self._tell(seat)

    def _listen(self):
        try:
            os.unlink(config.GAME_SOCKET)
        except OSError:
            pass
        os.makedirs(os.path.dirname(config.GAME_SOCKET), exist_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(config.GAME_SOCKET)
        os.chmod(config.GAME_SOCKET, 0o600)
        sock.listen(1)
        sock.settimeout(0.25)
        return sock

    def _handshake(self, listener):
        """Accept the frontend's connection and wait for it to say ready."""
        deadline = self._clock() + READY_SECONDS
        conn = None
        while conn is None:
            if self._proc.poll() is not None:
                raise Busy("The game would not start.")
            if self._clock() > deadline:
                raise Busy("The game took too long to start.")
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
        conn.settimeout(READY_SECONDS)
        try:
            kind, payload = read_message(conn)
        except (OSError, EOFError) as exc:
            conn.close()
            raise Busy("The game would not start.") from exc
        if kind == b"E":
            conn.close()
            raise Busy("The game would not start: %s"
                       % payload.decode("utf-8", "replace")[:200])
        if kind != b"R":
            conn.close()
            raise Busy("The game would not start.")
        conn.settimeout(None)
        try:
            info = json.loads(payload.decode("utf-8") or "{}")
        except ValueError:
            info = {}
        log("game ready: %s", info)
        return conn, info

    def _read_loop(self, proc):
        """Sound from the frontend, out to every phone."""
        conn = self._ctl
        while True:
            try:
                kind, payload = read_message(conn)
            except (OSError, EOFError):
                return
            if kind == b"A":
                with self._lock:
                    seats = list(self._seats) if self._proc is proc else []
                for seat in seats:
                    seat.conn.send_binary(payload)
            elif kind == b"E":
                log("game: %s", payload.decode("utf-8", "replace")[:400])

    def _reap(self, proc):
        proc.wait()
        with self._lock:
            if self._proc is proc:
                log("game finished (rc=%s)", proc.returncode)
                self._teardown()

    def _watch(self, proc):
        """Let go of a pad whose phone has gone quiet."""
        while True:
            time.sleep(STALE_SECONDS / 4)
            with self._lock:
                if self._proc is not proc:
                    return
                now = self._clock()
                for seat in self._seats:
                    if seat.port is not None and seat.mask and \
                            now - seat.heard > STALE_SECONDS:
                        seat.mask = 0
                        self._send_pad(seat.port, 0)

    def stop(self):
        with self._lock:
            proc = self._proc
        if proc is None:
            return
        self._send(pack(b"Q"))
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log("the game ignored quit; killing")
            self._kill()
        with self._lock:
            if self._proc is proc:
                self._teardown()

    def _kill(self):
        proc = self._proc
        if proc is None:
            return
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def _teardown(self):
        with self._lock:
            if self._ctl is not None:
                try:
                    self._ctl.close()
                except OSError:
                    pass
                self._ctl = None
            self._proc = None
            self._item = None
            self._info = {}
            self._sent = {}
            for seat in self._seats:
                seat.port = None
                seat.mask = 0
            try:
                os.unlink(config.GAME_SOCKET)
            except OSError:
                pass
            restore, self._restore_airplay = self._restore_airplay, False
            stage.give_back_display(restore)
            for seat in self._seats:
                self._tell(seat)

    # -- the frontend -------------------------------------------------------

    def _send(self, message):
        with self._send_lock:
            conn = self._ctl
            if conn is None:
                return
            try:
                conn.sendall(message)
            except OSError as exc:
                if exc.errno != errno.EPIPE:
                    log("game control: %s", exc)

    def _send_pad(self, port, mask):
        if self._sent.get(port) == mask:
            return
        self._sent[port] = mask
        self._send(pack(b"I", struct.pack("!BH", port, mask)))

    # -- the phones ---------------------------------------------------------

    def serve(self, conn):
        """Run one phone's socket until it closes. Called on the HTTP
        handler's own thread, which is what that thread is for from here on."""
        seat = Seat(conn)
        with self._lock:
            if len(self._seats) >= config.GAME_MAX_SOCKETS:
                conn.send_text({"error": "Too many phones are connected."})
                conn.close(1013)
                conn.drain()
                return
            self._seats.append(seat)
            self._tell(seat)
        conn.settimeout(SOCKET_TIMEOUT)
        try:
            while not conn.closed:
                try:
                    kind, payload = conn.recv()
                except (ws.Closed, OSError, ValueError):
                    break
                seat.heard = self._clock()
                if kind == ws.BINARY:
                    self._on_pad(seat, payload)
                else:
                    self._on_text(seat, payload)
        finally:
            with self._lock:
                if seat in self._seats:
                    self._seats.remove(seat)
                if seat.port is not None and self.running():
                    self._send_pad(seat.port, 0)
                seat.port = None
                for other in self._seats:
                    self._tell(other)
            conn.close()
            conn.drain()

    def _on_pad(self, seat, payload):
        """[seq u8, mask u16]. The sequence number is for the page's own
        debugging; the mask is the whole state, so order does not matter."""
        if len(payload) != 3:
            return
        mask = struct.unpack("!H", payload[1:])[0] & MASK_BITS
        with self._lock:
            seat.mask = mask
            if seat.port is not None and self.running():
                self._send_pad(seat.port, mask)

    def _on_text(self, seat, payload):
        try:
            message = json.loads(payload.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        with self._lock:
            if message.get("join") in ("auto", "any"):
                self._seat(seat, first_only=message["join"] == "auto")
            elif message.get("leave"):
                if seat.port is not None and self.running():
                    self._send_pad(seat.port, 0)
                seat.port = None
                seat.mask = 0
            for other in self._seats:
                self._tell(other)

    def _seat(self, seat, first_only):
        """Give `seat` a pad. "auto" is what the gamepad asks for when it
        opens, and only takes port 0: the phone that started the game is
        player one, and every other phone that opens the page listens until
        somebody chooses to join."""
        if seat.port is not None or not self.running():
            return
        players = (self._item or {}).get("players") or 1
        taken = {s.port for s in self._seats if s.port is not None}
        ports = (0,) if first_only else PORTS[:max(1, min(players, 2))]
        for port in ports:
            if port not in taken:
                seat.port = port
                seat.mask = 0
                return

    def _tell(self, seat):
        """Send one phone its role and the seats, after anything changes."""
        status = self.status()
        seat.conn.send_text({
            "role": "p%d" % (seat.port + 1) if seat.port is not None
            else "listener",
            "game": status,
        })


def read_message(conn):
    """One control message off the frontend's socket: (type, payload)."""
    head = _recv_exact(conn, 3)
    kind, n = struct.unpack("!cH", head)
    return kind, _recv_exact(conn, n)


def _recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise EOFError("the game closed its socket")
        data += chunk
    return data
