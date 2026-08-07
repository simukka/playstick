"""/api/time: the clock every listening phone measures itself against.

The route exists to be the cheapest thing this server does, and that is a claim
about what it touches rather than a comment about it. A phone estimates its own
clock offset by timing the round trip and taking the midpoint, so every
millisecond spent inside the handler lands in that estimate as error -- and it
lands there invisibly, because nothing the phone can measure separates "the
network was slow" from "the daemon was busy". Half of the tests below are
therefore about what this handler does NOT do.

The rest are about the session id, which is the only defence against a clock
whose origin has changed. time.monotonic() counts from an arbitrary point and a
restart picks a new one; a phone holding an offset across that would be wrong
by an unbounded amount, forever, with no reading it could take that would say
so.
"""

import ipaddress
import re
import time
import unittest

from support import ApiTest, api, patched


HEX8 = re.compile(r"^[0-9a-f]{8}$")


class Time(ApiTest):
    def test_it_answers_with_a_clock_and_a_session(self):
        body = self.assertJson(self.fetch("/api/time"))
        self.assertEqual(set(body), {"now", "session"})
        self.assertIsInstance(body["now"], float)
        self.assertTrue(HEX8.match(body["session"]), body["session"])

    def test_the_clock_is_the_one_the_timecode_is_stamped_on(self):
        """The whole contract between the two routes. A timecode's `at` is
        meaningless except against this, so if these ever came off different
        clocks every listener would be wrong by the difference and nothing in
        either payload would show it."""
        before = time.monotonic()
        now = self.assertJson(self.fetch("/api/time"))["now"]
        after = time.monotonic()
        self.assertLessEqual(before, now)
        self.assertLessEqual(now, after)

    def test_the_clock_only_ever_goes_forward(self):
        readings = [self.assertJson(self.fetch("/api/time"))["now"]
                    for _ in range(5)]
        self.assertEqual(readings, sorted(readings), readings)

    def test_the_session_is_the_same_for_every_request(self):
        """It names a run of the daemon. One that changed per request would
        make every phone drop its offset on every sample, which is the same as
        having no offset at all."""
        seen = {self.assertJson(self.fetch("/api/time"))["session"]
                for _ in range(5)}
        self.assertEqual(len(seen), 1, seen)

    def test_the_session_is_what_the_module_generated(self):
        body = self.assertJson(self.fetch("/api/time"))
        self.assertEqual(body["session"], api.SESSION)

    # -- what it must not do

    def test_it_touches_neither_the_library_nor_mpv(self):
        """The reason it is a route of its own rather than a field on
        /api/status. That handler takes the library snapshot and asks mpv four
        questions over a socket, and a phone timing THAT would be measuring
        this daemon's queueing as though it were the wire."""
        self.fetch("/api/time")
        self.assertEqual(self.library.snapshots, 0)
        self.assertEqual(self.player.statuses, 0)

    def test_the_status_route_really_does_touch_them(self):
        """The control for the test above: it would pass just as happily
        against fakes nobody ever calls."""
        self.fetch("/api/status")
        self.assertGreater(self.library.snapshots + self.player.statuses, 0)

    def test_the_reply_is_small_enough_not_to_change_what_it_measures(self):
        resp = self.fetch("/api/time")
        self.assertLess(len(resp.body), 128, resp.body)

    def test_it_is_never_cached(self):
        """A cached clock reading is a wrong clock reading, and a phone would
        have no way to tell -- it would simply believe the offset it computed
        from a reply the browser produced without asking anybody."""
        self.assertEqual(self.fetch("/api/time").header("Cache-Control"),
                         "no-store")

    def test_it_is_still_behind_the_network_filter(self):
        with patched(ALLOW_NETWORKS=[ipaddress.ip_network("10.99.0.0/16")]):
            body = self.assertJson(self.fetch("/api/time"), 403)
        self.assertEqual(body, {"error": "not on the local network"})

    def test_a_query_string_is_ignored(self):
        """Every route here matches the parsed path, which is what makes the
        build stamp safe to hang off any URL. Asserted rather than assumed,
        because this route is new and that invariant is old."""
        for query in ["?v=c20e48476c19", "?v=", "?v=../../etc/passwd"]:
            with self.subTest(query=query):
                body = self.assertJson(self.fetch("/api/time" + query))
                self.assertEqual(set(body), {"now", "session"})

    def test_head_is_not_answered(self):
        """do_HEAD serves the audio route and nothing else, for the reason in
        its docstring: everything else here is generated per request, so
        answering HEAD means building the body to measure it and throwing it
        away."""
        resp = self.fetch("/api/time", method="HEAD")
        self.assertEqual(resp.status, 404)
        self.assertEqual(resp.body, b"")

    def test_post_is_not_answered(self):
        self.assertNotFound(self.fetch("/api/time", method="POST", body={}))

    def test_a_burst_is_answered_without_help(self):
        """Eight in a row is what a phone does on arrival and after a pocket
        wake, and six phones may do it at once. Chained rather than parallel by
        the page, so this is the shape of the load: many small requests, one
        connection, no state accumulating between them."""
        conn = self.connect()
        sessions = set()
        for _ in range(8):
            body = self.assertJson(self.fetch("/api/time", conn=conn))
            sessions.add(body["session"])
        self.assertEqual(len(sessions), 1)


if __name__ == "__main__":
    unittest.main()
