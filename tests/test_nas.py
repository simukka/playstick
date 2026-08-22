"""Is the NAS awake, and does the button work.

Nothing here needs a NAS. The probe is pointed at a loopback listener that the
test opens and closes, which is the same question a real one answers -- can a
TCP connection be made to that port -- against something the test controls. The
wake packet is aimed at 127.0.0.1 and caught on a UDP socket, so its 102 bytes
are asserted as they go on the wire rather than as the function claims to build
them.

Module-level settings are patched into playstick.nas rather than into
playstick.config, for the same reason support.patched() patches into
playstick.http: the module bound those names at import with
`from .config import ...`, so patching config would change a value nothing
reads.
"""

import os
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "server"))
sys.dont_write_bytecode = True

from playstick import nas as nasmod                               # noqa: E402
from playstick.nas import NasWatcher, OFF, ON, UNKNOWN, WAKING     # noqa: E402


class Clock:
    """A hand-wound monotonic clock, so a sixty-second cooldown costs no time."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def listener():
    """A socket accepting connections on a kernel-picked loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    return sock, sock.getsockname()[1]


def closed_port():
    """A port nothing is listening on: bound, read for its number, released."""
    sock, port = listener()
    sock.close()
    return port


MAC = "00:11:32:aa:bb:cc"
MAC_BYTES = b"\x00\x11\x32\xaa\xbb\xcc"


class ParseMac(unittest.TestCase):
    def test_the_three_spellings_all_mean_the_same_six_bytes(self):
        for text in ("00:11:32:aa:bb:cc", "00-11-32-aa-bb-cc", "001132aabbcc",
                     "00:11:32:AA:BB:CC"):
            self.assertEqual(nasmod.parse_mac(text), MAC_BYTES, text)

    def test_surrounding_whitespace_survives_a_copy_paste(self):
        self.assertEqual(nasmod.parse_mac("  00:11:32:aa:bb:cc\n"), MAC_BYTES)

    def test_what_is_not_a_mac_is_none_rather_than_an_exception(self):
        """None is a real answer: it is what an unset nas_mac produces and what
        a typo produces, and both should cost the button rather than the
        daemon. Raising here would turn a misconfiguration into a service that
        will not start."""
        for text in ("", None, "00:11:32:aa:bb", "00:11:32:aa:bb:cc:dd",
                     "zz:11:32:aa:bb:cc", "00:11:32-aa:bb:cc", "10.0.1.83",
                     "0:1:2:3:4:5"):
            self.assertIsNone(nasmod.parse_mac(text), repr(text))


class MagicPacket(unittest.TestCase):
    def test_it_is_six_ff_bytes_then_the_mac_sixteen_times(self):
        packet = nasmod.magic_packet(MAC_BYTES)
        self.assertEqual(len(packet), 102)
        self.assertEqual(packet[:6], b"\xff" * 6)
        self.assertEqual(packet[6:], MAC_BYTES * 16)


class Power(unittest.TestCase):
    """What the page is told, in each of the four situations it can be in."""

    def test_no_host_configured_is_unknown_and_offers_nothing(self):
        """The shipped default, the development GUI, and any install without a
        NAS. The page must behave exactly as it did before this existed."""
        watcher = NasWatcher(host="", mac=MAC)
        self.assertEqual(watcher.snapshot(),
                         {"power": UNKNOWN, "can_wake": False})

    def test_before_the_first_probe_it_is_unknown_not_off(self):
        """'Not yet asked' and 'asked, and it is off' are different, and only
        the second one may draw a button that claims the NAS is off."""
        watcher = NasWatcher(host="127.0.0.1", mac=MAC)
        self.assertEqual(watcher.power(), UNKNOWN)

    def test_a_reachable_port_is_on(self):
        sock, port = listener()
        self.addCleanup(sock.close)
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", port):
            watcher = NasWatcher(host="127.0.0.1", mac=MAC)
            self.assertTrue(watcher.tick())
        self.assertEqual(watcher.snapshot(), {"power": ON, "can_wake": True})

    def test_an_unreachable_port_is_off(self):
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", closed_port()):
            watcher = NasWatcher(host="127.0.0.1", mac=MAC)
            self.assertFalse(watcher.tick())
        self.assertEqual(watcher.snapshot(), {"power": OFF, "can_wake": True})

    def test_a_bad_mac_reports_the_state_but_hides_the_button(self):
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", closed_port()):
            watcher = NasWatcher(host="127.0.0.1", mac="not-a-mac")
            watcher.tick()
        self.assertEqual(watcher.snapshot(), {"power": OFF, "can_wake": False})


