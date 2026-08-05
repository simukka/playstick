#!/usr/bin/env python3
"""Talk to the projector by hand, over the serial cable, before trusting it.

WHY THIS IS NOT PART OF THE DAEMON

It shares no code with playstick/projector/ on purpose. The daemon's driver is
written to degrade quietly -- an unreachable projector must never be the reason
a child cannot watch a film -- and quiet degradation is exactly the wrong
behaviour for the question this script exists to answer, which is "did any
bytes come back at all". Here every frame is printed in both directions, and
silence is a headline rather than a shrug.

THE TWO THINGS IT SETTLES

1. IS THE CABLE THE RIGHT KIND. The adapter on this appliance reports USB ID
   0403:6015, which is the FTDI FT230X/FT231X. Some cables carrying that ID
   have a MAX3232 on board and speak real +/-12 V RS-232; plenty of others are
   bare 3.3 V TTL breakouts, which this projector cannot hear and which will
   never answer. Nothing in software can tell them apart -- but a `status` that
   times out with the projector demonstrably powered and a cable demonstrably
   plugged in is the answer.

2. IS THE PROTOCOL RIGHT. The implementation it was ported from was written
   from the PT-AE4000 manual and verified only against mocks, so every command
   string here is a hypothesis until this script gets a reply to it.

It also MEASURES the two numbers the daemon then has to be configured with:
how long the lamp takes to answer QPW with 001 after PON, and how long after
POF the projector will accept a PON again. `cycle` reports both. Those belong
in the README as measurements, next to every other tunable this project
derives from the device rather than from a guess.

USAGE

    sudo ./projector-probe.py status
    sudo ./projector-probe.py --verbose status
    sudo ./projector-probe.py on
    sudo ./projector-probe.py input HD3
    sudo ./projector-probe.py raw QPW
    sudo ./projector-probe.py watch
    sudo ./projector-probe.py cycle

The device is discovered under /dev/serial/by-id by default -- that path is
stable across reboots and across a second USB serial adapter appearing, which
/dev/ttyUSB0 is not. Pass --device to override.

Root, or membership of the dialout group, is needed to open the port.
"""

import argparse
import glob
import os
import select
import sys
import termios
import time


STX = 0x02
ETX = 0x03

BAUD = termios.B9600

# The projector answers every command with an ETX-terminated frame, and the
# replies are short -- "000", "HD3", "ER401". A second and a half is far longer
# than 9600 baud needs for five characters and still short enough that a probe
# against a dead cable finishes while you are watching it.
DEFAULT_TIMEOUT = 1.5

# The manual's post-lamp-on blackout: the projector accepts nothing for ten
# seconds after PON. Polling inside it produces timeouts that mean nothing.
LAMP_BLACKOUT = 10.0

# How long to keep asking before giving up on a lamp that will not light. A
# cold PT-AE series projector is normally answering well inside this.
READY_LIMIT = 120.0

# ...and how long to keep trying to restart one that was just switched off.
# The fans run a cool-down cycle during which PON is refused outright.
RESTRIKE_LIMIT = 180.0

INPUTS = ("CP1", "CP2", "VID", "SVD", "HD1", "HD2", "HD3", "RG1")

# Every inquiry, with a human name and a decoder for the payload. The daemon
# only needs QPW and QIN; the rest are here because a projector that answers
# all seven has proved the transport far better than one that answers one.
INQUIRIES = (
    ("QPW", "power", {"000": "standby", "001": "on"}),
    ("QIN", "input", None),
    ("QPM", "picture mode", None),
    ("QSH", "blank", {"0": "off", "1": "on"}),
    ("QFZ", "freeze", {"0": "off", "1": "on"}),
    ("QOT", "sleep timer", {"0": "off"}),
)


class Silence(Exception):
    """No ETX arrived before the deadline."""


def say(msg, *args):
    print(msg % args if args else msg, flush=True)


