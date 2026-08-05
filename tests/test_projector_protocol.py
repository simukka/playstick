"""The wire: framing, parsing, and the five commands this appliance sends.

MOST OF THIS IS A PORT, AND THAT IS THE POINT

The vectors here come from the Rust implementation in the sibling ptae3000u
repository -- src/frame.rs, src/command.rs, src/query.rs and tests/driver.rs --
and are kept byte-for-byte identical on purpose. Two implementations of the
same protocol, written from the same manual, agreeing on the same bytes is
worth considerably more than either of them agreeing with itself. Where the
Python deliberately behaves differently from the Rust -- the reply deadline
covers a whole reply rather than each byte -- there is a test for the
difference rather than a silent divergence.

None of this proves the protocol is RIGHT. The manual could be wrong about the
projector, and the Rust crate was never run against one. That is what
scripts/projector-probe.py is for; these tests prove that whatever the manual
says, this code says it too.
"""

import unittest

from support import Clock, serial_io
from playstick.projector.base import NoReply, Refused, Unreachable
from playstick.projector.panasonic import PanasonicSerial
from playstick.projector.serial_io import (
    ETX, Link, ResponseParser, ResponseTooLong, STX, encode,
)


class ScriptedTransport:
    """Bytes in a list, and a clock that only moves while waiting.

    wait() advancing the clock when there is nothing to read is what makes a
    timeout test finish instantly instead of after a real second and a half.
    """

    def __init__(self, clock, chunks=()):
        self.clock = clock
        self.chunks = list(chunks)
        self.sent = []
        self.flushes = 0
        self.closed = False
        self.fail = None            # an exception to raise from write()

    def discard_input(self):
        self.flushes += 1

    def write(self, data):
        if self.fail is not None:
            raise self.fail
        self.sent.append(data)

    def wait(self, seconds):
        if self.chunks:
            return True
        self.clock.advance(seconds)
        return False

    def read(self, limit=64):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def link(chunks=(), timeout=1.5, clock=None):
    clock = clock or Clock()
    transport = ScriptedTransport(clock, chunks)
    return Link(transport, timeout, clock), transport


class Framing(unittest.TestCase):
    """src/frame.rs: frame_without_parameter, frame_with_parameter."""

    def test_a_command_with_no_parameter(self):
        self.assertEqual(encode("PON"), b"\x02PON\x03")
        self.assertEqual(encode("POF"), b"\x02POF\x03")
        self.assertEqual(encode("QPW"), b"\x02QPW\x03")

    def test_a_command_with_a_parameter(self):
        self.assertEqual(encode("IIS", "HD1"), b"\x02IIS:HD1\x03")
        self.assertEqual(encode("IIS", "HD3"), b"\x02IIS:HD3\x03")

    def test_an_empty_parameter_is_not_a_parameter(self):
        """No trailing colon: the manual's format omits both together."""
        self.assertEqual(encode("PON", ""), b"\x02PON\x03")

    def test_command_codes_are_exactly_three_bytes(self):
        # The Rust equivalent asserts, which on an appliance would take the
        # daemon down. A ValueError is reachable only from a programming
        # mistake in this repository, never from anything a client sends.
        for bad in ("PO", "PONX", ""):
            with self.assertRaises(ValueError):
                encode(bad)


class Parsing(unittest.TestCase):
    """src/frame.rs: the ResponseParser tests, one for one."""

    def feed(self, data):
        parser = ResponseParser()
        out = []
        for byte in data:
            payload = parser.push(byte)
            if payload is not None:
                out.append(payload)
        return out

    def test_stx_etx_framed_response(self):
        self.assertEqual(self.feed(b"\x02001\x03"), [b"001"])

    def test_bare_etx_terminated_response(self):
        """Recognised whether or not the projector prefixed it with STX."""
        self.assertEqual(self.feed(b"000\x03"), [b"000"])

    def test_stx_discards_leading_garbage(self):
        self.assertEqual(self.feed(b"junk\x02PON\x03"), [b"PON"])

    def test_reusable_across_frames(self):
        self.assertEqual(self.feed(b"\x02001\x03\x02ER401\x03"),
                         [b"001", b"ER401"])

    def test_overflow_resets_the_parser(self):
        parser = ResponseParser()
        for _ in range(serial_io.MAX_PAYLOAD):
            self.assertIsNone(parser.push(ord("x")))
        with self.assertRaises(ResponseTooLong):
            parser.push(ord("x"))
        # ...and recovers for the next frame, rather than staying poisoned.
        self.assertEqual(parser.push(ETX), b"")

    def test_an_empty_payload_is_not_no_payload(self):
        """b"" and None mean different things: a frame that said nothing, and
        no frame yet. Returning the first as the second would hang a read."""
        parser = ResponseParser()
        self.assertIsNone(parser.push(STX))
        self.assertEqual(parser.push(ETX), b"")


