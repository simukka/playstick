"""The probe behind the admin app's projector settings, and its route.

What matters most here is what the probe does NOT do: send anything but a
question, open a second fd on the port the daemon already holds, or raise when
a port is not there. The discovery itself is exercised against scratch
directories of symlinks and hand-built EDID blocks, so none of it needs
hardware.
"""

import os
import shutil
import tempfile
import unittest
from unittest import mock

from support import ApiTest, Clock, api
from test_projector_protocol import link
from playstick.projector import probe
from playstick.projector.base import Unreachable
from playstick.projector.panasonic import PanasonicSerial


def frame(payload):
    return b"\x02" + payload + b"\x03"


def edid_block(name):
    """A minimal EDID base block whose first descriptor is a monitor name."""
    block = bytearray(128)
    block[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    text = name.encode("ascii")[:13]
    text = text + (b"\x0a" if len(text) < 13 else b"")
    block[54:59] = b"\x00\x00\x00\xfc\x00"
    block[59:59 + len(text)] = text
    for i in range(59 + len(text), 72):
        block[i] = 0x20
    return bytes(block)


class Scratch(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="probe-")
        self.addCleanup(shutil.rmtree, self.root)
        self.by_id = os.path.join(self.root, "by-id")
        self.dev = os.path.join(self.root, "dev")
        self.drm = os.path.join(self.root, "drm")
        for d in (self.by_id, self.dev, self.drm):
            os.makedirs(d)

    def tty(self, name):
        path = os.path.join(self.dev, name)
        open(path, "w").close()
        return path

    def link_by_id(self, name, target):
        tty = os.path.join(self.dev, target)
        if not os.path.exists(tty):
            self.tty(target)
        path = os.path.join(self.by_id, name)
        os.symlink(tty, path)
        return path

    def connector(self, name, edid, status="connected"):
        d = os.path.join(self.drm, name)
        os.makedirs(d)
        with open(os.path.join(d, "edid"), "wb") as fh:
            fh.write(edid)
        with open(os.path.join(d, "status"), "w") as fh:
            fh.write(status + "\n")


class SerialPorts(Scratch):
    def test_ftdi_by_id_links_come_first(self):
        self.link_by_id("usb-Prolific_123-if00-port0", "ttyUSB1")
        ftdi = self.link_by_id("usb-FTDI_FT230X_ABC-if00-port0", "ttyUSB0")
        ports = probe.serial_ports(self.by_id, self.dev)
        self.assertEqual(ports[0]["path"], ftdi)
        self.assertEqual(ports[0]["target"], "ttyUSB0")
        self.assertTrue(ports[0]["ftdi"])
        self.assertFalse(ports[1]["ftdi"])

    def test_a_tty_with_no_by_id_link_is_still_offered_once(self):
        self.link_by_id("usb-FTDI_X-if00-port0", "ttyUSB0")
        self.tty("ttyACM0")
        paths = [p["target"] for p in probe.serial_ports(self.by_id, self.dev)]
        self.assertEqual(paths, ["ttyUSB0", "ttyACM0"])

    def test_nothing_plugged_in_is_an_empty_list(self):
        self.assertEqual(probe.serial_ports(self.by_id, self.dev), [])
        self.assertEqual(probe.serial_ports("/no/such", "/no/such"), [])


class Edid(Scratch):
    def test_the_monitor_name_descriptor_is_read(self):
        self.assertEqual(probe.edid_name(edid_block("AE-4000")), "AE-4000")

    def test_a_thirteen_byte_name_has_no_terminator(self):
        self.assertEqual(probe.edid_name(edid_block("ABCDEFGHIJKLM")),
                         "ABCDEFGHIJKLM")

    def test_garbage_is_no_name(self):
        self.assertEqual(probe.edid_name(b""), "")
        self.assertEqual(probe.edid_name(b"\x00" * 128), "")

    def test_connectors_report_name_and_whether_connected(self):
        self.connector("card0-HDMI-A-1", edid_block("AE-4000"))
        self.connector("card0-HDMI-A-2", b"", status="disconnected")
        self.assertEqual(probe.displays(self.drm), [
            {"connector": "card0-HDMI-A-1", "name": "AE-4000",
             "connected": True}])

    def test_model_names_map_to_drivers(self):
        self.assertEqual(probe.guess_model(["AE-4000"]), "pt-ae4000")
        self.assertEqual(probe.guess_model(["PT-AE3000"]), "pt-ae3000u")
        self.assertEqual(probe.guess_model(["LG TV", ""]), "")


class Asking(unittest.TestCase):
    def scripted(self, chunks):
        clock = Clock()
        conn, transport = link(chunks, clock=clock)
        made = []

        def opener(path, timeout=1.5):
            projector = PanasonicSerial(path, timeout=timeout, clock=clock,
                                        open_link=lambda: conn)
            made.append(projector)
            return projector
        return opener, transport, made

    def test_power_and_input_are_asked_and_nothing_else(self):
        opener, transport, _ = self.scripted([frame(b"001"), frame(b"HD3")])
        result = probe.ask("/dev/x", open_projector=opener)
        self.assertEqual(result, {"answers": True, "power": "on",
                                  "input": "HD3", "error": ""})
        self.assertEqual(transport.sent, [b"\x02QPW\x03", b"\x02QIN\x03"])
        self.assertTrue(transport.closed)

    def test_silence_is_an_error_string_not_an_exception(self):
        opener, transport, _ = self.scripted([])
        result = probe.ask("/dev/x", open_projector=opener)
        self.assertFalse(result["answers"])
        self.assertIn("no reply", result["error"])
        self.assertEqual(transport.sent, [b"\x02QPW\x03"])

    def test_a_port_that_will_not_open_is_reported(self):
        def opener(path, timeout=1.5):
            raise Unreachable("could not open %s" % path)
        result = probe.ask("/dev/x", open_projector=opener)
        self.assertEqual(result["error"], "could not open /dev/x")

    def test_the_daemons_own_port_goes_through_its_own_driver(self):
        opener, transport, _ = self.scripted([frame(b"000")])
        live = opener("/dev/null")

        def refuse(path, timeout=1.5):
            raise AssertionError("opened a second fd on the live port")
        result = probe.ask("/dev/null", live=live, open_projector=refuse)
        self.assertEqual(result["power"], "standby")
        # The daemon's link is left open for the keeper to carry on using.
        self.assertFalse(transport.closed)


class Payload(Scratch):
    def run_probe(self, answers, **kw):
        return probe.probe(by_id=self.by_id, dev=self.dev, drm=self.drm,
                           ask_port=lambda path, live, timeout: dict(
                               answers.get(os.path.basename(
                                   os.path.realpath(path)),
                                   {"answers": False, "power": "",
                                    "input": "", "error": "no reply"})),
                           **kw)

    def test_the_answering_port_and_edid_model_are_suggested(self):
        self.link_by_id("usb-FTDI_A-if00-port0", "ttyUSB0")
        answering = self.link_by_id("usb-FTDI_B-if00-port0", "ttyUSB1")
        self.connector("card0-HDMI-A-1", edid_block("AE-3000"))
        out = self.run_probe({"ttyUSB1": {"answers": True, "power": "on",
                                          "input": "HD2", "error": ""}})
        self.assertEqual(out["suggested"], {"model": "pt-ae3000u",
                                            "device": answering,
                                            "input": "HD2"})

    def test_with_no_answer_the_first_ftdi_port_and_no_model(self):
        ftdi = self.link_by_id("usb-FTDI_A-if00-port0", "ttyUSB0")
        out = self.run_probe({})
        self.assertEqual(out["suggested"],
                         {"model": "", "device": ftdi, "input": ""})

    def test_an_answer_without_edid_suggests_the_panasonic_driver(self):
        self.link_by_id("usb-FTDI_A-if00-port0", "ttyUSB0")
        out = self.run_probe({"ttyUSB0": {"answers": True, "power": "standby",
                                          "input": "", "error": ""}})
        self.assertEqual(out["suggested"]["model"], "pt-ae4000")
        # A lamp that is not lit is not showing anything to be on.
        self.assertEqual(out["suggested"]["input"], "")

    def test_the_lists_match_the_driver(self):
        out = self.run_probe({})
        self.assertEqual(sorted(i["id"] for i in out["inputs"]),
                         sorted(PanasonicSerial.INPUTS))
        from playstick.projector import MODELS
        self.assertEqual({m["id"] for m in out["models"]},
                         set(MODELS) - {"fake"})


class Route(ApiTest):
    def test_the_route_hands_over_the_live_driver_and_the_config(self):
        seen = {}

        def fake_probe(**kw):
            seen.update(kw)
            return {"ports": [], "suggested": {}}
        with mock.patch.object(api.projector_probe, "probe", fake_probe), \
                mock.patch.multiple(api, PROJECTOR_MODEL="pt-ae4000",
                                    PROJECTOR_DEVICE="/dev/x",
                                    PROJECTOR_INPUT="HD3"):
            body = self.assertJson(self.fetch("/api/admin/projector/probe"))
        self.assertEqual(body, {"ports": [], "suggested": {}})
        self.assertEqual(seen["configured"], {"model": "pt-ae4000",
                                              "device": "/dev/x",
                                              "input": "HD3"})
        self.assertIsNone(seen["live"])

    def test_the_real_probe_answers_on_a_machine_with_nothing_plugged_in(self):
        # Ports stubbed out: a test must not send frames to whatever serial
        # adapter happens to be on the machine running it.
        with mock.patch.object(api.projector_probe, "serial_ports",
                               lambda *a: []):
            body = self.assertJson(self.fetch("/api/admin/projector/probe"))
        for key in ("configured", "models", "inputs", "ports", "displays",
                    "suggested"):
            self.assertIn(key, body)


if __name__ == "__main__":
    unittest.main()