class Port:
    """A raw 9600 8N1 serial port, in about forty lines of termios.

    No pyserial: this script has to run on the appliance, which carries no
    Python packages beyond the standard library and has no room to start.
    """

    def __init__(self, path, timeout=DEFAULT_TIMEOUT, verbose=False):
        self.path = path
        self.timeout = timeout
        self.verbose = verbose
        # O_NONBLOCK on the open() itself matters: without it, opening a tty
        # blocks until carrier is asserted, and a cable with nothing on the
        # other end simply hangs here rather than failing.
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self._configure()
        except Exception:
            os.close(self.fd)
            raise

    def _configure(self):
        attrs = termios.tcgetattr(self.fd)
        cc = list(attrs[6])
        # Built from zero rather than masked out of whatever the port was left
        # in: the settings are fixed by the manual, so there is nothing in the
        # previous state worth preserving, and a flag cleared by accident here
        # is a fault that only shows up on one machine.
        iflag = 0          # no IXON/IXOFF (the projector uses no flow control),
                           # no ICRNL (0x0d in a payload must stay 0x0d)
        oflag = 0          # no ONLCR: the framing is binary, not text
        lflag = 0          # raw: no ICANON, no ECHO, no ISIG
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        # PARENB unset  -> no parity
        # CSTOPB unset  -> one stop bit
        # CRTSCTS unset -> no hardware flow control
        # CLOCAL is the load-bearing one: it tells the kernel to ignore modem
        # control lines, which a three-wire cable (TXD/RXD/GND) never asserts.
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, BAUD, BAUD, cc])
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- the wire

    def send(self, code, param=None):
        frame = bytearray([STX])
        frame += code.encode("ascii")
        if param:
            frame += b":" + param.encode("ascii")
        frame.append(ETX)
        # Anything still in the input buffer belongs to a previous exchange
        # that timed out. Keeping it would answer this command with the last
        # one's reply, which is the most confusing failure available here.
        termios.tcflush(self.fd, termios.TCIFLUSH)
        if self.verbose:
            say("  -> %s", hexdump(frame))
        os.write(self.fd, bytes(frame))

    def reply(self, timeout=None):
        """Bytes between STX and ETX, or Silence.

        The deadline covers the WHOLE reply rather than each byte, so a
        projector that answers slowly fails after `timeout` seconds and not
        after `timeout` x the length of what it was trying to say.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        raw = bytearray()
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.verbose and raw:
                    say("  <- %s (no ETX)", hexdump(raw))
                raise Silence()
            if not select.select([self.fd], [], [], remaining)[0]:
                continue
            chunk = os.read(self.fd, 64)
            if not chunk:
                continue
            raw += chunk
            for byte in chunk:
                if byte == STX:
                    # A mid-stream STX means everything before it was line
                    # noise. Start again rather than prefixing the payload
                    # with it.
                    payload.clear()
                elif byte == ETX:
                    if self.verbose:
                        say("  <- %s", hexdump(raw))
                    return bytes(payload)
                else:
                    payload.append(byte)

    def ask(self, code, param=None, timeout=None):
        self.send(code, param)
        return self.reply(timeout)


def hexdump(data):
    """Bytes as hex plus a printable rendering, because the framing bytes are
    control characters and the payload is ASCII, and both matter."""
    hexed = " ".join("%02x" % b for b in data)
    text = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in data)
    return "%s  |%s|" % (hexed, text)


def show(payload):
    return payload.decode("ascii", "replace") if payload else "(empty)"


# -- finding the port


def discover():
    """The first FTDI adapter under /dev/serial/by-id, or None.

    by-id rather than /dev/ttyUSB0 for the same reason the daemon uses it: the
    number depends on enumeration order, so a second serial device -- or a USB
    hub that comes up in a different order after a reboot -- silently moves it.
    """
    candidates = sorted(glob.glob("/dev/serial/by-id/*FTDI*")) \
        or sorted(glob.glob("/dev/serial/by-id/*"))
    return candidates[0] if candidates else None


def diagnose_missing():
    say("No serial port found under /dev/serial/by-id.")
    say("")
    ttys = sorted(glob.glob("/dev/ttyUSB*"))
    if ttys:
        say("There are ttyUSB devices, though: %s", ", ".join(ttys))
        say("Pass one with --device. That /dev/serial/by-id is empty usually")
        say("means udev is not populating it, not that the adapter is absent.")
    else:
        say("There are no /dev/ttyUSB* devices either, so the kernel has not")
        say("bound a driver to the adapter. Check in this order:")
        say("  lsusb | grep 0403          the adapter is enumerated at all")
        say("  lsmod | grep ftdi_sio      the driver is loaded")
        say("  modprobe ftdi_sio          load it if it is not")
        say("  dmesg | tail -20           what the kernel made of the device")


# -- what the commands do


def cmd_status(port, _args):
    """Every inquiry, one line each. The first is the one that matters."""
    answered = 0
    for code, label, decode in INQUIRIES:
        try:
            payload = port.ask(code)
        except Silence:
            say("  %-14s %s  SILENCE", label, code)
            continue
        answered += 1
        text = show(payload)
        if payload == b"ER401":
            say("  %-14s %s  ER401 (the projector does not know this command)",
                label, code)
        elif decode and text in decode:
            say("  %-14s %s  %s (%s)", label, code, decode[text], text)
        else:
            say("  %-14s %s  %s", label, code, text)
    say("")
    if answered:
        say("%d of %d inquiries answered. The cable and the framing are good.",
            answered, len(INQUIRIES))
        return 0
    report_silence()
    return 1


def report_silence():
    """The headline this script exists to produce."""
    say("NOTHING CAME BACK.")
    say("")
    say("Bytes went out and none returned. In order of likelihood:")
    say("")
    say("  1. The adapter is TTL, not RS-232. USB ID 0403:6015 is the FTDI")
    say("     FT230X/FT231X, which is sold both as a true RS-232 cable with a")
    say("     level shifter and as a bare 3.3 V UART breakout. The projector")
    say("     wants +/-12 V and cannot hear the second kind at all. If the")
    say("     plug on the projector end is a bare header rather than a moulded")
    say("     D-sub 9, this is the answer.")
    say("  2. The projector is unplugged from the mains. Standby still answers")
    say("     QPW; no power answers nothing.")
    say("  3. TXD and RXD are swapped. The manual specifies a STRAIGHT-THROUGH")
    say("     cable to the D-sub 9 female port -- pin 2 TXD, 3 RXD, 5 GND. A")
    say("     null-modem cable, which looks identical, will not work.")
    say("  4. Serial control is disabled in the projector's own menu.")
    say("")
    say("Re-run with --verbose to see the exact bytes written.")


def cmd_raw(port, args):
    code = args.code.upper()
    param = args.param
    if ":" in code and param is None:
        code, param = code.split(":", 1)
    if len(code) != 3:
        say("A command code is exactly three characters; got %r.", code)
        return 2
    try:
        payload = port.ask(code, param)
    except Silence:
        say("%s: SILENCE", code)
        return 1
    say("%s: %s", code, show(payload))
    return 0 if payload != b"ER401" else 1


def cmd_input(port, args):
    code = args.input.upper()
    if code not in INPUTS:
        say("Unknown input %r. The manual lists: %s", code, " ".join(INPUTS))
        return 2
    try:
        payload = port.ask("IIS", code)
    except Silence:
        say("IIS:%s: SILENCE", code)
        return 1
    if payload == b"ER401":
        say("IIS:%s: ER401 -- this projector has no such input.", code)
        return 1
    say("IIS:%s: %s", code, show(payload))
    # The echo is not proof; ask what it thinks it is on now.
    try:
        now = port.ask("QIN")
        say("QIN: %s%s", show(now),
            "" if now.decode("ascii", "replace") == code else "  (NOT what was asked for)")
    except Silence:
        say("QIN: SILENCE")
    return 0


def wait_for_power(port, want, limit, blackout=0.0):
    """Poll QPW until it reports `want`. Returns seconds taken, or None.

    Timeouts inside the blackout window are expected rather than interesting:
    the projector is documented to ignore everything for ten seconds after the
    lamp strikes, so a silent QPW there says nothing about the cable.
    """
    began = time.monotonic()
    deadline = began + limit
    last = None
    while time.monotonic() < deadline:
        elapsed = time.monotonic() - began
        try:
            payload = port.ask("QPW")
            state = {"000": "standby", "001": "on"}.get(
                payload.decode("ascii", "replace"), show(payload))
        except Silence:
            state = "silent" + (" (inside the blackout)" if elapsed < blackout else "")
        if state != last:
            say("  %6.1fs  %s", elapsed, state)
            last = state
        if state == want:
            return time.monotonic() - began
        time.sleep(1.0)
    return None


def cmd_on(port, _args):
    try:
        before = port.ask("QPW")
    except Silence:
        say("QPW: SILENCE -- not sending PON to a projector that cannot answer.")
        report_silence()
        return 1
    if before == b"001":
        say("Already on.")
        return 0
    say("PON ...")
    try:
        say("  reply: %s", show(port.ask("PON")))
    except Silence:
        # Worth continuing: PON is the one command standby is documented to
        # accept, and a missing echo is less conclusive than a missing QPW.
        say("  reply: SILENCE (carrying on -- watching QPW instead)")
    say("Waiting for the lamp. The first %.0f seconds are the documented",
        LAMP_BLACKOUT)
    say("blackout, during which the projector answers nothing.")
    took = wait_for_power(port, "on", READY_LIMIT, blackout=LAMP_BLACKOUT)
    if took is None:
        say("")
        say("Still not on after %.0f s. Either the lamp will not strike, or the",
            READY_LIMIT)
        say("projector was in its post-POF cool-down and refused the PON.")
        return 1
    say("")
    say("READY AFTER %.1f SECONDS.", took)
    say("Set player_projector_ready_seconds comfortably above this.")
    return 0


def cmd_off(port, _args):
    say("POF ...")
    try:
        say("  reply: %s", show(port.ask("POF")))
    except Silence:
        say("  reply: SILENCE")
        return 1
    return 0


def cmd_watch(port, _args):
    """QPW once a second, printing only transitions. For pressing buttons on
    the projector's own remote and seeing what the serial line says about it."""
    say("Polling QPW. Ctrl-C to stop.")
    last = None
    began = time.monotonic()
    try:
        while True:
            try:
                payload = port.ask("QPW")
                state = {"000": "standby", "001": "on"}.get(
                    payload.decode("ascii", "replace"), show(payload))
            except Silence:
                state = "silent"
            if state != last:
                say("  %6.1fs  %s", time.monotonic() - began, state)
                last = state
            time.sleep(1.0)
    except KeyboardInterrupt:
        say("")
        return 0


