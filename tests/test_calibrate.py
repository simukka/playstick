"""The calibration wizard's projector side, and the profiles it saves.

Calibration is driven one step() at a time against a fake mpv that records
what it was told, the way test_pointer drives PointerOverlay, so "only the
newest state is drawn" and "a session nobody holds ends" are counted rather
than timed. scene_ass() is pure, and is read as text.
"""

import json
import math
import os
import socket
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import ApiTest, Busy, Clock, FakePlayer, real_store  # noqa: E402
from playstick import calibrate, profiles, ws                     # noqa: E402
from playstick.player import Player                               # noqa: E402
from playstick.stage import Stage                                 # noqa: E402
from test_ws import client_frame                                  # noqa: E402


class FakeMpv:
    def __init__(self, state="calibrating"):
        self.playing = state
        self.commands = []

    def state(self):
        return self.playing

    def get_property(self, name):
        return {"w": 1280, "h": 720} if name == "osd-dimensions" else None

    def command(self, cmd, quiet=False):
        self.commands.append(cmd)
        return {"error": "success"}

    def overlays(self):
        return [c for c in self.commands if c[0] == "osd-overlay"]


def scene(**fields):
    fields.setdefault("step", "intro")
    return calibrate.parse_scene(fields)


TEL = {"a": 10.5, "b": -20.25, "g": 3, "angle": 90, "touch": [0.25, 0.5],
       "mask": 0b10010, "dot": [0.5, 0.5]}


def good_profile(**over):
    body = {
        "name": "Kid's iPhone",
        "pointer": {"yaw_across": 40, "pitch_across": 25, "yaw_sign": 1,
                    "pitch_sign": -1, "smoothing": 0.35, "deadzone": 0.2},
        "tilt": {"press": {"left": 12, "right": 11, "up": 10, "down": 14},
                 "release_ratio": 0.66, "roll_sign": 1, "pitch_sign": -1},
        "buttons": {"A": 32, "B": 16, "START": 64, "SELECT": 128},
        "checks": {"targets": [0.01, 0.02], "buttons_ok": True},
    }
    body.update(over)
    return body


class Messages(unittest.TestCase):
    def test_a_sample_is_read_and_clamped(self):
        tel = calibrate.parse_tel(dict(TEL, a=999, touch=[2, -1]))
        self.assertEqual(tel["a"], 360.0)
        self.assertEqual(tel["touch"], (1.0, 0.0))
        self.assertEqual(tel["mask"], 0b10010)

    def test_nan_and_infinity_are_not_angles(self):
        tel = calibrate.parse_tel({"a": math.nan, "b": math.inf, "g": "3"})
        self.assertIsNone(tel["a"])
        self.assertIsNone(tel["b"])
        self.assertIsNone(tel["g"])

    def test_a_step_nobody_knows_is_not_a_scene(self):
        self.assertIsNone(calibrate.parse_scene({"step": "hack"}))
        self.assertIsNone(calibrate.parse_scene("intro"))

    def test_words_outside_the_tables_are_dropped(self):
        s = scene(step="buttons", button="TURBO", edge="middle",
                  targets=[{"x": 0.5, "y": 0.5, "r": 0.05, "state": "evil"},
                           {"x": 0.1, "y": 0.1, "r": 0.05, "state": "hit"}])
        self.assertIsNone(s["button"])
        self.assertIsNone(s["edge"])
        self.assertEqual([t["state"] for t in s["targets"]], ["hit"])

    def test_a_name_cannot_open_a_tag(self):
        self.assertEqual(calibrate.clean_name("{\\fs200}Kid\n's\\N phone"),
                         "fs200Kid'sN phone")


