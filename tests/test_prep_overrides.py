"""Tests for the embedding hooks added to the preparer for the admin app:
overrides, the reporter callback, forced TMDb matches and cancellation.

No network, no ffmpeg. The module is loaded exactly as the other prep tests
load it, by path, because a hyphen once kept it out of `import` and the name
stuck.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "src", "prep", "playstick_prep.py")

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("playstick_prep", SCRIPT)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


class Overrides(unittest.TestCase):
    def test_load_missing_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(prep.load_overrides(d), {})

    def test_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            payload = {"schema": 1, "overrides": {"abc": {"title": "Hook"}}}
            with open(os.path.join(d, prep.OVERRIDES_NAME), "w") as f:
                json.dump(payload, f)
            self.assertEqual(prep.load_overrides(d), {"abc": {"title": "Hook"}})

    def test_load_garbage_is_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, prep.OVERRIDES_NAME), "w") as f:
                f.write("{not json")
            self.assertEqual(prep.load_overrides(d), {})

    def test_scalars_win_and_entry_returned(self):
        movie = {"id": "abc", "title": "Red Hook Summer", "year": 2012}
        overrides = {"abc": {"tmdb_id": 2493, "title": "Hook", "year": 1991,
                             "genres": ["Adventure"], "hidden": True}}
        entry = prep.apply_overrides(movie, overrides)
        self.assertEqual(movie["title"], "Hook")
        self.assertEqual(movie["year"], 1991)
        self.assertEqual(movie["genres"], ["Adventure"])
        self.assertTrue(movie["hidden"])
        self.assertEqual(entry["tmdb_id"], 2493)

    def test_no_entry_returns_none(self):
        movie = {"id": "zzz", "title": "Untouched"}
        self.assertIsNone(prep.apply_overrides(movie, {"abc": {"title": "x"}}))
        self.assertEqual(movie["title"], "Untouched")


class ForcedMatch(unittest.TestCase):
    def test_fetch_by_id_bypasses_the_gate(self):
        client = prep.Tmdb("KEY", None)
        # A film the automatic lookup would never have chosen -- here it is taken
        # on trust because a human picked the id.
        client._get = lambda path, params: {
            "id": 2493, "title": "Hook", "release_date": "1991-12-11",
            "vote_average": 6.8, "vote_count": 4000,
            "genres": [{"name": "Adventure"}, {"name": "Fantasy"}],
            "runtime": 142, "overview": "Peter Pan grew up.",
            "poster_path": "/hook.jpg",
        }
        meta, why = client.fetch_by_id(2493)
        self.assertIsNone(why)
        self.assertEqual(meta["tmdb_id"], 2493)
        self.assertEqual(meta["tmdb_score"], 100)
        self.assertEqual(meta["title"], "Hook")
        self.assertEqual(meta["year"], 1991)
        self.assertIn("Adventure", meta["genres"])
        self.assertEqual(meta["expected_runtime"], 142 * 60)

    def test_fetch_by_id_reports_a_miss(self):
        client = prep.Tmdb("KEY", None)
        client._get = lambda path, params: {}
        meta, why = client.fetch_by_id(999999)
        self.assertEqual(meta, {})
        self.assertIn("999999", why)


class ReporterAndCancel(unittest.TestCase):
    def tearDown(self):
        prep.set_reporter(None)
        prep.reset_cancel()

    def test_reporter_receives_events(self):
        seen = []
        prep.set_reporter(seen.append)
        prep.report_event("film_start", number=1, total=3, title="Hook")
        self.assertEqual(seen[-1]["event"], "film_start")
        self.assertEqual(seen[-1]["title"], "Hook")

    def test_reporter_gets_log_lines(self):
        seen = []
        prep.set_reporter(seen.append)
        prep.log("hello %s", "world")
        self.assertTrue(any(e.get("event") == "log" and "hello world" in e["text"]
                            for e in seen))

    def test_reporter_exception_never_escapes(self):
        def boom(_ev):
            raise RuntimeError("nope")
        prep.set_reporter(boom)
        prep.report_event("x")          # must not raise

    def test_a_pause_holds_ffmpeg_and_does_not_count_against_its_timeout(self):
        """Held for longer than the step's timeout, and it still finishes --
        and a cancel while held still ends it rather than waiting forever."""
        import threading
        import time
        self.addCleanup(prep.reset_cancel)

        def hold(seconds):
            prep.request_pause()
            time.sleep(seconds)
            prep.request_resume()

        holder = threading.Thread(target=hold, args=(1.5,))
        started = time.monotonic()
        holder.start()
        res = prep.run(["sleep", "0.5"], timeout=1.0)
        holder.join()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 1.5)

        prep.request_pause()
        threading.Timer(0.5, prep.request_cancel).start()
        started = time.monotonic()
        res = prep.run(["sleep", "30"], timeout=60)
        self.assertEqual(res.returncode, 130)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(prep.paused())

    def test_cancel_toggles(self):
        self.assertFalse(prep.cancelled())
        prep.request_cancel()
        self.assertTrue(prep.cancelled())
        prep.reset_cancel()
        self.assertFalse(prep.cancelled())


if __name__ == "__main__":
    unittest.main()
