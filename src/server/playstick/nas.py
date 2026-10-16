"""Is the NAS awake, and if not, wake it.

The films live on a CIFS share that a systemd .automount pulls in on demand.
When the NAS is off the mount fails, Library._walk catches the OSError, and the
page says "Can't reach the movies right now". That sentence was accurate and
useless: it named the fix and offered no way to apply it, so somebody had to
walk to the NAS and press its button.

This module is the missing half. It answers two questions and does one thing:

    snapshot()  is the NAS awake?      -- polled by /api/status
    wake()      make it awake          -- POST /api/nas/wake

WHY A SEPARATE PROBE RATHER THAN library.available. The library flag is derived
from a walk of the share that runs every SCAN_INTERVAL (300 s by default), and
it conflates three different situations: the NAS is off, the NAS is on but the
share is misconfigured, and the NAS is on and the share is fine but empty. A
button that offers to switch on a NAS which is already running would be a
button that does nothing, twice. More practically, a five-minute scan interval
cannot tell anybody whether the packet they just sent worked -- and the minute
after the tap is the only minute anyone is watching.

WHY TCP 445 RATHER THAN ICMP. A Synology answers ping well before smbd is
listening. A green light that means "responds to ping" would promise films that
are not there yet, and a page that then contradicts its own button is worse than
a page with no button. 445 is the port the mount itself will use, so an answer
here means the next scan has a real chance of succeeding -- which is why the
off->on edge triggers one.

Structurally this is the projectionist's lamp keeper: a thread that samples on
a timer, records rather than raises, and exposes a snapshot the HTTP layer
reads. See Projectionist.run for the original.
"""

import re
import socket
import subprocess
import threading
import time

from .config import (
    LIBRARY, NAS_HOST, NAS_MAC, NAS_MOUNT_UNIT, NAS_PROBE_INTERVAL,
    NAS_PROBE_INTERVAL_DOWN, NAS_PROBE_INTERVAL_WAKING, NAS_PROBE_PORT,
    NAS_PROBE_TIMEOUT, NAS_WAKE_BROADCAST, NAS_WAKE_COOLDOWN, NAS_WAKE_PORT,
    NAS_WAKE_WINDOW, log,
)

# The four values `power` takes, mirroring Projector.power_state deliberately:
# the page should not need a second vocabulary for a second device.
ON = "on"
OFF = "off"
WAKING = "waking"
UNKNOWN = "unknown"

# Six hex octets, separated by colons, hyphens, or nothing at all. Anything
# else is not a MAC and is refused rather than guessed at.
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2})([:-]?)(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2}$")
_SEPARATORS = str.maketrans("", "", ":-")


def parse_mac(text):
    """Six bytes, or None.

    None is a real answer here and not an error path: it is what an unset
    nas_mac produces, and what a typo produces, and in both cases the right
    behaviour is the same -- the page keeps its sentence and loses its button.
    Raising would turn a misconfiguration into a daemon that will not start,
    which is a much worse trade for a feature that is a convenience.
    """
    if not text:
        return None
    text = str(text).strip()
    if not _MAC_RE.match(text):
        return None
    return bytes.fromhex(text.translate(_SEPARATORS))


def magic_packet(mac):
    """The wake-on-LAN payload: six 0xFF bytes, then the MAC sixteen times.

    102 bytes, and the shape is the whole specification -- the NIC's wake
    circuitry scans arriving frames for this pattern regardless of protocol or
    port, which is why the UDP port below is a convention rather than a
    requirement.
    """
    return b"\xff" * 6 + mac * 16