class Scene(unittest.TestCase):
    def test_every_step_draws_its_own_sentence(self):
        for step in calibrate.STEPS:
            with self.subTest(step=step):
                ass = calibrate.scene_ass(scene(step=step, edge="left"), None,
                                          1280, 720)
                self.assertIn(calibrate.TEXTS[step].split(" ")[0], ass)

    def test_the_sweep_lights_the_edge_it_asks_for(self):
        ass = calibrate.scene_ass(scene(step="sweep", edge="right", hold=0.5),
                                  None, 1280, 720)
        self.assertIn("the right edge", ass)
        self.assertIn("&H00D7FF&", ass)

    def test_targets_are_drawn_where_they_are(self):
        s = scene(step="targets", targets=[
            {"x": 0.25, "y": 0.5, "r": 0.05, "state": "active"},
            {"x": 0.75, "y": 0.5, "r": 0.05, "state": "hit"}])
        ass = calibrate.scene_ass(s, calibrate.parse_tel(TEL), 1280, 720)
        self.assertIn("\\pos(320,360)", ass)
        self.assertIn("\\pos(960,360)", ass)
        self.assertIn("\\pos(640,360)", ass)      # the dot
        self.assertIn("&H%s&" % calibrate.GREEN, ass)

    def test_a_pressed_button_is_lit(self):
        dark = calibrate.scene_ass(scene(step="buttons", button="A"), None,
                                   1280, 720)
        lit = calibrate.scene_ass(scene(step="buttons", button="A",
                                        lights=calibrate.BIT["A"]),
                                  None, 1280, 720)
        self.assertEqual(lit.count("&H%s&" % calibrate.GREEN),
                         dark.count("&H%s&" % calibrate.GREEN) + 1)

    def test_the_panel_says_what_the_phone_said(self):
        lines = calibrate.telemetry_lines(calibrate.parse_tel(TEL), 25, True)
        self.assertIn("α 10.5°", lines[0])
        self.assertIn("screen 90°", lines[0])
        self.assertIn("touch 0.25, 0.50", lines[1])
        self.assertIn("00010010", lines[1])
        self.assertIn("25 Hz", lines[1])
        self.assertEqual(calibrate.telemetry_lines(None, 0, True),
                         ["Waiting for the phone…"])

    def test_the_summary_prints_its_numbers(self):
        ass = calibrate.scene_ass(scene(step="summary",
                                        summary={"yaw_across": 41.2,
                                                 "error": 1.25}),
                                  None, 1280, 720)
        self.assertIn("41°", ass)
        self.assertIn("1.2%", ass)