class Cadence(unittest.TestCase):
    """The probe interval, which is the difference between a button whose
    result appears while somebody is still looking at the screen and one that
    seems not to have worked."""

    def setUp(self):
        self.clock = Clock()
        self.watcher = NasWatcher(host="127.0.0.1", mac=MAC, now=self.clock)

    def test_reachable_gets_the_cheap_cadence(self):
        self.watcher._reachable = True
        self.assertEqual(self.watcher.interval(), nasmod.NAS_PROBE_INTERVAL)

    def test_unreachable_is_watched_harder(self):
        self.watcher._reachable = False
        self.assertEqual(self.watcher.interval(), nasmod.NAS_PROBE_INTERVAL_DOWN)

    def test_the_wake_window_is_watched_hardest_and_then_expires(self):
        self.watcher._reachable = False
        self.watcher._waking_until = self.clock() + 100
        self.assertEqual(self.watcher.interval(), nasmod.NAS_PROBE_INTERVAL_WAKING)
        self.clock.advance(101)
        self.assertEqual(self.watcher.interval(), nasmod.NAS_PROBE_INTERVAL_DOWN)


class Wake(unittest.TestCase):
    def setUp(self):
        # Catch the packet on loopback rather than letting it out on a
        # broadcast address: the assertion is about the bytes, and a test that
        # put real traffic on the developer's LAN would be a rude one.
        self.sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sink.bind(("127.0.0.1", 0))
        self.sink.settimeout(5)
        self.addCleanup(self.sink.close)
        port = self.sink.getsockname()[1]
        patches = mock.patch.multiple(nasmod, NAS_WAKE_BROADCAST="127.0.0.1",
                                      NAS_WAKE_PORT=port)
        patches.start()
        self.addCleanup(patches.stop)
        self.clock = Clock()

    def watcher(self, mac=MAC, host="10.0.0.9", library=None):
        return NasWatcher(host=host, mac=mac, library=library, now=self.clock)

    def test_the_packet_that_goes_on_the_wire_is_the_magic_packet(self):
        watcher = self.watcher()
        ok, reason = watcher.wake()
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        payload, _ = self.sink.recvfrom(256)
        self.assertEqual(payload, b"\xff" * 6 + MAC_BYTES * 16)

    def test_waking_is_what_the_page_is_told_next(self):
        watcher = self.watcher()
        watcher._reachable = False
        watcher.wake()
        self.assertEqual(watcher.power(), WAKING)

    def test_the_window_expires_back_to_off_so_the_button_returns(self):
        watcher = self.watcher()
        watcher._reachable = False
        watcher.wake()
        self.clock.advance(nasmod.NAS_WAKE_WINDOW + 1)
        self.assertEqual(watcher.power(), OFF)

    def test_a_second_tap_inside_the_cooldown_sends_nothing(self):
        """Six phones in a room each have this button, and between them they
        should send one packet."""
        watcher = self.watcher()
        self.assertTrue(watcher.wake()[0])
        self.sink.recvfrom(256)
        ok, reason = watcher.wake()
        self.assertFalse(ok)
        self.assertTrue(reason)
        self.sink.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            self.sink.recvfrom(256)

    def test_the_cooldown_expires(self):
        watcher = self.watcher()
        watcher.wake()
        self.sink.recvfrom(256)
        self.clock.advance(nasmod.NAS_WAKE_COOLDOWN + 1)
        self.assertTrue(watcher.wake()[0])
        self.assertEqual(self.sink.recvfrom(256)[0][:6], b"\xff" * 6)

    def test_no_mac_refuses_with_a_sentence_rather_than_sending(self):
        ok, reason = self.watcher(mac="").wake()
        self.assertFalse(ok)
        self.assertIn("NAS", reason)

    def test_no_nas_at_all_refuses_differently(self):
        """Two refusals rather than one, because they are two different things
        for whoever is reading the page: nothing is set up, versus something is
        set up wrong."""
        ok, reason = self.watcher(mac="", host="").wake()
        self.assertFalse(ok)
        self.assertIn("no NAS", reason)


