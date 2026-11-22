"""WebSockets, server side, on the stdlib HTTP handler. RFC 6455, the parts a
phone gamepad needs and nothing else.

WHY A WEBSOCKET AND NOT MORE POLLING

The pointer posts its position at 25 Hz and that is fine for a dot. A game is
different in two ways. A button press must land within a frame or two, and a
request per press costs a TCP round trip plus HTTP framing on a phone radio
that sleeps between packets. And the game's sound has to come back the other
way, continuously, which polling cannot do at all. One socket that stays open
carries both: button state up, sound down.

WHAT IS HERE

The handshake (accept() on a GET that asks to upgrade), framing for text,
binary, ping, pong and close, the client-mask check the RFC requires, and
reassembly of fragmented messages up to MAX_MESSAGE. No extensions and no
subprotocols: the page asks for neither.

WRITES GO THROUGH ONE THREAD PER SOCKET

The game's sound is sent from the thread reading the frontend, roughly sixty
times a second, to every phone at once. If that thread wrote to the sockets
itself, one phone on bad Wi-Fi with a full send buffer would stall the sound
for every other phone, and then the game. So each Connection has its own writer
thread and a short queue. When the queue is full the oldest sound frame is
dropped, because a sound frame that is late is worth nothing, and a control
frame (pong, close, a role change) is never dropped.
"""

import base64
import collections
import hashlib
import json
import struct
import threading

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

TEXT, BINARY, CLOSE, PING, PONG, CONTINUATION = 0x1, 0x2, 0x8, 0x9, 0xA, 0x0

# Larger than anything the page sends by two orders of magnitude. A frame
# claiming more is refused before its payload is read.
MAX_MESSAGE = 64 * 1024

# Sound frames held for one phone before the oldest is dropped. Each is about
# 16 ms of sound, so this is a quarter of a second -- more than that and the
# phone would be playing the past.
AUDIO_QUEUE = 16


class Closed(Exception):
    """The other end closed the socket, cleanly or otherwise."""


def accept_key(key):
    return base64.b64encode(hashlib.sha1(key.encode("ascii") + GUID)
                            .digest()).decode("ascii")


def wants_upgrade(headers):
    return ("websocket" in (headers.get("Upgrade") or "").lower()
            and "upgrade" in (headers.get("Connection") or "").lower())


def accept(handler):
    """Answer an upgrade request on `handler` (a BaseHTTPRequestHandler).
    Returns a Connection, or None after sending a 400 for one that is not a
    valid WebSocket handshake."""
    key = handler.headers.get("Sec-WebSocket-Key") or ""
    version = handler.headers.get("Sec-WebSocket-Version") or ""
    try:
        valid = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid = False
    if not wants_upgrade(handler.headers) or version != "13" or not valid:
        handler.send_response(400)
        handler.send_header("Content-Length", "0")
        handler.send_header("Sec-WebSocket-Version", "13")
        handler.end_headers()
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    # The handler must not try to read another HTTP request off this socket
    # once the WebSocket is done with it.
    handler.close_connection = True
    return Connection(handler.rfile, handler.wfile, handler.connection)


def encode(opcode, payload=b""):
    """One unmasked, unfragmented frame, as a server sends it."""
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(n)
    elif n < 65536:
        head.append(126)
        head += struct.pack("!H", n)
    else:
        head.append(127)
        head += struct.pack("!Q", n)
    return bytes(head) + payload


def read_frame(rfile):
    """(fin, opcode, payload) from a client. Raises Closed on EOF or on a
    frame a client must never send."""
    head = _read_exact(rfile, 2)
    fin, opcode = head[0] & 0x80, head[0] & 0x0F
    if head[0] & 0x70:
        raise Closed("reserved bits set")
    if not head[1] & 0x80:
        # RFC 6455 5.1: a server MUST close on an unmasked client frame.
        raise Closed("unmasked client frame")
    n = head[1] & 0x7F
    if n == 126:
        n = struct.unpack("!H", _read_exact(rfile, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(rfile, 8))[0]
    if n > MAX_MESSAGE:
        raise Closed("frame too large")
    if opcode >= 0x8 and (n > 125 or not fin):
        raise Closed("bad control frame")
    mask = _read_exact(rfile, 4)
    data = bytearray(_read_exact(rfile, n))
    for i in range(n):
        data[i] ^= mask[i & 3]
    return bool(fin), opcode, bytes(data)


def _read_exact(rfile, n):
    if n == 0:
        return b""
    data = rfile.read(n)
    if not data or len(data) < n:
        raise Closed("end of stream")
    return data


class Connection:
    """One open WebSocket. recv() on the handler's thread, send_*() from
    anywhere."""

    def __init__(self, rfile, wfile, sock=None):
        self._rfile = rfile
        self._wfile = wfile
        self._sock = sock
        self._cond = threading.Condition()
        self._control = collections.deque()
        self._audio = collections.deque(maxlen=AUDIO_QUEUE)
        self._closed = False
        self.dropped = 0
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    # -- sending, from any thread ------------------------------------------

    def send_text(self, obj):
        """A JSON message. Never dropped."""
        self._queue(self._control, encode(TEXT, json.dumps(obj).encode()))

    def send_binary(self, data):
        """A sound frame. Dropped, oldest first, if the phone falls behind."""
        with self._cond:
            if self._closed:
                return
            if len(self._audio) == self._audio.maxlen:
                self.dropped += 1
            self._audio.append(encode(BINARY, data))
            self._cond.notify()

    def _queue(self, queue, frame):
        with self._cond:
            if self._closed:
                return
            queue.append(frame)
            self._cond.notify()

    def _write_loop(self):
        while True:
            with self._cond:
                while not self._control and not self._audio and not self._closed:
                    self._cond.wait()
                if self._control:
                    frame = self._control.popleft()
                elif self._audio and not self._closed:
                    frame = self._audio.popleft()
                else:
                    # Closed, and the close frame is already on the wire.
                    return
            try:
                self._wfile.write(frame)
                self._wfile.flush()
            except (OSError, ValueError):
                self.close()
                return

    # -- receiving, on the handler's thread --------------------------------

    def recv(self):
        """The next message as (opcode, payload): TEXT or BINARY. Answers
        pings itself. Raises Closed when the socket ends, and TimeoutError
        (socket.timeout) when nothing arrived within the socket's timeout."""
        parts, kind = [], None
        while True:
            fin, opcode, payload = read_frame(self._rfile)
            if opcode == PING:
                self._queue(self._control, encode(PONG, payload))
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self._queue(self._control, encode(CLOSE, payload[:2]))
                raise Closed("closed by the phone")
            if opcode == CONTINUATION:
                if kind is None:
                    raise Closed("continuation with nothing to continue")
            elif opcode in (TEXT, BINARY):
                if kind is not None:
                    raise Closed("new message inside a fragmented one")
                kind = opcode
            else:
                raise Closed("unknown opcode %d" % opcode)
            parts.append(payload)
            if sum(len(p) for p in parts) > MAX_MESSAGE:
                raise Closed("message too large")
            if fin:
                return kind, b"".join(parts)

    def settimeout(self, seconds):
        if self._sock is not None:
            self._sock.settimeout(seconds)

    def close(self, code=1000):
        with self._cond:
            if self._closed:
                return
            self._control.append(encode(CLOSE, struct.pack("!H", code)))
            self._closed = True
            self._cond.notify()

    @property
    def closed(self):
        return self._closed

    def drain(self, timeout=1.0):
        """Wait for the writer to send what is queued, for a clean close."""
        self._writer.join(timeout)


