"""The admin app's own database: schema, catalogue, queue and prepared films.

None of this needs Qt or a display, and none of it needs a device -- these are
the tables the app keeps on the host, and the point of testing them apart from
the UI is that everything expensive in this app is a consequence of what they
say.

Two of these are worth more than the rest.

`IdsAreTheJoinToTheStick` checks that a film already on the share keeps the id
the old whole-library preparer gave it. Three things on the device hang off that
id -- the poster, every metadata correction, and every resume position -- and
re-issuing one is invisible from this side: the sync succeeds, the grid fills,
and the house quietly loses where it was in everything.

`WhatRemovingASourceMustNotDestroy` checks that unplugging a drive does not
delete the record of films from it that are already on the NAS and on the stick.
A cascade there would leave files on the share nothing references and rows on
the device nothing here can name in order to remove.
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import catalog, identity, prepared, queue, scan  # noqa: E402
from playstick_admin.core.db import Database                          # noqa: E402
from playstick_admin.core.schema import SCHEMA_VERSION                # noqa: E402
import playstick_prep as prep                                         # noqa: E402


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.nas = os.path.join(self.dir, "nas")
        os.makedirs(self.nas)
        self.db = Database(os.path.join(self.dir, "admin.db"))
        self.db.migrate()

    def tearDown(self):
        self.db.close()
        self._tmp.cleanup()

    def add_file(self, *parts, size=1024):
        path = os.path.join(self.dir, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * size)
        return path

    def add_item(self, source_id, path, title="A film"):
        ident, nas_rel = identity.film_id(self.nas, path)
        catalog.upsert_item(
            self.db, ident, source_id, os.path.basename(path), path,
            nas_rel=nas_rel, size=os.path.getsize(path), title=title)
        return ident


class Migrations(Fixture):
    def test_migrate_is_idempotent(self):
        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.db.migrate(), SCHEMA_VERSION)
        self.assertEqual(self.db.one("PRAGMA user_version")[0], SCHEMA_VERSION)

    def test_foreign_keys_are_on(self):
        # Every ON DELETE in the schema depends on this, and it is per
        # connection rather than stored in the file -- so a connection that
        # forgot it would silently do nothing on a cascade.
        self.assertEqual(self.db.one("PRAGMA foreign_keys")[0], 1)


class IdsAreTheJoinToTheStick(Fixture):
    def test_a_film_on_the_share_keeps_its_old_id(self):
        path = self.add_file("nas", "Hook (1991)", "Hook.mkv")
        ident, nas_rel = identity.film_id(self.nas, path)
        self.assertEqual(nas_rel, "Hook (1991)/Hook.mkv")
        # Exactly what the old single-library preparer would have called it.
        self.assertEqual(ident, prep.file_id("Hook (1991)/Hook.mkv"))

    def test_the_same_relative_path_under_two_sources_does_not_collide(self):
        on_nas = self.add_file("nas", "Hook", "Hook.mkv")
        elsewhere = self.add_file("incoming", "Hook", "Hook.mkv")
        first, rel = identity.film_id(self.nas, on_nas)
        second, none = identity.film_id(self.nas, elsewhere)
        self.assertEqual(rel, "Hook/Hook.mkv")
        self.assertIsNone(none)
        self.assertNotEqual(first, second)

    def test_guard_refuses_a_different_share(self):
        self.assertIsNone(identity.guard_nas_root(self.db, self.nas))
        self.assertIsNone(identity.guard_nas_root(self.db, self.nas))
        complaint = identity.guard_nas_root(self.db, os.path.join(self.dir, "other"))
        self.assertIn("named by a path relative to the first", complaint)


class Catalogue(Fixture):
    def test_add_source_is_idempotent_on_the_resolved_path(self):
        first = catalog.add_source(self.db, self.nas)
        second = catalog.add_source(self.db, self.nas + os.sep)
        self.assertEqual(first, second)
        self.assertEqual(len(catalog.sources(self.db)), 1)

    def test_a_rescan_does_not_walk_a_queued_film_back_to_available(self):
        source = catalog.add_source(self.db, self.nas)
        path = self.add_file("nas", "Hook.mkv")
        ident = self.add_item(source, path)
        queue.enqueue(self.db, ident)
        self.assertEqual(catalog.get_item(self.db, ident)["state"], "queued")
        self.add_item(source, path)          # the next scan finds it again
        self.assertEqual(catalog.get_item(self.db, ident)["state"], "queued")

    def test_a_file_that_vanishes_goes_missing_and_comes_back(self):
        source = catalog.add_source(self.db, self.nas)
        path = self.add_file("nas", "Hook.mkv")
        ident = self.add_item(source, path)
        import time
        catalog.mark_unseen_missing(self.db, source, time.time() + 1)
        self.assertEqual(catalog.get_item(self.db, ident)["state"], "missing")
        self.add_item(source, path)
        self.assertEqual(catalog.get_item(self.db, ident)["state"], "available")

    def test_the_probe_cache_refuses_a_stale_fingerprint(self):
        source = catalog.add_source(self.db, self.nas)
        ident = self.add_item(source, self.add_file("nas", "Hook.mkv"))
        fp = catalog.fingerprint(1024, 5, "quick", prep.META_VERSION)
        catalog.save_probe(self.db, ident, {"title": "Hook", "duration": 1.5}, fp)
        self.assertEqual(catalog.load_probe(self.db, ident, fp)["title"], "Hook")
        stale = catalog.fingerprint(2048, 5, "quick", prep.META_VERSION)
        self.assertIsNone(catalog.load_probe(self.db, ident, stale))


class TheShareIsASource(Fixture):
    """Settings' NAS root is offered in Prepare without being added by hand,
    and only the current one is."""

    def test_the_share_is_added_once(self):
        source, added = catalog.ensure_share_source(self.db, self.nas)
        self.assertTrue(added)
        again, added = catalog.ensure_share_source(self.db, self.nas)
        self.assertEqual((again, added), (source, False))
        self.assertEqual([s["label"] for s in catalog.sources(self.db)], ["NAS"])
        self.assertEqual(catalog.share_source_path(self.db),
                         os.path.realpath(self.nas))

    def test_a_new_share_replaces_the_old_one_and_nothing_else(self):
        other = tempfile.mkdtemp(prefix="playstick-share-")
        self.addCleanup(__import__("shutil").rmtree, other, True)
        mine = catalog.add_source(self.db, tempfile.gettempdir(), "Mine")
        catalog.ensure_share_source(self.db, self.nas)
        catalog.ensure_share_source(self.db, other)
        paths = sorted(s["path"] for s in catalog.sources(self.db))
        self.assertEqual(paths, sorted([os.path.realpath(other),
                                        os.path.realpath(tempfile.gettempdir())]))
        self.assertIsNotNone(catalog.get_source(self.db, mine))

    def test_no_share_is_no_source(self):
        catalog.ensure_share_source(self.db, self.nas)
        self.assertEqual(catalog.ensure_share_source(self.db, ""), (None, False))
        self.assertEqual(catalog.sources(self.db), [])


class Queue(Fixture):
    def setUp(self):
        super().setUp()
        self.source = catalog.add_source(self.db, self.nas)
        self.ids = [self.add_item(self.source, self.add_file("nas", "%d.mkv" % n))
                    for n in range(3)]

    def test_enqueue_is_idempotent_while_a_job_is_pending(self):
        self.assertIsNotNone(queue.enqueue(self.db, self.ids[0]))
        self.assertIsNone(queue.enqueue(self.db, self.ids[0]))
        self.assertEqual(len(queue.pending(self.db)), 1)

    def test_one_at_a_time(self):
        queue.enqueue_many(self.db, self.ids)
        first = queue.claim_next(self.db)
        self.assertEqual(first["item_id"], self.ids[0])
        # The preparer keeps its run state in module globals, so a second claim
        # while one is running would be two runs sharing one cancel flag.
        self.assertIsNone(queue.claim_next(self.db))
        queue.finish(self.db, first["id"], catalog.READY)
        self.assertEqual(queue.claim_next(self.db)["item_id"], self.ids[1])

    def test_a_job_running_when_the_process_died_goes_back_on_the_queue(self):
        queue.enqueue_many(self.db, self.ids)
        queue.claim_next(self.db)
        self.assertEqual(queue.reset_running(self.db), 1)
        again = queue.claim_next(self.db)
        self.assertEqual(again["item_id"], self.ids[0])
        self.assertEqual(again["attempts"], 2)

    def test_a_failure_keeps_its_message(self):
        queue.enqueue(self.db, self.ids[0])
        job = queue.claim_next(self.db)
        queue.finish(self.db, job["id"], catalog.FAILED,
                     error="ffmpeg: no space left on device")
        failed = queue.recent_failures(self.db)
        self.assertEqual(len(failed), 1)
        self.assertIn("no space", failed[0]["error"])
        self.assertEqual(catalog.get_item(self.db, self.ids[0])["state"], "failed")

    def _fail(self, ident, error="ffmpeg exploded"):
        queue.enqueue(self.db, ident)
        job = queue.claim_next(self.db)
        queue.finish(self.db, job["id"], catalog.FAILED, error=error)
        return job["id"]

    def test_a_failure_is_shown_until_something_deals_with_it(self):
        """The queue table lists unresolved failures under the work still to
        do: queued again, or prepared by a later attempt, and it is gone."""
        first = self._fail(self.ids[0], "first try")
        second = self._fail(self.ids[0], "second try")
        self._fail(self.ids[1])
        shown = {r["item_id"]: r["id"] for r in queue.unresolved_failures(self.db)}
        # One row per film, the latest.
        self.assertEqual(shown, {self.ids[0]: second, self.ids[1]: shown[self.ids[1]]})
        self.assertNotIn(first, shown.values())
        queue.enqueue(self.db, self.ids[1])            # retried
        job = queue.claim_next(self.db)
        queue.finish(self.db, job["id"], catalog.READY)  # ...and it worked
        self.assertEqual([r["item_id"] for r in queue.unresolved_failures(self.db)],
                         [self.ids[0]])
        queue.enqueue(self.db, self.ids[0])            # queued again
        self.assertEqual(queue.unresolved_failures(self.db), [])

    def test_a_failure_can_be_dismissed(self):
        job = self._fail(self.ids[0])
        self.assertTrue(queue.remove(self.db, job))
        self.assertEqual(queue.unresolved_failures(self.db), [])
        self.assertEqual(catalog.get_item(self.db, self.ids[0])["state"],
                         "available")

    def test_a_running_job_is_not_removed(self):
        queue.enqueue(self.db, self.ids[0])
        job = queue.claim_next(self.db)
        self.assertFalse(queue.remove(self.db, job["id"]))

    def test_move_and_remove(self):
        queue.enqueue_many(self.db, self.ids)
        jobs = [row["id"] for row in queue.pending(self.db)]
        self.assertTrue(queue.move(self.db, jobs[2], -2))
        self.assertEqual([r["id"] for r in queue.pending(self.db)][0], jobs[2])
        self.assertTrue(queue.remove(self.db, jobs[0]))
        self.assertEqual(len(queue.pending(self.db)), 2)
        self.assertEqual(catalog.get_item(self.db, self.ids[0])["state"],
                         "available")


class PreparedFilms(Fixture):
    def setUp(self):
        super().setUp()
        self.source = catalog.add_source(self.db, self.nas)
        self.ident = self.add_item(self.source, self.add_file("nas", "Hook.mkv"))
        self.movie = {
            "title": "Hook", "sort_title": "hook", "year": 1991,
            "rating": 6.8, "genres": ["Adventure", "Fantasy"],
            "duration": 8520.0, "width": 1280, "height": 720,
            "vcodec": "h264", "acodec": "aac", "tmdb_id": 2493,
            "verify_notes": ["runs short"],
            "subtitles": [{"rel": ".playstick/subs/x.eng.srt", "lang": "eng"}],
            "phone_audio": [{"n": 0, "rel": ".playstick/audio/x/0.eng.m4a",
                             "lang": "eng", "title": "", "channels": 6,
                             "default": True, "offset": 0.0}],
        }

    def test_record_round_trips(self):
        prepared.record(self.db, self.ident, self.movie,
                        ".playstick/media/x.mp4", "Hook.mkv",
                        bytes_on_nas=5, poster=(b"jpegbytes", "image/jpeg", "tmdb"))
        row = prepared.get(self.db, self.ident)
        self.assertEqual(row["title"], "Hook")
        # Text, because a rating is both 7.8 and "PG".
        self.assertEqual(row["rating"], "6.8")
        self.assertEqual(json.loads(row["notes"]), ["runs short"])
        kids = prepared.children(self.db, self.ident)
        self.assertEqual(kids["genres"], ["Adventure", "Fantasy"])
        self.assertEqual(kids["audio"][0]["is_default"], 1)
        self.assertEqual(prepared.poster(self.db, self.ident)["etag"],
                         prepared.etag_of(b"jpegbytes"))

    def test_the_etag_is_the_one_the_daemon_computes(self):
        # thumbs.etag_for() on the device is sha1(bytes)[:16]. If these ever
        # disagree, every poster uploads on every sync forever and nothing says
        # anything is wrong.
        import hashlib
        self.assertEqual(prepared.etag_of(b"abc"),
                         hashlib.sha1(b"abc").hexdigest()[:16])

    def test_a_re_prepare_clears_pushed_and_replaces_children(self):
        prepared.record(self.db, self.ident, self.movie, ".playstick/media/x.mp4",
                        "Hook.mkv")
        prepared.mark_pushed(self.db, self.ident)
        self.assertIsNotNone(prepared.get(self.db, self.ident)["pushed_at"])
        thinner = dict(self.movie, subtitles=[], genres=["Fantasy"])
        prepared.record(self.db, self.ident, thinner, ".playstick/media/x.mp4",
                        "Hook.mkv")
        self.assertIsNone(prepared.get(self.db, self.ident)["pushed_at"])
        kids = prepared.children(self.db, self.ident)
        self.assertEqual(kids["subtitles"], [])
        self.assertEqual(kids["genres"], ["Fantasy"])

    def test_artifact_rels_names_only_what_preparing_created(self):
        # The film was already on the share and plays from where it lies. Its
        # own file is not this app's to delete -- only what it made beside it.
        prepared.record(self.db, self.ident, self.movie, "Hook.mkv", "Hook.mkv")
        rels = prepared.artifact_rels(self.db, self.ident)
        self.assertNotIn("Hook.mkv", rels)
        self.assertIn(".playstick/subs/x.eng.srt", rels)
        self.assertIn(".playstick/audio/x/0.eng.m4a", rels)

    def test_unpushed_is_not_an_error_state(self):
        prepared.record(self.db, self.ident, self.movie, ".playstick/media/x.mp4",
                        "Hook.mkv")
        self.assertEqual(len(prepared.unpushed(self.db)), 1)
        prepared.mark_pushed(self.db, self.ident)
        self.assertEqual(prepared.unpushed(self.db), [])


class Overrides(Fixture):
    def setUp(self):
        super().setUp()
        source = catalog.add_source(self.db, self.nas)
        self.ident = self.add_item(source, self.add_file("nas", "Hook.mkv"))

    def test_a_reset_drops_back_rather_than_writing_the_old_value(self):
        prepared.set_override(self.db, self.ident, title="Hook", year=1991)
        prepared.set_override(self.db, self.ident, year=None)
        entry = prepared.overrides(self.db, self.ident)
        self.assertEqual(entry, {"title": "Hook"})
        self.assertEqual(
            self.db.one("SELECT COUNT(*) AS n FROM override WHERE "
                        "item_id = ? AND field = 'year'", (self.ident,))["n"], 0)

    def test_a_list_field_fits_beside_the_scalars(self):
        prepared.set_override(self.db, self.ident, genres=["Adventure"])
        self.assertEqual(prepared.overrides(self.db, self.ident)["genres"],
                         ["Adventure"])

    def test_the_shape_is_what_the_preparer_reads(self):
        prepared.set_override(self.db, self.ident, title="Hook", tmdb_id=2493)
        movie = {"id": self.ident, "title": "Red Hook Summer", "year": 2012}
        prep.apply_overrides(movie, prepared.overrides(self.db))
        self.assertEqual(movie["title"], "Hook")


class WhatRemovingASourceMustNotDestroy(Fixture):
    def test_unplugging_a_drive_keeps_films_that_are_already_on_the_stick(self):
        source = catalog.add_source(self.db, os.path.join(self.dir, "usb"))
        path = self.add_file("usb", "Hook.mkv")
        ident = self.add_item(source, path)
        prepared.record(self.db, ident, {"title": "Hook"},
                        ".playstick/media/x.mp4", ".playstick/media/x.mp4",
                        poster=(b"bytes", "image/jpeg", "tmdb"))
        prepared.set_override(self.db, ident, title="Hook")

        catalog.remove_source(self.db, source)

        # The item survives, orphaned rather than deleted -- otherwise the
        # files on the share have no name and the row on the stick has nothing
        # here that can ask for it to be removed.
        item = catalog.get_item(self.db, ident)
        self.assertIsNotNone(item)
        self.assertIsNone(item["source_id"])
        self.assertIsNotNone(prepared.get(self.db, ident))
        self.assertIsNotNone(prepared.poster(self.db, ident))
        self.assertEqual(prepared.overrides(self.db, ident)["title"], "Hook")

    def test_forgetting_a_film_does_take_its_poster(self):
        source = catalog.add_source(self.db, self.nas)
        ident = self.add_item(source, self.add_file("nas", "Hook.mkv"))
        prepared.record(self.db, ident, {"title": "Hook"}, "a", "a",
                        poster=(b"bytes", "image/jpeg", "tmdb"))
        prepared.forget(self.db, ident)
        self.assertIsNone(prepared.get(self.db, ident))
        self.assertIsNone(prepared.poster(self.db, ident))


class Scanning(Fixture):
    """The walk, and what it is allowed to disturb.

    prep.discover decides what a film is; this only decides where to look. The
    cases here are about the second half -- that a scan is safe to run at any
    moment, including while somebody is halfway through selecting things.
    """

    def setUp(self):
        super().setUp()
        # discover() rejects anything under --min-size-mb, which defaults to
        # 100. These are the flags the tab passes for a test library.
        self.extra = "--min-size-mb 0 --min-duration 0"
        self.incoming = os.path.join(self.dir, "incoming")
        os.makedirs(self.incoming)

    def video(self, root, *parts):
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"\0" * 2048)
        return path

    def scan(self, source_id):
        source = catalog.get_source(self.db, source_id)
        return scan.scan_source(self.db, source, self.nas, self.extra)

    def test_a_scan_records_what_it_finds_without_probing_it(self):
        self.video(self.incoming, "Hook (1991)", "Hook.mkv")
        source = catalog.add_source(self.db, self.incoming)
        result = self.scan(source)
        self.assertEqual(result["found"], 1)
        row = catalog.items(self.db)[0]
        self.assertEqual(row["title"], "Hook")
        self.assertEqual(row["year"], 1991)
        self.assertEqual(row["state"], "available")
        # No ffprobe ran: a walk of three mounts must not cost minutes.
        self.assertIsNone(row["probe_json"])

    def test_films_on_the_share_are_marked_as_such(self):
        self.video(self.nas, "Hook.mkv")
        source = catalog.add_source(self.db, self.nas)
        self.scan(source)
        row = catalog.items(self.db)[0]
        self.assertEqual(row["on_nas"], 1)
        self.assertEqual(row["nas_rel"], "Hook.mkv")

    def test_prepared_output_is_not_offered_back_as_a_source_film(self):
        # A share that has been prepared into holds encodes under .playstick/,
        # and a scan of the share that offered them would invite somebody to
        # prepare the encodes it had just made.
        self.video(self.nas, ".playstick", "media", "abc.mp4")
        self.video(self.nas, "Hook.mkv")
        source = catalog.add_source(self.db, self.nas)
        self.assertEqual(self.scan(source)["found"], 1)
        self.assertEqual([r["rel"] for r in catalog.items(self.db)], ["Hook.mkv"])

    def test_a_box_set_is_offered_episode_by_episode(self):
        # The stick folds a series into one poster, so an episode is an
        # ordinary row the Prepare tab offers, named for which one it is.
        self.video(self.incoming, "Detectorists", "S01E02.mkv")
        self.video(self.incoming, "Detectorists", "Series 2", "03.mkv")
        self.video(self.incoming, "Hook.mkv")
        source = catalog.add_source(self.db, self.incoming)
        result = self.scan(source)
        self.assertEqual((result["found"], result["episodic"]), (1, 2))
        rows = catalog.items(self.db, states=[catalog.AVAILABLE])
        self.assertEqual(sorted(r["title"] for r in rows),
                         ["Detectorists S01E02", "Detectorists S02E03", "Hook"])

    def test_an_episode_an_older_scan_excluded_is_offered_now(self):
        path = self.video(self.incoming, "Detectorists", "S01E02.mkv")
        source = catalog.add_source(self.db, self.incoming)
        ident, _ = identity.film_id(self.nas, path)
        catalog.upsert_item(self.db, ident, source,
                            "Detectorists/S01E02.mkv", path,
                            title="S01E02", state=catalog.EXCLUDED,
                            excluded_reason="episodic television")
        self.scan(source)
        row = catalog.get_item(self.db, ident)
        self.assertEqual(row["state"], catalog.AVAILABLE)
        self.assertIsNone(row["excluded_reason"])
        self.assertEqual(row["title"], "Detectorists S01E02")

    def test_a_rescan_leaves_a_selection_alone(self):
        path = self.video(self.incoming, "Hook.mkv")
        source = catalog.add_source(self.db, self.incoming)
        self.scan(source)
        ident = catalog.items(self.db)[0]["id"]
        queue.enqueue(self.db, ident)
        self.video(self.incoming, "Ponyo.mkv")
        self.scan(source)
        self.assertEqual(catalog.get_item(self.db, ident)["state"], "queued")
        self.assertEqual(len(catalog.items(self.db)), 2)

    def test_an_unplugged_drive_does_not_declare_its_films_gone(self):
        self.video(self.incoming, "Hook.mkv")
        source = catalog.add_source(self.db, self.incoming)
        self.scan(source)
        os.rename(self.incoming, self.incoming + "-unplugged")
        result = self.scan(source)
        self.assertTrue(result["unreachable"])
        # Still available, not missing: an external drive that is unplugged is
        # an ordinary Tuesday, not a library that has been deleted.
        self.assertEqual(catalog.items(self.db)[0]["state"], "available")

    def test_a_file_deleted_from_a_reachable_source_does_go_missing(self):
        path = self.video(self.incoming, "Hook.mkv")
        self.video(self.incoming, "Ponyo.mkv")
        source = catalog.add_source(self.db, self.incoming)
        self.scan(source)
        os.unlink(path)
        self.scan(source)
        states = {r["rel"]: r["state"] for r in catalog.items(self.db)}
        self.assertEqual(states["Hook.mkv"], "missing")
        self.assertEqual(states["Ponyo.mkv"], "available")

    def test_the_same_film_through_two_sources_is_one_row(self):
        # Somebody adds the share as well as the drive it was copied from.
        self.video(self.nas, "Hook.mkv")
        first = catalog.add_source(self.db, self.nas)
        second = catalog.add_source(self.db, os.path.dirname(self.nas))
        self.scan(first)
        self.scan(second)
        rows = [r for r in catalog.items(self.db) if r["title"] == "Hook"]
        self.assertEqual(len(rows), 1)
        # The source that found it first keeps it, rather than the row flapping
        # between two parents on every scan.
        self.assertEqual(rows[0]["source_id"], first)

    def test_a_scan_against_a_different_share_refuses_rather_than_rekeys(self):
        # The crash this replaces: films recorded while nas_root was the share
        # are named by their path relative to it. Point nas_root somewhere else
        # and the same file at the same (source, rel) gets a new id, and the
        # insert died on UNIQUE (source_id, rel) half way through a source.
        self.video(self.nas, "Hook (1991)", "Hook.mkv")
        source = catalog.add_source(self.db, self.nas)
        self.scan(source)
        before = catalog.items(self.db)[0]["id"]

        elsewhere = os.path.join(self.dir, "smoke-nas")
        os.makedirs(elsewhere)
        with self.assertRaises(scan.WrongShare) as caught:
            scan.scan_source(self.db, catalog.get_source(self.db, source),
                             elsewhere, self.extra)
        self.assertIn(os.path.realpath(self.nas), str(caught.exception))
        rows = catalog.items(self.db)
        self.assertEqual([r["id"] for r in rows], [before])
        self.assertEqual(rows[0]["state"], "available")

    def test_a_database_from_before_the_guard_is_still_guarded(self):
        # A database built before anything recorded which share it was built
        # against has no `nas_root_seen`. The first root it is shown must not
        # simply be believed when the rows already say otherwise.
        self.video(self.nas, "Hook (1991)", "Hook.mkv")
        source = catalog.add_source(self.db, self.nas)
        self.scan(source)
        with self.db.write() as conn:
            conn.execute("DELETE FROM meta WHERE key = 'nas_root_seen'")

        elsewhere = os.path.join(self.dir, "smoke-nas")
        os.makedirs(elsewhere)
        with self.assertRaises(scan.WrongShare):
            scan.scan_source(self.db, catalog.get_source(self.db, source),
                             elsewhere, self.extra)
        # And the share it was really built against is the one now on record.
        self.assertEqual(self.db.get_meta("nas_root_seen"),
                         os.path.realpath(self.nas))


if __name__ == "__main__":
    unittest.main()