def cmd_cycle(port, _args):
    """Measure both numbers the daemon needs, in one run.

    Takes several minutes and switches the lamp off and on, so it is a
    deliberate act rather than something `status` does on the way past.
    """
    say("This powers the projector off, waits out the cool-down, and powers it")
    say("back on. Expect it to take three to five minutes.")
    say("")
    try:
        if port.ask("QPW") != b"001":
            say("The projector is not on. Run `on` first.")
            return 2
    except Silence:
        report_silence()
        return 1

    say("POF, then polling QPW until it reports standby:")
    port.ask("POF")
    cooled = wait_for_power(port, "standby", RESTRIKE_LIMIT)
    if cooled is None:
        say("Never reported standby. Stopping here.")
        return 1
    say("  reached standby after %.1f s", cooled)
    say("")

    # Standby is not the same as ready to restrike: the fans keep running and
    # PON is refused until they stop. The only way to find the boundary is to
    # keep asking.
    say("Retrying PON until one is accepted -- this finds the cool-down window:")
    began = time.monotonic()
    accepted = None
    while time.monotonic() - began < RESTRIKE_LIMIT:
        elapsed = time.monotonic() - began
        try:
            port.ask("PON")
        except Silence:
            pass
        time.sleep(LAMP_BLACKOUT + 2.0)
        try:
            if port.ask("QPW") == b"001":
                accepted = elapsed
                break
        except Silence:
            pass
        say("  %6.1fs  PON not accepted yet", elapsed)
    say("")
    if accepted is None:
        say("No PON was accepted within %.0f s of standby.", RESTRIKE_LIMIT)
        return 1
    say("MEASURED:")
    say("  off -> standby            %.1f s", cooled)
    say("  standby -> PON accepted   %.1f s after POF", cooled + accepted)
    say("")
    say("The daemon only powers off after 30 minutes of nothing, so it should")
    say("never meet this window -- but the prepare sequence retries PON across")
    say("player_projector_ready_seconds precisely in case somebody does.")
    return 0