class NasWatcher:
    """One thread, one socket at a time, and a snapshot anybody may read.

    Every field is guarded by the lock because /api/status is served from a
    thread per request and the probe runs on its own; the critical sections are
    all field reads and writes, so contention is not a consideration.
    """

    def __init__(self, host=NAS_HOST, mac=NAS_MAC, library=None, now=time.monotonic):
        self._host = (host or "").strip()
        self._mac = parse_mac(mac)
        self._library = library
        self._now = now
        self._lock = threading.Lock()
        # None rather than False: "not yet asked" and "asked, and it is off"
        # are different, and only the second one may draw a button.
        self._reachable = None
        self._waking_until = 0.0
        self._last_wake = 0.0
        self._wake = threading.Event()
        if self._host and mac and self._mac is None:
            # Worth a line: this is the difference between a button that is
            # absent because there is no NAS and one that is absent because
            # somebody typed the MAC wrong, and nothing else would say so.
            log("nas: %r is not a MAC address; wake-on-LAN is off", mac)

    # -- what the page sees

    @property
    def configured(self):
        return bool(self._host)

    def power(self):
        with self._lock:
            return self._power_locked()

    def _power_locked(self):
        if not self._host:
            return UNKNOWN
        if self._reachable:
            return ON
        # The wake window outranks "off" so the page can say "this takes a
        # minute" instead of offering a button that has just been pressed.
        if self._now() < self._waking_until:
            return WAKING
        return OFF if self._reachable is False else UNKNOWN

    def snapshot(self):
        """The /api/status payload. Shaped like the projector's, which is the
        only other device the page reasons about."""
        with self._lock:
            return {
                "power": self._power_locked(),
                # Whether to draw a button at all. False for an unconfigured
                # NAS and for a MAC that would not parse, so the page never
                # offers an action that cannot work.
                "can_wake": bool(self._host and self._mac),
            }

    # -- the one thing it does

    def wake(self):
        """Send the packet. Returns (ok, reason); reason is shown verbatim.

        The cooldown lives here rather than in the page because there is more
        than one page: six phones in a room each have this button, and they
        should between them send one packet.
        """
        if not self._mac:
            return False, ("I don't know how to switch the NAS on."
                           if self._host else "There's no NAS set up.")
        now = self._now()
        with self._lock:
            if now - self._last_wake < NAS_WAKE_COOLDOWN:
                return False, "Already waking it up — give it a minute."
            self._last_wake = now
            self._waking_until = now + NAS_WAKE_WINDOW
        try:
            self._send(magic_packet(self._mac))
        except OSError as exc:
            log("nas: wake-on-LAN failed: %s", exc)
            return False, "Couldn't send the wake-up. Try the NAS's own button."
        log("nas: wake-on-LAN sent to %s via %s:%d",
            self._mac.hex(":"), NAS_WAKE_BROADCAST, NAS_WAKE_PORT)
        # Look again promptly rather than waiting out the current interval:
        # the page is polling and somebody is watching it.
        self._wake.set()
        return True, ""

    def _send(self, payload):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(payload, (NAS_WAKE_BROADCAST, NAS_WAKE_PORT))
        finally:
            sock.close()

    # -- the thread

    def probe(self):
        """One TCP connect, closed immediately. True, False, or None when there
        is nothing to ask."""
        if not self._host:
            return None
        try:
            with socket.create_connection((self._host, NAS_PROBE_PORT),
                                          NAS_PROBE_TIMEOUT):
                return True
        except OSError:
            # Refused, unreachable and timed out are all the same answer to the
            # only question being asked: can the share be reached. Which of
            # them it was is not something the page could act on differently.
            return False

    def interval(self):
        with self._lock:
            if self._now() < self._waking_until:
                return NAS_PROBE_INTERVAL_WAKING
            return NAS_PROBE_INTERVAL if self._reachable else NAS_PROBE_INTERVAL_DOWN

    def tick(self):
        """Sample once and act on a change. Returns the new reachability."""
        reachable = self.probe()
        if reachable is None:
            return None
        with self._lock:
            was = self._reachable
            self._reachable = reachable
            if reachable:
                # Whether it was us or somebody's finger on the NAS, the wait
                # is over and the page should stop saying "waking".
                self._waking_until = 0.0
        if was != reachable:
            log("nas: %s is %s", self._host, "up" if reachable else "down")
        if reachable and was is False:
            self._recover()
        return reachable

    def _recover(self):
        """The NAS just came back. Get the films in front of somebody.

        Two steps, and the second is the one that matters. Without the rescan
        the grid stays empty until the next scheduled scan -- up to
        SCAN_INTERVAL, five minutes by default -- which for whoever pressed the
        button is indistinguishable from the button not having worked.
        """
        unit = self._mount_unit()
        if unit:
            # Best effort, and belt-and-braces: a mount that failed against a
            # sleeping NAS leaves its .mount unit failed, and a failed unit
            # that has hit StartLimitBurst will not be retried by the next
            # automount trigger. Clearing it costs one subprocess on an edge
            # that happens at most once a night.
            try:
                subprocess.run(["systemctl", "reset-failed", unit],
                               capture_output=True, timeout=10)
            except Exception as exc:                 # noqa: BLE001
                log("nas: reset-failed %s: %s", unit, exc)
        if self._library is not None:
            self._library.request_rescan()

    def _mount_unit(self):
        """The .mount unit for the library path, asked of systemd rather than
        spelled out. Cached after the first call; the answer cannot change."""
        if NAS_MOUNT_UNIT:
            return NAS_MOUNT_UNIT
        if not hasattr(self, "_derived_unit"):
            self._derived_unit = ""
            try:
                res = subprocess.run(
                    ["systemd-escape", "--path", "--suffix=mount", LIBRARY],
                    capture_output=True, text=True, timeout=10)
                if res.returncode == 0:
                    self._derived_unit = res.stdout.strip()
            except Exception as exc:                 # noqa: BLE001
                log("nas: cannot derive the mount unit for %s: %s", LIBRARY, exc)
        return self._derived_unit

    def run(self, stopping):
        """The keeper. Same shape as Projectionist.run: never let one bad
        sample take the thread down, because the thread not running is the
        failure nobody notices."""
        if not self._host:
            log("nas: no host configured, not watching")
            return
        while not stopping.is_set():
            try:
                self.tick()
            except Exception as exc:                 # noqa: BLE001
                log("nas keeper: %s", exc)
            # Woken early by wake(), so the first look after a packet is
            # seconds away rather than a whole interval.
            self._wake.wait(self.interval())
            self._wake.clear()
