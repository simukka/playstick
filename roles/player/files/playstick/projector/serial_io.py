"""Framing and the serial port, kept apart so one of them can be tested.

The Panasonic wire format is three pieces: a frame goes out, an ETX-terminated
frame comes back, and nothing is ever pipelined. `encode` and `ResponseParser`
are pure functions of bytes with no port behind them; `SerialTransport` is the
port with no protocol in it; `Link` is the pair, and is the seam the tests
drive -- a Link over a list-of-bytes transport exercises every line of the
framing on a machine with no projector and no serial hardware at all.

WHY NOT pyserial

The daemon has no third-party dependencies and there is no room on the eMMC to
start acquiring them for a device this simple. Nine thousand lines of pyserial
would be carried to configure a port that needs exactly one termios call, and
the same argument is already made in thumbs.py about ffmpeg.

WHY THE DEADLINE COVERS THE WHOLE REPLY

The Rust implementation this was ported from reads one byte at a time with a
one-second timeout on each, so a projector that answers slowly can take
`timeout x len(payload)` to fail rather than `timeout`. Here `read_frame` takes
a single deadline for the entire exchange, which is both what the caller means
and what makes the timeout a number anybody can reason about.
"""

import os
import select
import termios

from .base import NoReply, Unreachable


STX = 0x02
ETX = 0x03

# Replies are short: "000", "HD3", "ER401", a command echo. The Rust crate
# fixes its buffer at 32 for the same reason, and the cap matters here for the
# same one -- a line that has come loose can deliver noise indefinitely, and
# something has to decline to accumulate it.
MAX_PAYLOAD = 32

# The projector's ER401: "I do not accept that command."
ERROR_PAYLOAD = b"ER401"


class ResponseTooLong(Exception):
    """More than MAX_PAYLOAD bytes arrived with no ETX to end them.

    Not a ProjectorError: the parser recovers by itself and the exchange that
    provoked it will fail on its own deadline. It is raised rather than
    swallowed so that a test can prove the recovery happened.
    """


def encode(code, param=None):
    """STX, a three-byte command, an optional ":parameter", ETX.

    >>> encode("PON")
    b'\\x02PON\\x03'
    >>> encode("IIS", "HD3")
    b'\\x02IIS:HD3\\x03'
    """
    if len(code) != 3:
        raise ValueError("command codes are exactly three bytes: %r" % (code,))
    frame = bytearray([STX])
    frame += code.encode("ascii")
    if param:
        frame += b":" + param.encode("ascii")
    frame.append(ETX)
    return bytes(frame)


class ResponseParser:
    """Bytes in, one payload out per ETX.

    A leading STX -- or one arriving mid-stream after line noise -- discards
    whatever had accumulated, so a reply is recognised whether or not the
    projector prefixed it and whether or not there was garbage in front of it.
    That behaviour is carried over deliberately from the Rust implementation,
    where it is what makes a glitchy line self-correcting rather than
    permanently one frame behind.
    """

    def __init__(self):
        self._buf = bytearray()

    def reset(self):
        self._buf.clear()

    def push(self, byte):
        """Feed one byte. Returns the payload when a frame completes."""
        if byte == STX:
            self._buf.clear()
            return None
        if byte == ETX:
            payload = bytes(self._buf)
            self._buf.clear()
            return payload
        if len(self._buf) >= MAX_PAYLOAD:
            self._buf.clear()
            raise ResponseTooLong()
        self._buf.append(byte)
        return None


class SerialTransport:
    """A raw 9600 8N1 port, which is all the manual asks for."""

    def __init__(self, path):
        try:
            # O_NONBLOCK on the open itself, not just afterwards: opening a
            # tty otherwise blocks waiting for carrier, and a cable with
            # nothing on the far end hangs here rather than failing.
            self._fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise Unreachable("could not open %s: %s" % (path, exc)) from exc
        try:
            self._configure()
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise Unreachable("could not configure %s: %s" % (path, exc)) from exc

    def _configure(self):
        attrs = termios.tcgetattr(self._fd)
        cc = list(attrs[6])
        # Built from zero rather than masked out of the port's previous state.
        # Every setting is fixed by the manual, so there is nothing worth
        # preserving, and a flag left set by whatever touched the port last is
        # a fault that reproduces on one machine and nowhere else.
        iflag = 0    # no IXON/IXOFF -- the projector uses no flow control --
                     # and no ICRNL, which would rewrite a 0x0d in a payload
        oflag = 0    # no ONLCR: this is binary framing, not text
        lflag = 0    # raw: no ICANON, no ECHO, no ISIG
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        # PARENB clear  -> no parity        CSTOPB clear -> one stop bit
        # CRTSCTS clear -> no flow control
        # CLOCAL is the load-bearing one: it tells the kernel not to wait on
        # modem control lines, which a three-wire TXD/RXD/GND cable never
        # asserts. Without it reads block forever on a perfectly good link.
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag,
                           termios.B9600, termios.B9600, cc])

    def discard_input(self):
        """Drop anything buffered from an exchange that timed out.

        Without this, a command that gave up waiting leaves its late reply in
        the kernel buffer, and the NEXT command reads it and believes it -- so
        one timeout makes every subsequent answer off by one. That is a far
        worse failure than the timeout itself, because nothing about it looks
        wrong.
        """
        try:
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except OSError as exc:
            raise Unreachable("flush failed: %s" % exc) from exc

    def write(self, data):
        try:
            os.write(self._fd, data)
        except OSError as exc:
            raise Unreachable("write failed: %s" % exc) from exc

    def wait(self, seconds):
        """True if there is something to read within `seconds`."""
        try:
            return bool(select.select([self._fd], [], [], max(0.0, seconds))[0])
        except OSError as exc:
            raise Unreachable("select failed: %s" % exc) from exc

    def read(self, limit=64):
        try:
            return os.read(self._fd, limit)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise Unreachable("read failed: %s" % exc) from exc

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None


class Link:
    """One transport, one parser, and the request/response discipline.

    Commands are never pipelined: the manual requires waiting for a reply
    before sending the next frame, and both the caller's lock and this class's
    single-threaded exchange are what keep that true.
    """

    def __init__(self, transport, timeout, clock):
        self._transport = transport
        self._timeout = timeout
        self._clock = clock
        self._parser = ResponseParser()

    def exchange(self, code, param=None, timeout=None):
        """Send a frame, return the payload of the reply.

        Raises NoReply if no complete frame arrives before the deadline, and
        Unreachable if the port itself fails. ER401 is returned as a payload
        rather than raised here -- whether it is an error depends on the
        command, and only the caller knows that.
        """
        self._parser.reset()
        self._transport.discard_input()
        self._transport.write(encode(code, param))
        return self._read_frame(self._timeout if timeout is None else timeout)

    def _read_frame(self, timeout):
        deadline = self._clock() + timeout
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise NoReply("no reply within %.1fs" % timeout)
            if not self._transport.wait(remaining):
                continue
            chunk = self._transport.read()
            if not chunk:
                continue
            for byte in chunk:
                try:
                    payload = self._parser.push(byte)
                except ResponseTooLong:
                    # The line is producing bytes but no frames. Keep reading
                    # until the deadline rather than failing early: the noise
                    # may stop, and if it does not, NoReply is the honest
                    # answer and the one the caller already handles.
                    continue
                if payload is not None:
                    return payload

    def close(self):
        self._transport.close()