COMMANDS = {
    "status": cmd_status,
    "on": cmd_on,
    "off": cmd_off,
    "input": cmd_input,
    "raw": cmd_raw,
    "watch": cmd_watch,
    "cycle": cmd_cycle,
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Talk to the projector over RS-232, by hand.",
        epilog="Run `status` first: it is the one that proves the cable.")
    parser.add_argument("--device", default="",
                        help="serial port (default: the first FTDI adapter "
                             "under /dev/serial/by-id)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="seconds to wait for a whole reply "
                             "(default: %(default)s)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print every frame in both directions, in hex")

    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "on", "off", "watch", "cycle"):
        sub.add_parser(name)
    p_input = sub.add_parser("input")
    p_input.add_argument("input", help="one of: " + " ".join(INPUTS))
    p_raw = sub.add_parser("raw")
    p_raw.add_argument("code", help="a three-letter command, e.g. QPW")
    p_raw.add_argument("param", nargs="?", default=None,
                       help="optional parameter, e.g. HD3 for IIS")

    args = parser.parse_args(argv)

    path = args.device or discover()
    if not path:
        diagnose_missing()
        return 2
    if not os.path.exists(path):
        say("No such device: %s", path)
        return 2

    say("port    %s", path)
    say("settings 9600 8N1, no flow control, %.1fs reply timeout", args.timeout)
    say("")

    try:
        port = Port(path, timeout=args.timeout, verbose=args.verbose)
    except PermissionError:
        say("Permission denied opening %s.", path)
        say("Run this with sudo, or add yourself to the dialout group and log")
        say("in again. The daemon does not need either -- it runs as root.")
        return 2
    except OSError as exc:
        say("Could not open %s: %s", path, exc)
        return 2

    with port:
        return COMMANDS[args.command](port, args)


if __name__ == "__main__":
    sys.exit(main())