class Recovery(unittest.TestCase):
    """The off->on edge. Without the rescan the grid stays empty until the next
    scheduled scan -- up to five minutes -- which to whoever pressed the button
    is indistinguishable from the button not having worked."""

    class Lib:
        def __init__(self):
            self.rescans = 0

        def request_rescan(self):
            self.rescans += 1

    def setUp(self):
        # No systemd on a developer machine, and none wanted: the reset-failed
        # call is best-effort by design, so it is stubbed rather than skipped.
        self.run = mock.patch.object(nasmod.subprocess, "run").start()
        self.addCleanup(mock.patch.stopall)

    def test_coming_back_asks_for_a_rescan(self):
        lib = self.Lib()
        watcher = NasWatcher(host="127.0.0.1", mac=MAC, library=lib)
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", closed_port()):
            watcher.tick()
        self.assertEqual(lib.rescans, 0)
        sock, port = listener()
        self.addCleanup(sock.close)
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", port):
            watcher.tick()
        self.assertEqual(lib.rescans, 1)

    def test_staying_up_does_not_rescan_every_tick(self):
        """The scan walks the whole share over CIFS. Doing it once a minute
        because the NAS is merely still switched on would be a busy loop with a
        five-second Wi-Fi cost."""
        lib = self.Lib()
        watcher = NasWatcher(host="127.0.0.1", mac=MAC, library=lib)
        sock, port = listener()
        self.addCleanup(sock.close)
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", port):
            watcher.tick()
            watcher.tick()
            watcher.tick()
        self.assertEqual(lib.rescans, 0)

    def test_the_first_ever_probe_finding_it_up_is_not_a_recovery(self):
        """A daemon that started while the NAS was already on has nothing to
        recover from, and a rescan there would duplicate the one Library.run
        does at startup anyway."""
        lib = self.Lib()
        watcher = NasWatcher(host="127.0.0.1", mac=MAC, library=lib)
        sock, port = listener()
        self.addCleanup(sock.close)
        with mock.patch.object(nasmod, "NAS_PROBE_PORT", port):
            watcher.tick()
        self.assertEqual(lib.rescans, 0)

    def test_the_mount_unit_is_asked_of_systemd_rather_than_spelled_out(self):
        watcher = NasWatcher(host="127.0.0.1", mac=MAC)
        self.run.return_value = mock.Mock(returncode=0, stdout="srv-movies.mount\n")
        self.assertEqual(watcher._mount_unit(), "srv-movies.mount")
        self.assertEqual(self.run.call_args[0][0][:3],
                         ["systemd-escape", "--path", "--suffix=mount"])

    def test_a_configured_mount_unit_skips_the_derivation(self):
        with mock.patch.object(nasmod, "NAS_MOUNT_UNIT", "media-films.mount"):
            watcher = NasWatcher(host="127.0.0.1", mac=MAC)
            self.assertEqual(watcher._mount_unit(), "media-films.mount")
        self.run.assert_not_called()


class Thread(unittest.TestCase):
    def test_an_unconfigured_nas_costs_a_thread_that_exits(self):
        watcher = NasWatcher(host="", mac="")
        stopping = threading.Event()
        thread = threading.Thread(target=watcher.run, args=(stopping,))
        thread.start()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_a_bad_sample_does_not_take_the_keeper_down(self):
        """Same rule as the lamp keeper: the thread not running is the failure
        nobody notices, so one exception must cost one tick."""
        watcher = NasWatcher(host="127.0.0.1", mac=MAC)
        stopping = threading.Event()
        ticks = []

        def boom():
            ticks.append(1)
            if len(ticks) >= 3:
                stopping.set()
            raise RuntimeError("probe exploded")

        with mock.patch.object(watcher, "tick", boom), \
             mock.patch.object(nasmod, "NAS_PROBE_INTERVAL_DOWN", 0.01):
            thread = threading.Thread(target=watcher.run, args=(stopping,))
            thread.start()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(ticks), 3)


if __name__ == "__main__":
    unittest.main()
