"""The Panasonic PT-AE4000 / PT-AE3000U, over RS-232C.

Starting a film needs five commands: PON, POF, IIS, QPW and QIN. The settings
view adds the rest of what this projector exposes over the cable, in SETTINGS
below -- and "the rest" is a measurement, not a reading of the manual.

WHAT THE SERIAL PORT OFFERS, MEASURED ON A PT-AE4000 (2026-11-23)

Every Q?? code was asked with the lamp lit; seven answered anything but ER401:
QPW, QIN, QPM, QSH, QFZ, QOT and QWM. Each setter was then found by writing
the current value back under every first letter -- VPM, OSH, OFZ, OOT, OWM --
and each value list is the values that read back afterwards. Nothing answers
for lamp power, brightness, contrast or colour, so none of those are here.

THE PROJECTOR ECHOES A VALUE IT IGNORES

VPM:VIV comes back as "VPM:VIV" exactly like VPM:CN1 does, and leaves the mode
where it was. A setter's reply therefore proves only that the frame arrived,
which is why change() asks again afterwards and only that answer counts.

In standby it echoes inquiries too -- QPM is answered "QPM" -- so a value read
then is not a choice and comes out as "", which is the same "will not say" the
page already shows for a projector that has not been asked.

    9600 baud, 8 data bits, no parity, one stop bit, no flow control
    STX (0x02) | three ASCII bytes | optional ":parameter" | ETX (0x03)

Two facts from the manual shape everything above this file:

  * In standby the projector ignores every command except PON.
  * For ten seconds after the lamp strikes it ignores everything, PON included.

Neither is handled here, and that is deliberate. Sleeping for ten seconds
inside power_on() would make the one part of this feature the child actually
watches -- "Waiting for the lamp..." -- invisible to the code that draws it.
The sequencing lives in projectionist.py, where it can be reported as it
happens.

WHAT IS AND IS NOT VERIFIED

The command strings come from the PT-AE4000 manual (TQBJ0313, pp. 42-44) by
way of a Rust implementation that was tested only against mocks. Every one of
them is confirmed by scripts/projector-probe.py against the real projector
before this driver is trusted -- see the README section. The AE3000U/AE4000
divergence the manual leaves ambiguous (CP2 versus RG1) is handled the way the
Rust crate handles it: both codes are accepted here, and the projector answers
ER401 for whichever it does not have.
"""

import threading
import time

from .base import (
    ON, STANDBY, UNKNOWN, NoReply, Projector, ProjectorError, Refused,
    Unreachable,
)
from .serial_io import ERROR_PAYLOAD, Link, SerialTransport


