"""The shared drawing over the film, and POST /api/draw.

Drawing is driven a step() at a time against the pointer tests' fake mpv, so
what reaches libass is read back rather than timed.
"""

import unittest
from unittest import mock

from support import ApiTest, Clock, api
from playstick import draw
from test_pointer import FakeMpv


class Canvas(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.mpv = FakeMpv()
        self.d = draw.Drawing(self.mpv, clock=self.clock)

    def line(self, phone, sid, pts, tool="brush", end=True, color="red", size="m"):
        return self.d.stroke(phone, sid, tool, color, size, pts, end)

    def test_a_stroke_is_drawn_where_it_was_made(self):
        self.line("a", 1, [(0.25, 0.5), (0.5, 0.5)])
        self.d.step()
        (cmd,) = self.mpv.overlays()
        self.assertEqual(cmd[:3], ["osd-overlay", draw.OVERLAY_ID, "ass-events"])
        self.assertEqual(cmd[4:7], [1280, 720, draw.Z])
        self.assertIn("\\an7\\pos(0,0)", cmd[3])
        self.assertIn("m 320 360 l ", cmd[3])
        self.assertIn("640 360", cmd[3])
        self.assertIn("\\3c&H%s&" % draw.COLORS["red"], cmd[3])

    def test_points_far_apart_are_filled_in(self):
        self.line("a", 1, [(0.0, 0.5), (1.0, 0.5)])
        (s,) = self.d._strokes
        gaps = [draw._dist(a, b) ** 0.5 for a, b in zip(s.pts, s.pts[1:])]
        self.assertLessEqual(max(gaps), draw.STEP + 1e-9)

    def test_two_phones_draw_on_one_canvas(self):
        self.line("a", 1, [(0.1, 0.1), (0.2, 0.2)])
        reply = self.line("b", 1, [(0.6, 0.6), (0.7, 0.7)], tool="pencil")
        self.assertEqual([s.tool for s in self.d._strokes], ["brush", "pencil"])
        self.assertEqual(reply["painters"], 2)

    def test_each_painter_has_a_cursor_of_its_own(self):
        self.d.move("a", 0.25, 0.5, "brush", "red", "m")
        self.d.move("b", 0.75, 0.5, "eraser", "red", "l")
        self.d.step()
        ass = self.mpv.overlays()[-1][3]
        self.assertEqual(ass.count("\n") + 1, 2)
        self.assertIn("\\pos(320,360)", ass)
        self.assertIn("\\pos(960,360)", ass)
        self.assertEqual(self.d._strokes, [])

    def test_a_stroke_arrives_in_pieces_and_stays_one_stroke(self):
        self.line("a", 1, [(0.1, 0.1)], end=False)
        self.line("a", 1, [(0.2, 0.1)], end=False)
        self.line("a", 1, [(0.3, 0.1)], end=True)
        self.assertEqual(len(self.d._strokes), 1)

    def test_the_eraser_cuts_a_line_in_two(self):
        self.line("a", 1, [(0.1, 0.5), (0.9, 0.5)])
        self.line("b", 1, [(0.5, 0.2), (0.5, 0.8)], tool="eraser")
        self.assertEqual(len(self.d._strokes), 2)
        left, right = self.d._strokes
        self.assertLess(max(x for x, _ in left.pts), 0.5)
        self.assertGreater(min(x for x, _ in right.pts), 0.5)

    def test_the_eraser_rubs_out_a_whole_dot(self):
        self.line("a", 1, [(0.5, 0.5)])
        self.line("b", 1, [(0.5, 0.5)], tool="eraser")
        self.assertEqual(self.d._strokes, [])

    def test_a_line_still_being_drawn_carries_on_after_an_eraser(self):
        self.line("a", 1, [(0.1, 0.5), (0.9, 0.5)], end=False)
        self.line("b", 1, [(0.5, 0.2), (0.5, 0.8)], tool="eraser")
        self.line("a", 1, [(0.9, 0.6)], end=True)
        self.assertEqual(len(self.d._strokes), 2)
        self.assertEqual(self.d._strokes[1].pts[-1], (0.9, 0.6))

    def test_clear_empties_it_and_hides_the_overlay(self):
        self.line("a", 1, [(0.1, 0.1)])
        self.d.step()
        self.d.clear("b")
        self.d.step()
        self.assertEqual(self.d._strokes, [])
        self.assertEqual(self.mpv.overlays()[-1][2], "none")

    def test_a_new_film_starts_a_new_drawing(self):
        self.line("a", 1, [(0.1, 0.1)])
        self.mpv.current_item = lambda: {"id": "film2"}
        self.d.sync("a")
        self.assertEqual(self.d._strokes, [])

    def test_with_no_film_nothing_is_kept_or_drawn(self):
        self.mpv.playing = "idle"
        reply = self.line("a", 1, [(0.1, 0.1)])
        self.assertFalse(reply["shown"])
        self.d.step()
        self.assertEqual(self.d._strokes, [])
        self.assertEqual(self.mpv.commands, [])

    def test_the_drawing_has_a_limit(self):
        with mock.patch.object(draw, "MAX_POINTS", 10):
            reply = self.line("a", 1, [(0.0, 0.5), (1.0, 0.5)])
            self.assertTrue(reply["full"])
            self.assertEqual(self.d._points, 10)
            self.line("a", 2, [(0.5, 0.9)])
            self.assertEqual(len(self.d._strokes), 1)

    def test_a_painter_who_goes_quiet_stops_counting(self):
        self.line("a", 1, [(0.1, 0.1)])
        self.clock.advance(draw.PAINTER_SECONDS + 0.1)
        self.assertEqual(self.d.sync("b")["painters"], 1)
        self.d.leave("b")
        self.assertEqual(self.d.sync("c")["painters"], 1)

    def test_the_cursor_stays_where_a_line_ended_until_it_fades(self):
        self.line("a", 1, [(0.5, 0.5)], end=False)
        self.line("a", 1, [], end=True)
        self.d.step()
        self.assertEqual(self.mpv.overlays()[-1][3].count("\n"), 1)
        self.clock.advance(draw.CURSOR_SECONDS + 0.1)
        self.d.step()
        self.assertEqual(self.mpv.overlays()[-1][3].count("\n"), 0)

    def test_a_cursor_alone_goes_by_itself_and_hides_the_overlay(self):
        self.d.move("a", 0.5, 0.5, "pencil", "blue", "s")
        self.d.step()
        self.clock.advance(draw.CURSOR_SECONDS + 0.1)
        self.d.step()
        self.assertEqual(self.mpv.overlays()[-1][2], "none")

    def test_leaving_takes_the_cursor_away(self):
        self.d.move("a", 0.5, 0.5, "pencil", "blue", "s")
        self.d.step()
        self.d.leave("a")
        self.d.step()
        self.assertEqual(self.mpv.overlays()[-1][2], "none")


class Route(ApiTest):
    def setUp(self):
        super().setUp()
        self.mpv = FakeMpv()
        self.d = draw.Drawing(self.mpv)
        self.server.RequestHandlerClass.drawing = self.d
        api._draw_window[:] = [0.0, 0]

    def tearDown(self):
        self.server.RequestHandlerClass.drawing = None
        super().tearDown()

    def post(self, body):
        return self.fetch("/api/draw", method="POST", body=body)

    def test_a_stroke_lands_clamped_and_comes_back(self):
        body = self.assertJson(self.post({
            "phone": "phone-0001", "op": "stroke", "id": 1, "tool": "pencil",
            "color": "blue", "size": "s", "points": [1.5, -1, 0.5, 0.5],
            "end": True}))
        self.assertEqual(body, {"shown": True, "painters": 1, "full": False})
        (s,) = self.d._strokes
        self.assertEqual(s.pts[0], (1.0, 0.0))
        self.assertEqual((s.tool, s.color, s.size), ("pencil", "blue", "s"))

    def test_a_move_places_the_cursor(self):
        self.assertJson(self.post({
            "phone": "phone-0001", "op": "move", "x": 0.25, "y": 2,
            "tool": "eraser", "color": "red", "size": "l"}))
        self.assertEqual(self.d._cursors["phone-0001"][:4],
                         (0.25, 1.0, "eraser", "l"))

    def test_bad_requests_are_refused(self):
        good = {"phone": "phone-0001", "op": "stroke", "id": 1,
                "tool": "brush", "color": "red", "size": "m",
                "points": [0.5, 0.5]}
        for change in ({"phone": "x"}, {"op": "explode"}, {"tool": "spray"},
                       {"color": "&H00&\\p0"}, {"size": "xl"}, {"id": "1"},
                       {"points": [0.5]}, {"points": [0.5, "a"]},
                       {"points": [0.5, float("nan")]}, {"points": [True, 0.5]},
                       {"points": [0.5, 0.5] * (draw.MAX_BATCH + 1)}):
            with self.subTest(change=change):
                self.assertEqual(self.post(dict(good, **change)).status, 400)
        move = {"phone": "phone-0001", "op": "move", "x": 0.5, "y": 0.5,
                "tool": "brush", "color": "red", "size": "m"}
        for change in ({"x": None}, {"y": "top"}, {"x": float("inf")},
                       {"tool": "spray"}):
            with self.subTest(change=change):
                self.assertEqual(self.post(dict(move, **change)).status, 400)

    def test_clear_sync_and_leave(self):
        self.post({"phone": "phone-0001", "op": "stroke", "id": 1,
                   "tool": "brush", "points": [0.5, 0.5]})
        self.assertJson(self.post({"phone": "phone-0002", "op": "clear"}))
        self.assertEqual(self.d._strokes, [])
        body = self.assertJson(self.post({"phone": "phone-0002", "op": "sync"}))
        self.assertEqual(body["painters"], 2)
        self.assertEqual(self.post({"phone": "phone-0002", "op": "leave"}).status,
                         200)

    def test_the_rate_is_capped_for_everybody(self):
        codes = [self.post({"phone": "phone-0001", "op": "sync"}).status
                 for _ in range(api.DRAW_MAX_RATE + 5)]
        self.assertIn(429, codes)
        self.assertEqual(codes.count(200), api.DRAW_MAX_RATE)


if __name__ == "__main__":
    unittest.main()
