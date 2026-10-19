"""Bringing a library prepared by the old arrangement into the new database.

The property that matters is in `IdsAreCopiedNeverRecomputed`. An imported
film's id is the join to three things on the stick that exist nowhere else --
the poster on the children's grid, every correction anybody has made, and where
each film got to. An import that recomputed an id would orphan all three, and
the grid would look perfect afterwards.

The second thing worth a test is that the import does not delete what it read.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import (catalog, import_legacy, prepared,  # noqa: E402
                                  sync)
from playstick_admin.core.db import Database                          # noqa: E402
import playstick_prep as prep                                          # noqa: E402

PONYO = prep.file_id("Ponyo/Ponyo.mkv")
HOOK = prep.file_id("Hook (1991)/Hook.mkv")


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.nas = os.path.join(self._tmp.name, "nas")
        os.makedirs(self.nas)
        self.db = Database(os.path.join(self._tmp.name, "admin.db"))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self._write_old_library()

    def write(self, rel, body):
        path = os.path.join(self.nas, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(body, bytes) else "w"
        with open(path, mode) as handle:
            handle.write(body)
        return path

    def _write_old_library(self):
        index = {
            "schema": 1, "count": 2,
            "movies": [
                {"id": PONYO, "title": "Ponyo", "sort_title": "ponyo",
                 "year": 2008, "rating": 8.0, "genres": ["Animation"],
                 "duration": 6060.0, "width": 1280, "height": 720,
                 "vcodec": "h264", "acodec": "aac", "tmdb_id": 12429,
                 "source_rel": "Ponyo/Ponyo.mkv",
                 "rel": ".playstick/media/%s.mp4" % PONYO,
                 "poster": ".playstick/posters/%s.jpg" % PONYO,
                 "notes": ["runs short"],
                 "subtitles": [{"rel": ".playstick/subs/%s.eng.srt" % PONYO,
                                "lang": "eng"}],
                 "audio": [{"n": 1, "rel": ".playstick/audio/%s/1.eng.m4a" % PONYO,
                            "lang": "eng", "title": "", "channels": 2,
                            "default": True, "offset": 0.25}]},
                {"id": HOOK, "title": "Hook", "sort_title": "hook",
                 "year": 1991, "source_rel": "Hook (1991)/Hook.mkv",
                 "rel": "Hook (1991)/Hook.mkv"},
            ],
        }
        self.write("playstick-library.json", json.dumps(index))
        self.write("playstick-overrides.json", json.dumps({
            "schema": 1,
            "overrides": {HOOK: {"title": "Hook", "tmdb_id": 2493,
                                 "source_rel": "Hook (1991)/Hook.mkv"}}}))
        self.write(".playstick/prep-state.json", json.dumps({
            "Ponyo/Ponyo.mkv": {
                "fingerprint": [1024, 5], "verify_mode": "quick",
                "meta_version": prep.META_VERSION,
                "movie": {"title": "Ponyo", "duration": 6060.0,
                          "audio_streams_info": []}}}))
        self.write(".playstick/posters/%s.jpg" % PONYO, b"jpegbytes")
        self.write(".playstick/tmdb-cache.json", "{}")
        self.write(".playstick/media/%s.mp4" % PONYO, b"encode")

    def run_import(self):
        return import_legacy.run(self.db, self.nas, self.nas, self.nas)


class IdsAreCopiedNeverRecomputed(Fixture):
    def test_every_film_keeps_the_id_the_stick_knows_it_by(self):
        self.run_import()
        self.assertIsNotNone(prepared.get(self.db, PONYO))
        self.assertIsNotNone(prepared.get(self.db, HOOK))

    def test_a_mismatch_is_reported_and_not_acted_on(self):
        # The rule for a file already on the share is deliberately the old
        # rule, so this should be zero -- but if it ever is not, the index wins.
        result = self.run_import()
        self.assertEqual(result["mismatched_ids"], 0)


class WhatComesAcross(Fixture):
    def test_the_film_table(self):
        self.run_import()
        row = prepared.get(self.db, PONYO)
        self.assertEqual(row["title"], "Ponyo")
        self.assertEqual(row["media_rel"], ".playstick/media/%s.mp4" % PONYO)
        self.assertEqual(row["source_rel"], "Ponyo/Ponyo.mkv")
        self.assertEqual(json.loads(row["notes"]), ["runs short"])
        kids = prepared.children(self.db, PONYO)
        self.assertEqual(kids["genres"], ["Animation"])
        self.assertEqual(kids["subtitles"][0]["lang"], "eng")
        self.assertEqual(kids["audio"][0]["offset"], 0.25)

    def test_the_posters_become_bytes(self):
        result = self.run_import()
        self.assertEqual(result["posters"], 1)
        row = prepared.poster(self.db, PONYO)
        self.assertEqual(row["bytes"], b"jpegbytes")
        self.assertEqual(row["etag"], prepared.etag_of(b"jpegbytes"))

    def test_the_corrections(self):
        self.run_import()
        entry = prepared.overrides(self.db, HOOK)
        self.assertEqual(entry["title"], "Hook")
        # tmdb_id is this app's own idea and has to survive the move.
        self.assertEqual(entry["tmdb_id"], 2493)

    def test_the_probe_cache_so_the_first_job_does_not_re_probe(self):
        self.run_import()
        row = catalog.get_item(self.db, PONYO)
        self.assertIsNotNone(row["probe_json"])
        self.assertEqual(json.loads(row["probe_json"])["title"], "Ponyo")

    def test_the_films_are_already_on_the_stick(self):
        # An old library is by definition one that has been synced, so nothing
        # should offer to push it again.
        self.run_import()
        self.assertEqual(prepared.unpushed(self.db), [])

    def test_they_show_up_in_the_prepare_tab_as_done(self):
        self.run_import()
        states = {r["id"]: r["state"] for r in catalog.items(self.db)}
        self.assertEqual(states[PONYO], catalog.READY)


class TheImportIsARead(Fixture):
    def test_nothing_on_the_share_is_deleted(self):
        before = sorted(os.listdir(self.nas))
        self.run_import()
        self.assertEqual(sorted(os.listdir(self.nas)), before)
        self.assertTrue(os.path.isfile(
            os.path.join(self.nas, "playstick-library.json")))

    def test_running_it_twice_changes_nothing(self):
        first = self.run_import()
        second = self.run_import()
        self.assertEqual(first["films"], second["films"])
        self.assertEqual(len(prepared.all_prepared(self.db)), 2)
        self.assertEqual(len(catalog.sources(self.db)), 1)

    def test_no_index_is_not_an_error(self):
        os.unlink(os.path.join(self.nas, "playstick-library.json"))
        self.assertIsNone(import_legacy.describe(self.nas))
        self.assertEqual(import_legacy.run(self.db, self.nas)["films"], 0)


if __name__ == "__main__":
    unittest.main()
