"""The dot over the film, and the three routes behind the page's hidden menu.

PointerOverlay is driven one step() at a time against a fake mpv that records
what it was told, so "only the newest sample is drawn" and "a dot nobody moves
goes away" are counted rather than timed.
"""

import subprocess
import unittest
from unittest import mock

from support import ApiTest, Busy, Clock, api
from playstick import pointer
from playstick.projector.base import NoReply, Refused


class FakeMpv:
    def __init__(self, state="playing"):
        self.playing = state
        self.commands = []
        self.dims = {"w": 1280, "h": 720}

    def state(self):
        return self.playing

    def current_item(self):
        return {"id": "film1"} if self.playing != "idle" else None

    def get_property(self, name):
        self.commands.append(["get_property", name])
        return self.dims if name == "osd-dimensions" else None

    def command(self, cmd, quiet=False):
        self.commands.append(cmd)
        return {"error": "success"}

    def overlays(self):
        return [c for c in self.commands if c[0] == "osd-overlay"]


class Overlay(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.mpv = FakeMpv()
        self.overlay = pointer.PointerOverlay(self.mpv, clock=self.clock)

    def test_a_sample_is_drawn_where_it_was_asked_for(self):
        self.assertTrue(self.overlay.update(0.25, 0.5, True, "m", "green"))
        self.overlay.step()
        (cmd,) = self.mpv.overlays()
        self.assertEqual(cmd[:3], ["osd-overlay", pointer.OVERLAY_ID, "ass-events"])
        self.assertEqual(cmd[4:6], [1280, 720])
        self.assertIn("\\pos(320,360)", cmd[3])
        self.assertIn("\\1c&H00FF00&", cmd[3])

    def test_only_the_newest_of_a_burst_is_drawn(self):
        for i in range(100):
            self.overlay.update(i / 100, 0.5, True)
        self.overlay.step()
        self.assertEqual(self.overlay.draws, 1)
        self.assertIn("\\pos(1267,360)", self.mpv.overlays()[0][3])

    def test_nothing_new_draws_nothing(self):
        self.overlay.update(0.5, 0.5, True)
        self.overlay.step()
        self.overlay.step()
        self.assertEqual(self.overlay.draws, 1)

    def test_off_hides_it(self):
        self.overlay.update(0.5, 0.5, True)
        self.overlay.step()
        self.overlay.update(0.5, 0.5, False)
        self.overlay.step()
        self.assertEqual(self.mpv.overlays()[-1][:3],
                         ["osd-overlay", pointer.OVERLAY_ID, "none"])

    def test_a_dot_nobody_moves_goes_away(self):
        self.overlay.update(0.5, 0.5, True)
        self.overlay.step()
        self.clock.advance(pointer.HIDE_SECONDS - 0.1)
        self.overlay.step()
        self.assertEqual(len(self.mpv.overlays()), 1)
        self.clock.advance(0.2)
        self.overlay.step()
        self.assertEqual(self.mpv.overlays()[-1][2], "none")

    def test_with_no_film_nothing_is_sent_to_mpv(self):
        self.mpv.playing = "idle"
        self.assertFalse(self.overlay.update(0.5, 0.5, True))
        self.overlay.step()
        self.assertEqual(self.mpv.commands, [])

    def test_the_osd_size_is_asked_once_per_film(self):
        for x in (0.1, 0.2, 0.3):
            self.overlay.update(x, 0.5, True)
            self.overlay.step()
        asks = [c for c in self.mpv.commands if c[0] == "get_property"]
        self.assertEqual(len(asks), 1)

    def test_the_circle_passes_through_its_four_extremes(self):
        path = pointer.circle(0, 0, 10)
        self.assertTrue(path.startswith("m 0 -10 "))
        self.assertTrue(path.endswith(" 0 -10"))
        for extreme in ("10 0", "0 10", "-10 0"):
            self.assertIn(extreme, path)


class Routes(ApiTest):
    def setUp(self):
        super().setUp()
        self.mpv = FakeMpv()
        self.overlay = pointer.PointerOverlay(self.mpv)
        self.server.RequestHandlerClass.pointer = self.overlay
        api._pointer_window[:] = [0.0, 0]

    # -- the pointer

    def test_a_sample_reaches_the_overlay_clamped(self):
        body = self.assertJson(self.fetch("/api/pointer", method="POST",
                                          body={"x": 1.7, "y": -3, "on": True}))
        self.assertEqual(body, {"shown": True})
        self.assertEqual(self.overlay._want[:2], (1.0, 0.0))

    def test_unknown_colours_and_sizes_are_refused(self):
        for extra in ({"color": "&H0000FF&\\\\p0"}, {"size": "huge"}):
            with self.subTest(extra=extra):
                resp = self.fetch("/api/pointer", method="POST",
                                  body=dict({"x": 0.5, "y": 0.5}, **extra))
                self.assertEqual(resp.status, 400)

    def test_a_non_number_is_refused(self):
        for bad in ("left", None, float("nan")):
            with self.subTest(bad=bad):
                resp = self.fetch("/api/pointer", method="POST",
                                  body={"x": bad, "y": 0.5})
                self.assertEqual(resp.status, 400)

    def test_the_rate_is_capped_for_everybody(self):
        codes = [self.fetch("/api/pointer", method="POST",
                            body={"x": 0.5, "y": 0.5}).status
                 for _ in range(api.POINTER_MAX_RATE + 5)]
        self.assertIn(429, codes)
        self.assertEqual(codes.count(200), api.POINTER_MAX_RATE)

    # -- the projector

    def test_power_answers_with_the_projectionists_sentence(self):
        body = self.assertJson(self.fetch("/api/projector/power", method="POST",
                                          body={"on": True}))
        self.assertEqual(body["message"], "Switching on.")
        self.assertIn(("power", True), self.projectionist.calls)

    def test_power_needs_a_boolean(self):
        resp = self.fetch("/api/projector/power", method="POST", body={"on": "yes"})
        self.assertEqual(resp.status, 400)
        self.assertNotIn(("power", "yes"), self.projectionist.calls)

    def test_a_refusal_is_a_sentence(self):
        for error, code in ((Busy("A film is playing. Stop it first."), 409),
                            (Refused("ER401"), 409),
                            (NoReply("no reply"), 503)):
            with self.subTest(error=error):
                self.projectionist.power_error = error
                resp = self.fetch("/api/projector/power", method="POST",
                                  body={"on": False})
                self.assertEqual(resp.status, code)
                self.assertTrue(self.assertJson(resp, code)["error"])

    def test_settings_are_read_on_request(self):
        body = self.assertJson(self.fetch("/api/projector/settings"))
        self.assertEqual(body["power"], "on")
        self.assertEqual(body["settings"][0]["name"], "picture")

    def test_a_setting_change_answers_with_the_new_settings(self):
        body = self.assertJson(self.fetch(
            "/api/projector/setting", method="POST",
            body={"name": "picture", "value": "CN1"}))
        self.assertIn(("change", "picture", "CN1"), self.projectionist.calls)
        self.assertIn("settings", body)

    def test_a_setting_change_needs_two_strings(self):
        for body in ({"name": "picture"}, {"name": "picture", "value": 1},
                     {"value": "CN1"}):
            with self.subTest(body=body):
                resp = self.fetch("/api/projector/setting", method="POST",
                                  body=body)
                self.assertEqual(resp.status, 400)
        self.assertFalse([c for c in self.projectionist.calls
                          if c[0] == "change"])

    def test_a_setting_refusal_is_a_sentence(self):
        # error -> (status reading, status changing)
        cases = ((Busy("The projector is off. Turn it on first."), 409, 409),
                 (Refused("VPM:CN2 did not take"), 503, 409),
                 (NoReply("no reply"), 503, 503))
        for error, read_code, change_code in cases:
            with self.subTest(error=error):
                self.projectionist.power_error = error
                resp = self.fetch("/api/projector/settings")
                self.assertTrue(self.assertJson(resp, read_code)["error"])
                resp = self.fetch("/api/projector/setting", method="POST",
                                  body={"name": "picture", "value": "CN2"})
                self.assertTrue(self.assertJson(resp, change_code)["error"])

    # -- the clock

    def test_wake_signals_the_idle_clock(self):
        calls = []

        def fake(*args, timeout=45):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        self.library.add("0123456789abcdef", "Ponyo")
        with mock.patch.multiple(api, systemctl=fake,
                                 IDLE_UNIT="uxplay-idle.service"):
            body = self.assertJson(self.fetch("/api/clock/wake", method="POST"))
        self.assertEqual(body["message"], "The clock is back.")
        self.assertEqual(calls, [("kill", "--signal=SIGUSR1",
                                  "uxplay-idle.service")])

    def test_wake_during_a_film_says_why_not(self):
        self.player.playing = "playing"
        resp = self.fetch("/api/clock/wake", method="POST")
        self.assertEqual(resp.status, 409)


if __name__ == "__main__":
    unittest.main()