class Exchanges(unittest.TestCase):
    """tests/driver.rs: round trips over a mock transport."""

    def test_a_command_goes_out_and_a_payload_comes_back(self):
        conn, transport = link([b"\x02001\x03"])
        self.assertEqual(conn.exchange("QPW"), b"001")
        self.assertEqual(transport.sent, [b"\x02QPW\x03"])

    def test_a_reply_split_across_reads_is_reassembled(self):
        conn, _ = link([b"\x02H", b"D", b"3\x03"])
        self.assertEqual(conn.exchange("QIN"), b"HD3")

    def test_er401_is_returned_not_raised(self):
        """Whether ER401 is an error depends on the command, and only the
        driver above this knows which. See PanasonicSerial for the decision."""
        conn, _ = link([b"\x02ER401\x03"])
        self.assertEqual(conn.exchange("IIS", "RG1"), b"ER401")

    def test_silence_is_NoReply(self):
        clock = Clock()
        conn, _ = link([], timeout=1.5, clock=clock)
        with self.assertRaises(NoReply):
            conn.exchange("QPW")
        # The DELIBERATE difference from the Rust implementation, which reads
        # one byte at a time with a timeout on each and so can take
        # timeout x len(payload) to give up. Here the deadline is the whole
        # exchange, so the wait is the number that was configured.
        self.assertAlmostEqual(clock.now, 1.5, places=3)

    def test_a_truncated_reply_times_out_rather_than_returning_it(self):
        """Bytes with no ETX are not a short answer, they are no answer."""
        conn, _ = link([b"\x0200"])
        with self.assertRaises(NoReply):
            conn.exchange("QPW")

    def test_stale_input_is_flushed_before_each_command(self):
        """A reply that arrived after its command timed out must not be read
        as the answer to the NEXT one -- one timeout would otherwise leave
        every subsequent answer off by one, with nothing looking wrong."""
        conn, transport = link([b"\x02001\x03"])
        conn.exchange("QPW")
        self.assertEqual(transport.flushes, 1)

    def test_noise_before_a_reply_does_not_prevent_it(self):
        conn, _ = link([b"\xff\xfe\x02001\x03"])
        self.assertEqual(conn.exchange("QPW"), b"001")

    def test_a_runaway_line_gives_up_on_the_deadline(self):
        """Overflow inside an exchange keeps reading rather than failing at
        once: the noise may stop. When it does not, NoReply is the answer."""
        clock = Clock()
        conn, _ = link([b"x" * 200], timeout=1.0, clock=clock)
        with self.assertRaises(NoReply):
            conn.exchange("QPW")