class PanasonicSerial(Projector):
    model = "pt-ae4000"

    # Every input code in the manual, for both models in the family. Validated
    # here so that a mistyped player_projector_input produces one clear line in
    # the journal rather than an ER401 nobody connects to the config.
    INPUTS = ("CP1", "CP2", "VID", "SVD", "HD1", "HD2", "HD3", "RG1")

    _POWER = {b"000": STANDBY, b"001": ON}

    # (name, label, inquiry, setter, ((value, label), ...)), in the order the
    # settings view lists them. The off timer's and the waveform monitor's
    # values are numbered rather than named: the projector takes 0-5 and 0-3,
    # and what each number does on screen has not been written down yet.
    SETTINGS = (
        ("picture", "Picture mode", "QPM", "VPM", (
            ("NOR", "Normal"), ("DYN", "Dynamic"),
            ("CN1", "Cinema 1"), ("CN2", "Cinema 2"), ("CN3", "Cinema 3"),
            ("CL1", "Colour 1"), ("CL2", "Colour 2"))),
        ("input", "Input", "QIN", "IIS", (
            ("HD1", "HDMI 1"), ("HD2", "HDMI 2"), ("HD3", "HDMI 3"),
            ("CP1", "Component 1"), ("CP2", "Component 2"),
            ("VID", "Video"), ("SVD", "S-video"), ("RG1", "Computer"))),
        ("blank", "Blank the picture", "QSH", "OSH", (
            ("0", "Off"), ("1", "On"))),
        ("freeze", "Freeze the picture", "QFZ", "OFZ", (
            ("0", "Off"), ("1", "On"))),
        ("timer", "Off timer", "QOT", "OOT", (
            ("0", "Off"), ("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"),
            ("5", "5"))),
        ("waveform", "Waveform monitor", "QWM", "OWM", (
            ("0", "Off"), ("1", "1"), ("2", "2"), ("3", "3"))),
    )

    # How long change() keeps asking before deciding a value did not take. On
    # the AE4000 a picture mode reads back at once; this is room for a slow
    # one, and short enough that a wrong value is reported while the finger
    # that chose it is still there.
    READBACK_SECONDS = 1.5
    READBACK_INTERVAL = 0.25

    def __init__(self, device, timeout=1.5, clock=time.monotonic,
                 open_link=None, sleep=time.sleep):
        self._device = device
        self._timeout = timeout
        self._clock = clock
        self._sleep = sleep
        # Injected by the tests, which drive a Link over a scripted transport.
        # Nothing else passes it.
        self._open_link = open_link or self._open_serial
        # The prepare sequence and the lamp keeper are different threads and
        # both reach for the port. One exchange at a time is not an
        # optimisation to relax later -- the manual requires waiting for each
        # reply before the next frame goes out.
        self._lock = threading.Lock()
        self._link = None

    def _open_serial(self):
        return Link(SerialTransport(self._device), self._timeout, self._clock)

    # -- the port

    def _exchange(self, code, param=None, timeout=None):
        """One command, one reply, reopening the port once if it has died.

        The retry covers exactly one case and no others: the adapter was
        unplugged and plugged back in, so the fd this object is holding refers
        to a device that no longer exists while a perfectly good one sits at
        the same path. A single reopen recovers that without anybody having to
        restart the daemon.

        NoReply is deliberately NOT retried. Silence means the projector is not
        answering, and asking again only doubles the time the child spends
        looking at a step that is going to fail anyway.
        """
        with self._lock:
            had_link = self._link is not None
            try:
                return self._talk(code, param, timeout)
            except Unreachable:
                self._drop()
                if not had_link:
                    raise
            return self._talk(code, param, timeout)

    def _talk(self, code, param, timeout):
        if self._link is None:
            self._link = self._open_link()
        try:
            payload = self._link.exchange(code, param, timeout)
        except ProjectorError:
            # NoReply leaves the port open on purpose: the projector being
            # quiet says nothing about the fd, and reopening on every timeout
            # would churn the device for no reason during a lamp warm-up,
            # which is exactly when timeouts are expected.
            raise
        if payload == ERROR_PAYLOAD:
            raise Refused("%s: the projector does not accept this command"
                          % (code if not param else "%s:%s" % (code, param)))
        return payload

    def _drop(self):
        if self._link is not None:
            self._link.close()
            self._link = None

    def close(self):
        with self._lock:
            self._drop()

    # -- power

    def power_state(self):
        """QPW. An answer that is not 000 or 001 is UNKNOWN, not an error.

        The distinction matters to the keeper in projectionist.py: UNKNOWN
        means "do not act on this", whereas an exception would be recorded as
        a fault and shown to somebody. A projector that says something the
        manual does not list is not broken, and neither is the cable.
        """
        return self._POWER.get(self._exchange("QPW"), UNKNOWN)

    def power_on(self):
        """PON. Returns as soon as the projector acknowledges.

        The lamp is NOT lit when this returns, and the projector is deaf for
        the next ten seconds. Whoever calls this owns the wait.
        """
        self._exchange("PON")

    def power_off(self):
        self._exchange("POF")

    # -- input

    def set_input(self, code):
        code = (code or "").upper()
        if code not in self.INPUTS:
            # Refused rather than ValueError: this is reached with a value from
            # the Ansible config, and every caller in the daemon is written to
            # step over a ProjectorError and start the film regardless. A
            # ValueError here would escape that and turn a typo in a variable
            # into a child unable to watch anything.
            raise Refused("%r is not an input on this projector; the manual "
                          "lists %s" % (code, " ".join(self.INPUTS)))
        self._exchange("IIS", code)

    def current_input(self):
        """QIN, or "" if the answer is not an input code we know.

        Empty rather than an exception for the same reason power_state answers
        UNKNOWN: this is only ever used to confirm that set_input landed, and
        "cannot tell" is a legitimate outcome that the caller already treats as
        "carry on".
        """
        try:
            payload = self._exchange("QIN")
        except (NoReply, Refused):
            return ""
        code = payload.decode("ascii", "replace").upper()
        return code if code in self.INPUTS else ""

    # -- the settings view

    def _setting(self, name):
        for entry in self.SETTINGS:
            if entry[0] == name:
                return entry
        raise Refused("this projector has no setting %r" % (name,))

    def _read(self, entry):
        """The setting's current value, or "" if the answer is not one of its
        values -- an echo in standby, or ER401. Silence is NOT swallowed: it
        means the projector is not there, and the caller says so once rather
        than waiting out a timeout per row."""
        _name, _label, inquiry, _setter, choices = entry
        try:
            payload = self._exchange(inquiry)
        except Refused:
            return ""
        value = payload.decode("ascii", "replace").upper()
        return value if value in dict(choices) else ""

    def settings(self):
        out = []
        for entry in self.SETTINGS:
            name, label, _inquiry, _setter, choices = entry
            out.append({"name": name, "label": label,
                        "value": self._read(entry),
                        "choices": [{"value": v, "label": text}
                                    for v, text in choices]})
        return out

    def change(self, name, value):
        entry = self._setting(name)
        _name, label, _inquiry, setter, choices = entry
        value = str(value or "").upper()
        if value not in dict(choices):
            raise Refused("%r is not a value of %s; it takes %s"
                          % (value, name, " ".join(v for v, _ in choices)))
        self._exchange(setter, value)
        # The echo proves nothing (see the module header), so ask until the
        # projector says the new value or the deadline passes.
        deadline = self._clock() + self.READBACK_SECONDS
        while True:
            now = self._read(entry)
            if now == value:
                return
            if self._clock() >= deadline:
                raise Refused("%s:%s did not take; the projector still says %r"
                              % (setter, value, now))
            self._sleep(self.READBACK_INTERVAL)
