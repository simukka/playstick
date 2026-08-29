"""Preparing one film, end to end, without ffmpeg and without a stick.

The single most breakable property of this design is that the paths recorded
here and sent to the stick are relative to the SHARE, whatever directory the
film was actually found in. Get that wrong and everything still looks fine from
this side: the scan works, the encode works, the push is accepted, and the film
simply never plays -- the daemon drops the row with one line in a journal nobody
is reading. `PathsAreRelativeToTheShare` is that property, from both a source on
the share and a source nowhere near it.

The rest is about the order of the steps, which is the error handling: nothing
is recorded until everything has landed, so a failure anywhere leaves
reclaimable debris rather than a tile a child presses that does nothing.

Two seams are faked and no more: ffprobe, so that a fixture can be four
kilobytes of zeroes instead of a real encode, and prep.prepare_one, replaced by
a stub that writes the files the real one would have written. Everything else --
collect_one and its cache, place_media, the moves, the database, the payload and
the push -- runs for real.
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import (catalog, identity, nas_store, pipeline,  # noqa: E402
                                  prepared, queue, scan, sync)
from playstick_admin.core.db import Database                          # noqa: E402
from playstick_admin.core.device_client import DeviceError            # noqa: E402
import playstick_prep as prep                                          # noqa: E402


# What ffprobe would say about a 720p H.264 film with one stereo track. Small
# enough that transcode_plan() finds nothing to complain about, so the tests
# that care about "no encode needed" get that path without arranging it.
PROBE = {
    "format": {"duration": "5460.0", "bit_rate": "3000000", "tags": {}},
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264",
         "profile": "High", "pix_fmt": "yuv420p", "width": 1280, "height": 720,
         "avg_frame_rate": "24/1", "duration": "5460.0", "tags": {},
         "disposition": {}},
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2,
         "start_time": "0.0", "tags": {"language": "eng"}, "disposition": {}},
    ],
}


class FakeSettings:
    def __init__(self, nas_root, staging, device_url="http://stick"):
        self.nas_root = nas_root
        self.staging_dir = staging
        self.device_url = device_url
        self.tmdb_key = ""
        self.tmdb_language = "en-US"
        self.ffmpeg = ""
        self.ffprobe = ""
        self.prep_extra_args = "--min-size-mb 0 --min-duration 0 --verify none"


class FakeStick:
    """Enough of DeviceClient to see what was sent, and to be switched off."""

    def __init__(self, up=True):
        self.up = up
        self.films = {}
        self.thumbs = {}
        self.deleted = []
        self.reconciles = []

    def _check(self):
        if not self.up:
            raise DeviceError("POST /api/admin/films: connection refused")

    def sync_films(self, films, reconcile=True):
        self._check()
        self.reconciles.append(reconcile)
        for film in films:
            self.films[film["id"]] = film
        return {"written": len(films), "removed": 0}

    def admin_library(self):
        self._check()
        return {"films": [dict(f, thumb_etag=self.thumbs.get(f["id"], ""))
                          for f in self.films.values()]}

    def put_thumb(self, ident, data, mime="image/jpeg"):
        self._check()
        self.thumbs[ident] = prepared.etag_of(data)
        return {"id": ident}

    def delete_films(self, ids):
        self._check()
        for ident in ids:
            self.films.pop(ident, None)
            self.deleted.append(ident)
        return {"removed": len(ids)}


class Fixture(unittest.TestCase):
    def setUp(self):
        self._verbosity = prep._verbosity
        prep._verbosity = -1
        self.addCleanup(setattr, prep, "_verbosity", self._verbosity)

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.nas = os.path.join(self.dir, "nas")
        self.staging = os.path.join(self.dir, "staging")
        self.incoming = os.path.join(self.dir, "incoming")
        for path in (self.nas, self.staging, self.incoming):
            os.makedirs(path)

        self.db = Database(os.path.join(self.dir, "admin.db"))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self.settings = FakeSettings(self.nas, self.staging)
        self.stick = FakeStick()
        self.encoded = []

        # ffprobe, faked at the one call that shells out. collect_one still
        # runs for real above it: the title and year derivation, the verify
        # step and the state cache are all things this file is testing.
        patch = mock.patch.object(prep, "ffprobe_json",
                                  lambda path, timeout=120: (PROBE, None))
        patch.start()
        self.addCleanup(patch.stop)

    # -- fixtures ----------------------------------------------------------

    def video(self, root, *parts, body=b"\0" * 4096):
        path = os.path.join(root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def offer(self, root, *parts):
        """A film on disk and the catalogue row for it, as a scan would."""
        path = self.video(root, *parts)
        source = catalog.add_source(self.db, root)
        scan.scan_source(self.db, catalog.get_source(self.db, source), self.nas,
                         self.settings.prep_extra_args)
        ident, _rel = identity.film_id(self.nas, path)
        return catalog.get_joined(self.db, ident)

    def preparer(self, encode=True, subs=True, audio=True, poster=True):
        """What prepare_one would have written into the staging tree."""
        def stub(movie, args, paths, tmdb):
            self.encoded.append(movie["id"])
            out = paths["output"]
            ident = movie["id"]

            def write(rel, body):
                path = os.path.join(out, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(body)

            if encode:
                rel = ".playstick/media/%s.mp4" % ident
                write(rel, b"encoded" * 100)
                movie["media_rel"] = rel
                movie["prepared"] = True
            if subs:
                rel = ".playstick/subs/%s.eng.srt" % ident
                write(rel, b"1\n00:00:01,000 --> 00:00:02,000\nhello\n")
                movie["subtitles"] = [{"rel": rel, "lang": "eng"}]
            if audio:
                rel = ".playstick/audio/%s/0.eng.m4a" % ident
                write(rel, b"aac" * 50)
                movie["phone_audio"] = [{"n": 0, "rel": rel, "lang": "eng",
                                         "title": "", "channels": 2,
                                         "default": True, "offset": 0.0}]
            if poster:
                write(".playstick/posters/%s.jpg" % ident, b"jpegbytes")
                movie["poster_source"] = "frame"
            return movie
        return mock.patch.object(prep, "prepare_one", stub)

    def prepare(self, item, **kw):
        with self.preparer(**kw):
            return pipeline.prepare(self.db, item, self.settings, self.stick)


class PathsAreRelativeToTheShare(Fixture):
    def test_a_film_from_a_source_nowhere_near_the_share(self):
        item = self.offer(self.incoming, "Hook (1991)", "Hook.mkv")
        self.prepare(item)

        sent = self.stick.films[item["id"]]
        for key in ("source_rel", "media_rel"):
            self.assertFalse(os.path.isabs(sent[key]), sent[key])
            self.assertNotIn("..", sent[key].split("/"))
            # And it names something that is actually there.
            self.assertTrue(
                os.path.isfile(os.path.join(self.nas, sent[key])), sent[key])
        for track in sent["audio"] + sent["subtitles"]:
            self.assertTrue(
                os.path.isfile(os.path.join(self.nas, track["rel"])),
                track["rel"])

    def test_a_film_already_on_the_share_keeps_its_own_path(self):
        item = self.offer(self.nas, "Hook (1991)", "Hook.mkv")
        # No encode needed: the point is that the original is not copied.
        self.prepare(item, encode=False)
        sent = self.stick.films[item["id"]]
        self.assertEqual(sent["source_rel"], "Hook (1991)/Hook.mkv")
        self.assertEqual(sent["media_rel"], "Hook (1991)/Hook.mkv")
        self.assertFalse(os.path.isdir(
            os.path.join(self.nas, ".playstick", "media")))

    def test_an_off_share_film_that_needs_no_encode_is_still_placed(self):
        # This is the gap the restructure opened. Left alone, the film would be
        # indexed by a path relative to somebody's drive, accepted by the stick,
        # and then silently never play.
        item = self.offer(self.incoming, "Hook.mkv")
        with mock.patch.object(prep, "run", self._remuxer()):
            self.prepare(item, encode=False)
        sent = self.stick.films[item["id"]]
        self.assertEqual(sent["media_rel"],
                         ".playstick/media/%s.mp4" % item["id"])
        self.assertTrue(os.path.isfile(
            os.path.join(self.nas, sent["media_rel"])))

    def _remuxer(self):
        def stub(argv, timeout=None, progress=None):
            with open(argv[-1], "wb") as handle:
                handle.write(b"remuxed")
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return stub


class OnlyTheShareGetsFilms(Fixture):
    def test_nothing_but_films_is_written_to_the_share(self):
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)

        top = sorted(os.listdir(self.nas))
        self.assertEqual(top, [".playstick"])
        under = sorted(os.listdir(os.path.join(self.nas, ".playstick")))
        # No index, no overrides file, no prep-state, no tmdb cache -- and no
        # posters, which the stick keeps in its own database and has never
        # opened off the mount.
        self.assertEqual(under, ["audio", "media", "subs"])

    def test_the_poster_goes_to_the_stick_and_into_the_database(self):
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        row = prepared.poster(self.db, item["id"])
        self.assertEqual(row["bytes"], b"jpegbytes")
        self.assertEqual(self.stick.thumbs[item["id"]],
                         prepared.etag_of(b"jpegbytes"))

    def test_the_probe_is_remembered_here_not_there(self):
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        row = catalog.get_item(self.db, item["id"])
        self.assertIsNotNone(row["probe_json"])
        self.assertIn("duration", json.loads(row["probe_json"]))


class NothingIsRecordedUntilItHasLanded(Fixture):
    def test_a_failed_move_records_no_film(self):
        item = self.offer(self.incoming, "Hook.mkv")
        with mock.patch.object(nas_store, "_move_one",
                               side_effect=nas_store.NasError("mount gone")):
            with self.assertRaises(pipeline.PrepareError):
                self.prepare(item)
        # No row, so nothing on the stick can point at a file that is not there.
        self.assertIsNone(prepared.get(self.db, item["id"]))
        self.assertEqual(self.stick.films, {})

    def test_what_a_failed_move_leaves_is_findable(self):
        item = self.offer(self.incoming, "Hook.mkv")
        real = nas_store._move_one

        def fail_on_the_film(src, dest):
            if dest.endswith(".mp4"):
                raise nas_store.NasError("mount gone")
            return real(src, dest)

        with mock.patch.object(nas_store, "_move_one", fail_on_the_film):
            with self.assertRaises(pipeline.PrepareError):
                self.prepare(item)
        self.assertTrue(nas_store.orphans(self.db, self.nas))

    def test_a_film_that_will_not_fit_is_refused_before_anything_moves(self):
        item = self.offer(self.incoming, "Hook.mkv")
        with mock.patch.object(nas_store, "check_space",
                               return_value="not enough room"):
            with self.assertRaises(pipeline.PrepareError):
                self.prepare(item)
        self.assertFalse(os.path.isdir(os.path.join(self.nas, ".playstick")))

    def test_a_missing_file_is_refused_rather_than_probed(self):
        item = self.offer(self.incoming, "Hook.mkv")
        os.unlink(item["abs_path"])
        with self.assertRaises(pipeline.PrepareError):
            self.prepare(item)


class AnAsleepStickIsNotAFailedPreparation(Fixture):
    def test_the_film_is_prepared_and_marked_unpushed(self):
        # Treating this as a failure would mean re-encoding a film because a
        # box in another room was switched off.
        self.stick.up = False
        item = self.offer(self.incoming, "Hook.mkv")
        result = self.prepare(item)
        self.assertFalse(result["pushed"])
        self.assertIsNotNone(result["push_error"])
        row = prepared.get(self.db, item["id"])
        self.assertIsNotNone(row)
        self.assertIsNone(row["pushed_at"])
        self.assertEqual([r["item_id"] for r in prepared.unpushed(self.db)],
                         [item["id"]])

    def test_and_the_recovery_costs_no_ffmpeg(self):
        self.stick.up = False
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        self.encoded.clear()

        self.stick.up = True
        result = sync.push_pending(self.stick, self.db)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(self.encoded, [])
        self.assertIn(item["id"], self.stick.films)
        self.assertIsNotNone(prepared.get(self.db, item["id"])["pushed_at"])


class OneFilmAtATime(Fixture):
    def test_a_push_of_one_film_never_reconciles(self):
        # reconcile deletes everything the batch does not mention. On a batch
        # of one that is the whole library.
        first = self.offer(self.incoming, "Hook.mkv")
        second = self.offer(self.incoming, "Ponyo.mkv")
        self.prepare(first)
        self.prepare(second)
        self.assertEqual(self.stick.reconciles, [False, False])
        self.assertEqual(len(self.stick.films), 2)


class CorrectingAFilm(Fixture):
    def test_a_metadata_change_does_not_re_encode(self):
        # The old app's "save and re-prepare" ran the whole library and relied
        # on a cache to skip it. Nothing on the share changes when a title does.
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        self.encoded.clear()

        prepared.set_override(self.db, item["id"], title="Hook",
                              genres=["Adventure"])
        pipeline.refresh_metadata(self.db, item["id"], self.settings, self.stick)

        self.assertEqual(self.encoded, [])
        self.assertEqual(self.stick.films[item["id"]]["title"], "Hook")
        self.assertEqual(self.stick.films[item["id"]]["genres"], ["Adventure"])

    def test_the_film_keeps_its_id_and_therefore_its_place(self):
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        before = prepared.get(self.db, item["id"])["media_rel"]
        prepared.set_override(self.db, item["id"], title="Hook")
        pipeline.refresh_metadata(self.db, item["id"], self.settings, self.stick)
        self.assertEqual(prepared.get(self.db, item["id"])["media_rel"], before)


class TakingAFilmBackOut(Fixture):
    def test_it_leaves_the_stick_the_share_and_the_library(self):
        item = self.offer(self.incoming, "Hook.mkv")
        self.prepare(item)
        self.assertIn(item["id"], self.stick.films)

        result = pipeline.remove(self.db, item["id"], self.settings, self.stick)

        self.assertNotIn(item["id"], self.stick.films)
        self.assertGreater(result["files"], 0)
        self.assertEqual(nas_store.orphans(self.db, self.nas), [])
        self.assertIsNone(prepared.get(self.db, item["id"]))
        # And it is offerable again, rather than gone from the tab.
        self.assertEqual(catalog.get_item(self.db, item["id"])["state"],
                         "available")

    def test_the_original_film_is_never_deleted(self):
        item = self.offer(self.nas, "Hook (1991)", "Hook.mkv")
        self.prepare(item, encode=False)
        pipeline.remove(self.db, item["id"], self.settings, self.stick)
        self.assertTrue(os.path.isfile(item["abs_path"]))


if __name__ == "__main__":
    unittest.main()