class Panasonic(unittest.TestCase):
    """The five commands, and what the driver makes of the replies."""

    def driver(self, chunks=(), clock=None):
        clock = clock or Clock()
        conn, transport = link(chunks, clock=clock)
        projector = PanasonicSerial("/dev/null", clock=clock,
                                    open_link=lambda: conn)
        return projector, transport

    def test_power_state_maps_the_two_documented_replies(self):
        for payload, expected in ((b"001", "on"), (b"000", "standby")):
            projector, transport = self.driver([b"\x02" + payload + b"\x03"])
            self.assertEqual(projector.power_state(), expected)
            self.assertEqual(transport.sent, [b"\x02QPW\x03"])

    def test_an_undocumented_power_reply_is_unknown_not_an_error(self):
        """UNKNOWN is a state the caller already handles; an exception would
        be recorded as a fault and shown to somebody, and a projector saying
        something the manual does not list is not a fault."""
        projector, _ = self.driver([b"\x02002\x03"])
        self.assertEqual(projector.power_state(), "unknown")

    def test_power_on_and_off_send_the_right_frames(self):
        projector, transport = self.driver([b"\x02PON\x03"])
        projector.power_on()
        self.assertEqual(transport.sent, [b"\x02PON\x03"])

        projector, transport = self.driver([b"\x02POF\x03"])
        projector.power_off()
        self.assertEqual(transport.sent, [b"\x02POF\x03"])

    def test_set_input_sends_IIS(self):
        projector, transport = self.driver([b"\x02IIS\x03"])
        projector.set_input("HD3")
        self.assertEqual(transport.sent, [b"\x02IIS:HD3\x03"])

    def test_set_input_accepts_lower_case(self):
        projector, transport = self.driver([b"\x02IIS\x03"])
        projector.set_input("hd3")
        self.assertEqual(transport.sent, [b"\x02IIS:HD3\x03"])

    def test_an_input_this_family_does_not_have_is_Refused_before_the_wire(self):
        """Refused rather than ValueError, and this is load-bearing: the value
        comes from Ansible, and every caller in the daemon steps over a
        ProjectorError and starts the film anyway. A ValueError would escape
        that and turn a typo in a variable into a child unable to watch
        anything."""
        projector, transport = self.driver()
        with self.assertRaises(Refused):
            projector.set_input("HDMI1")
        self.assertEqual(transport.sent, [])

    def test_er401_from_the_projector_is_Refused(self):
        projector, _ = self.driver([b"\x02ER401\x03"])
        with self.assertRaises(Refused):
            projector.set_input("RG1")

    def test_current_input_returns_a_known_code(self):
        projector, _ = self.driver([b"\x02HD3\x03"])
        self.assertEqual(projector.current_input(), "HD3")

    def test_current_input_swallows_silence(self):
        """Only ever used to confirm that set_input landed, and "cannot tell"
        is a legitimate outcome the caller treats as "carry on"."""
        projector, _ = self.driver([])
        self.assertEqual(projector.current_input(), "")

    def test_current_input_ignores_an_answer_that_is_not_an_input(self):
        projector, _ = self.driver([b"\x02ZZZ\x03"])
        self.assertEqual(projector.current_input(), "")

    def test_silence_reaches_the_caller_as_NoReply(self):
        projector, _ = self.driver([])
        with self.assertRaises(NoReply):
            projector.power_state()

    def test_a_dead_port_is_reopened_once(self):
        """The case is an adapter unplugged and plugged back in: the fd this
        object holds refers to a device that no longer exists while a good one
        sits at the same path. One reopen recovers it without a restart."""
        clock = Clock()
        alive, _ = link([b"\x02001\x03", b"\x02001\x03"], clock=clock)
        dead, dead_transport = link([], clock=clock)
        dead.exchange = lambda *a, **k: (_ for _ in ()).throw(Unreachable("gone"))
        opened = []

        def opener():
            opened.append(1)
            return alive

        projector = PanasonicSerial("/dev/null", clock=clock, open_link=opener)
        self.assertEqual(projector.power_state(), "on")
        self.assertEqual(len(opened), 1)

        # Now the port dies under it, the way a replugged adapter does.
        projector._link = dead
        self.assertEqual(projector.power_state(), "on")
        self.assertEqual(len(opened), 2, "the dead link should have been reopened")
        self.assertTrue(dead_transport.closed, "the dead link should be closed")

    def test_a_port_that_will_not_open_is_not_retried(self):
        """Retrying an open that just failed only doubles the delay before the
        sequence carries on and starts the film."""
        attempts = []

        def opener():
            attempts.append(1)
            raise Unreachable("no such device")

        projector = PanasonicSerial("/dev/null", open_link=opener)
        with self.assertRaises(Unreachable):
            projector.power_state()
        self.assertEqual(len(attempts), 1)

    def test_close_releases_the_port(self):
        projector, transport = self.driver([b"\x02001\x03"])
        projector.power_state()
        projector.close()
        self.assertTrue(transport.closed)


if __name__ == "__main__":
    unittest.main()