class Worker(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(100.0)
        self.mpv = FakeMpv()
        self.stops = []
        self.cal = calibrate.Calibration(self.mpv, stop=lambda: self.stops.append(1),
                                         clock=self.clock)
        # One phone holding it, unless a test says otherwise.
        self.cal._conns = 1
        quiet = mock.patch("playstick.calibrate.log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def test_only_the_newest_of_a_burst_is_drawn(self):
        for i in range(50):
            self.cal.on_message({"tel": dict(TEL, a=i)})
        self.cal.on_message({"scene": {"step": "sweep", "edge": "top"}})
        self.cal.step()
        (cmd,) = self.mpv.overlays()
        self.assertEqual(cmd[:3], ["osd-overlay", calibrate.OVERLAY_ID,
                                   "ass-events"])
        self.assertIn("α 49.0°", cmd[3])
        self.assertIn("the top edge", cmd[3])

    def test_it_draws_no_faster_than_the_interval(self):
        self.cal.on_message({"tel": TEL})
        self.cal.step()
        self.cal.on_message({"tel": TEL})
        self.cal.step()
        self.assertEqual(self.cal.draws, 1)
        self.clock.advance(calibrate.DRAW_INTERVAL)
        self.cal.step()
        self.assertEqual(self.cal.draws, 2)

    def test_nothing_is_drawn_without_the_black_screen(self):
        self.mpv.playing = "playing"
        self.cal.on_message({"tel": TEL})
        self.cal.step()
        self.assertEqual(self.mpv.overlays(), [])

    def test_a_quiet_phone_reads_as_waiting(self):
        self.cal.on_message({"tel": TEL})
        self.cal.step()
        self.clock.advance(calibrate.TEL_STALE + 0.1)
        self.cal.step()
        self.assertIn("Waiting for the phone", self.mpv.overlays()[-1][3])

    def test_a_session_nobody_holds_ends_after_the_grace(self):
        self.cal._conns = 0
        self.cal.step()
        self.clock.advance(calibrate.GRACE_SECONDS - 1)
        self.cal.step()
        self.assertEqual(self.stops, [])
        self.clock.advance(1)
        self.cal.step()
        self.assertEqual(self.stops, [1])

    def test_done_ends_it_at_once(self):
        self.cal.on_message({"done": True})
        self.assertEqual(self.stops, [1])

    def test_done_does_not_stop_a_film(self):
        self.mpv.playing = "playing"
        self.cal.on_message({"done": True})
        self.assertEqual(self.stops, [])


class Socket(unittest.TestCase):
    def test_messages_arrive_and_closing_starts_the_grace(self):
        server_end, phone = socket.socketpair()
        self.addCleanup(server_end.close)
        self.addCleanup(phone.close)
        conn = ws.Connection(server_end.makefile("rb"),
                             server_end.makefile("wb", buffering=0), server_end)
        clock = Clock(5.0)
        cal = calibrate.Calibration(FakeMpv(), clock=clock)
        phone.sendall(client_frame(ws.TEXT, json.dumps(
            {"scene": {"step": "tilt", "edge": "left"}}).encode()))
        phone.sendall(client_frame(ws.TEXT, b"not json"))
        phone.sendall(client_frame(ws.CLOSE, b"\x03\xe8"))
        cal.serve(conn)
        self.assertEqual(cal._scene["step"], "tilt")
        self.assertEqual(cal._conns, 0)
        self.assertEqual(cal._orphan_since, 5.0)


class Blank(unittest.TestCase):
    """The player and the stage, as far as the black screen changes them."""

    def test_the_black_screen_is_its_own_state_with_no_status(self):
        player = Player()
        proc = mock.Mock()
        proc.poll.return_value = None
        player._proc = proc
        player._item = {"id": "calibrate", "kind": "calibrate"}
        self.assertEqual(player.state(), "calibrating")
        self.assertEqual(player.status(), {})
        player._proc = None

    def test_the_stage_sends_calibrate_to_the_blank_player(self):
        player = FakePlayer()
        player.start_blank = mock.Mock()
        game = mock.Mock()
        game.running.return_value = False
        Stage(player, game).start({"id": "calibrate", "kind": "calibrate"})
        player.start_blank.assert_called_once()
        self.assertEqual(player.calls, [])


class Validate(unittest.TestCase):
    def test_a_good_profile_comes_through(self):
        out = profiles.validate(good_profile())
        self.assertEqual(out["name"], "Kid's iPhone")
        self.assertEqual(out["buttons"]["A"], 32)
        self.assertEqual(out["tilt"]["press"]["down"], 14.0)

    def test_what_it_refuses(self):
        cases = {
            "unknown key": good_profile(extra=1),
            "no name": good_profile(name="{}"),
            "range": good_profile(pointer=dict(good_profile()["pointer"],
                                               yaw_across=500)),
            "nan": good_profile(pointer=dict(good_profile()["pointer"],
                                             smoothing=math.nan)),
            "sign": good_profile(tilt=dict(good_profile()["tilt"],
                                           roll_sign=0)),
            "bool is not a sign": good_profile(tilt=dict(good_profile()["tilt"],
                                                         roll_sign=True)),
            "missing": good_profile(tilt={"press": {}}),
            "bad bit": good_profile(buttons={"A": 1, "B": 32, "START": 64,
                                             "SELECT": 128}),
            "twice": good_profile(buttons={"A": 16, "B": 16, "START": 64,
                                           "SELECT": 128}),
        }
        for label, body in cases.items():
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    profiles.validate(body)


class Store(unittest.TestCase):
    def test_saved_listed_and_replaced(self):
        db, _library, _thumbs = real_store("profiles")
        store = profiles.Profiles(db, clock=lambda: 1000.0)
        saved = store.save("phone-0001", profiles.validate(good_profile()))
        self.assertEqual(saved["id"], "phone-0001")
        self.assertEqual(saved["pointer"]["yaw_across"], 40.0)
        store.save("phone-0001", profiles.validate(good_profile(name="Renamed")))
        self.assertEqual(store.list(), [{"id": "phone-0001", "name": "Renamed",
                                         "updated_at": 1000.0}])
        self.assertIsNone(store.get("phone-9999"))

    def test_a_version_four_file_gains_the_table(self):
        import sqlite3
        from playstick.db import (Database, EPISODES, GAMES, SCHEMA,
                                  SCHEMA_VERSION, SERIES_ART)
        from support import TMP
        path = os.path.join(TMP, "old-v4.sqlite")
        if os.path.exists(path):
            os.remove(path)
        conn = sqlite3.connect(path)
        for step in (SCHEMA, EPISODES, SERIES_ART, GAMES):
            conn.executescript(step)
        conn.execute("PRAGMA user_version=4")
        conn.commit()
        conn.close()
        db = Database(path)
        with mock.patch("playstick.db.log"):
            self.assertEqual(db.migrate(), SCHEMA_VERSION)
        tables = {row[0] for row in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        db.close()
        self.assertIn("phone_profile", tables)


class FakeProfiles:
    """The store as a dict. The real one is tested above against a real file;
    behind the handler it would open a connection per request thread that
    nothing closes, and the ResourceWarnings would bury a real one."""

    def __init__(self):
        self.rows = {}

    def list(self):
        return [{"id": k, "name": v["name"], "updated_at": 1.0}
                for k, v in self.rows.items()]

    def get(self, ident):
        found = self.rows.get(ident)
        return dict(found, id=ident, updated_at=1.0) if found else None

    def save(self, ident, profile):
        self.rows[ident] = profile
        return self.get(ident)


class FakeCalibration:
    def serve(self, conn):
        conn.close()
        conn.drain()


class Routes(ApiTest):
    def setUp(self):
        super().setUp()
        self.server.RequestHandlerClass.profiles = FakeProfiles()
        quiet = mock.patch("playstick.http.log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.server.RequestHandlerClass.calibration = FakeCalibration()

    def test_a_profile_round_trips(self):
        saved = self.assertJson(self.fetch("/api/profile/phone-abcdef",
                                           "POST", good_profile()))
        self.assertEqual(saved["name"], "Kid's iPhone")
        got = self.assertJson(self.fetch("/api/profile/phone-abcdef"))
        self.assertEqual(got["buttons"], saved["buttons"])
        listed = self.assertJson(self.fetch("/api/profiles"))["profiles"]
        self.assertEqual([p["id"] for p in listed], ["phone-abcdef"])

    def test_a_bad_profile_is_a_sentence(self):
        body = self.assertJson(self.fetch("/api/profile/phone-abcdef", "POST",
                                          good_profile(extra=1)), 400)
        self.assertIn("extra", body["error"])

    def test_an_id_that_is_not_one(self):
        self.assertJson(self.fetch("/api/profile/x", "POST", good_profile()), 400)
        self.assertJson(self.fetch("/api/profile/phone-nobody"), 404)
        self.assertJson(self.fetch("/api/profile/..%2F..%2Fetc"), 404)

    def test_start_goes_through_the_projectionist(self):
        self.assertJson(self.fetch("/api/calibrate/start", "POST", {}))
        self.assertEqual(self.projectionist.calls, [("begin", "calibrate")])

    def test_start_is_refused_while_a_film_plays(self):
        self.projectionist.begin_error = Busy("A film is already playing.")
        body = self.assertJson(self.fetch("/api/calibrate/start", "POST", {}),
                               409)
        self.assertEqual(body["error"], "A film is already playing.")

    def test_start_again_while_calibrating_is_fine(self):
        self.player.playing = "calibrating"
        self.assertJson(self.fetch("/api/calibrate/start", "POST", {}))
        self.assertEqual(self.projectionist.calls, [])

    def test_start_again_while_warming_up_is_fine(self):
        self.projectionist.prepare = {"step": "warming"}
        self.player.item = {"id": "calibrate", "kind": "calibrate"}
        self.assertJson(self.fetch("/api/calibrate/start", "POST", {}))
        self.assertEqual(self.projectionist.calls, [])

    def test_a_daemon_without_it_says_so(self):
        self.server.RequestHandlerClass.calibration = None
        self.server.RequestHandlerClass.profiles = None
        self.assertJson(self.fetch("/api/calibrate/start", "POST", {}), 404)
        self.assertEqual(self.assertJson(self.fetch("/api/profiles")),
                         {"profiles": []})


if __name__ == "__main__":
    unittest.main()
