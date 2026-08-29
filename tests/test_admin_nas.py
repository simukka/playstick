"""Moving a finished film onto the share, and taking one back off.

This is the step where the admin app touches somebody's film collection, so the
cases here are mostly about restraint: what it will not write over, what it will
not delete, and what it leaves behind when it fails.

The two worth reading are `AFailedMoveLeavesTheShareUsable` and
`RemovalTouchesOnlyWhatPreparingMade`. The first is why the database row is
written after the last rename rather than before the first: a crash in the
middle must leave reclaimable debris, never a film the stick has been told about
and cannot play. The second is why `remove` refuses any path outside the two
directories this app owns -- a film that was already on the share plays from
where it lies, and its own file is not this app's to delete.
"""

import errno
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import nas_store, prepared                 # noqa: E402
from playstick_admin.core.db import Database                         # noqa: E402

MEDIA = ".playstick/media/%s.mp4"
SUB = ".playstick/subs/%s.eng.srt"
AUDIO = ".playstick/audio/%s/0.eng.m4a"
FILM = "a1b2c3d4e5f60718"


class Fixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self.nas = os.path.join(self.dir, "nas")
        self.staging = os.path.join(self.dir, "staging")
        os.makedirs(self.nas)
        os.makedirs(self.staging)

    def staged(self, rel, body=b"x" * 64):
        path = os.path.join(nas_store.staging_for(self.staging, FILM),
                            rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def on_nas(self, rel, body=b"y"):
        path = os.path.join(self.nas, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path

    def rels(self):
        return [MEDIA % FILM, SUB % FILM, AUDIO % FILM]


class WhatMoves(Fixture):
    def test_the_layout_on_the_share_matches_the_one_in_staging(self):
        for rel in self.rels():
            self.staged(rel)
        moved = nas_store.place(self.staging, self.nas, FILM, self.rels())
        self.assertEqual(moved, 64 * 3)
        for rel in self.rels():
            self.assertTrue(os.path.isfile(
                os.path.join(self.nas, rel.replace("/", os.sep))), rel)

    def test_nothing_is_left_half_named(self):
        # A file written in place is served at a stale size for a minute on a
        # mount with actimeo=60, and a wrong Content-Length is a phone that
        # stops halfway through a film.
        self.staged(MEDIA % FILM)
        nas_store.place(self.staging, self.nas, FILM, [MEDIA % FILM])
        leftovers = []
        for dirpath, _d, files in os.walk(self.nas):
            leftovers += [f for f in files if f.endswith(".part")]
        self.assertEqual(leftovers, [])

    def test_subtitles_land_before_the_film(self):
        # The daemon stats every subtitle path before it starts mpv; a missing
        # one is a hard error at film start rather than a missing feature.
        for rel in self.rels():
            self.staged(rel)
        order = []
        real = nas_store._move_one

        def spy(src, dest):
            order.append(dest)
            return real(src, dest)

        with mock.patch.object(nas_store, "_move_one", spy):
            nas_store.place(self.staging, self.nas, FILM, self.rels())
        self.assertTrue(order[-1].endswith(".mp4"),
                        "the film should be the last thing to cross")

    def test_a_film_already_on_the_share_is_not_looked_for_in_staging(self):
        # place_media() writes a film that needed no encode straight onto the
        # share: a copy has no reason to go via local disk and would cost two.
        self.staged(SUB % FILM)
        moved = nas_store.place(self.staging, self.nas, FILM,
                                ["Hook (1991)/Hook.mkv", SUB % FILM])
        self.assertEqual(moved, 64)

    def test_a_move_across_filesystems_still_lands(self):
        for rel in self.rels():
            self.staged(rel)
        real = os.replace
        state = {"first": True}

        def cross_device(src, dest):
            # The first call per file is staging -> .part; make that fail the
            # way a different filesystem does, and let the rename through.
            if src.startswith(self.staging):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dest)

        with mock.patch.object(os, "replace", cross_device):
            nas_store.place(self.staging, self.nas, FILM, self.rels())
        for rel in self.rels():
            self.assertTrue(os.path.isfile(
                os.path.join(self.nas, rel.replace("/", os.sep))), rel)


class AFailedMoveLeavesTheShareUsable(Fixture):
    def test_the_part_file_is_cleaned_up(self):
        self.staged(MEDIA % FILM)
        with mock.patch.object(nas_store, "shutil") as fake:
            fake.copyfile.side_effect = OSError(errno.ENOSPC, "no space")
            with mock.patch.object(
                    os, "replace",
                    side_effect=OSError(errno.EXDEV, "cross-device")):
                with self.assertRaises(nas_store.NasError):
                    nas_store.place(self.staging, self.nas, FILM,
                                    [MEDIA % FILM])
        self.assertFalse(os.path.exists(
            os.path.join(self.nas, (MEDIA % FILM).replace("/", os.sep)) + ".part"))

    def test_what_already_crossed_is_left_for_the_retry(self):
        # Deleting it would mean the retry re-copies what it had already got
        # across, which on a 20 GB film over wireless is the difference between
        # a nuisance and an evening.
        for rel in self.rels():
            self.staged(rel)
        real = nas_store._move_one

        def fail_on_the_film(src, dest):
            if dest.endswith(".mp4"):
                raise nas_store.NasError("the mount went away")
            return real(src, dest)

        with mock.patch.object(nas_store, "_move_one", fail_on_the_film):
            with self.assertRaises(nas_store.NasError):
                nas_store.place(self.staging, self.nas, FILM, self.rels())
        self.assertTrue(os.path.isfile(
            os.path.join(self.nas, (SUB % FILM).replace("/", os.sep))))


class Orphans(Fixture):
    def setUp(self):
        super().setUp()
        self.db = Database(os.path.join(self.dir, "admin.db"))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self.db.write().execute(
            "INSERT INTO item (id, rel, abs_path, seen_at) VALUES (?,?,?,0)",
            (FILM, "Hook.mkv", "/x/Hook.mkv"))

    def test_a_crash_mid_move_leaves_reclaimable_debris(self):
        self.on_nas(MEDIA % FILM)
        self.on_nas(SUB % FILM)
        # No prepared row: this is exactly the state a job that died between
        # the last rename and the commit leaves behind.
        found = dict(nas_store.orphans(self.db, self.nas))
        self.assertIn(MEDIA % FILM, found)
        self.assertIn(SUB % FILM, found)

    def test_a_recorded_film_is_not_an_orphan(self):
        self.on_nas(MEDIA % FILM)
        self.on_nas(SUB % FILM)
        prepared.record(
            self.db, FILM, {"title": "Hook",
                            "subtitles": [{"rel": SUB % FILM, "lang": "eng"}]},
            MEDIA % FILM, MEDIA % FILM)
        self.assertEqual(nas_store.orphans(self.db, self.nas), [])

    def test_a_part_file_is_always_debris(self):
        prepared.record(self.db, FILM, {"title": "Hook"}, MEDIA % FILM,
                        MEDIA % FILM)
        self.on_nas(MEDIA % FILM)
        self.on_nas((MEDIA % FILM) + ".part")
        found = [rel for rel, _size in nas_store.orphans(self.db, self.nas)]
        self.assertEqual(found, [(MEDIA % FILM) + ".part"])

    def test_the_sweep_never_looks_at_the_film_collection(self):
        # Everything on the share outside .playstick/ is somebody's films.
        self.on_nas("Hook (1991)/Hook.mkv")
        self.on_nas("holiday-video.mp4")
        self.assertEqual(nas_store.orphans(self.db, self.nas), [])


class RemovalTouchesOnlyWhatPreparingMade(Fixture):
    def test_the_artifacts_go(self):
        for rel in self.rels():
            self.on_nas(rel)
        removed, freed = nas_store.remove(self.nas, self.rels())
        self.assertEqual(removed, 3)
        self.assertEqual(freed, 3)
        for rel in self.rels():
            self.assertFalse(os.path.exists(
                os.path.join(self.nas, rel.replace("/", os.sep))))

    def test_the_original_film_is_never_touched(self):
        original = self.on_nas("Hook (1991)/Hook.mkv")
        removed, _freed = nas_store.remove(self.nas, ["Hook (1991)/Hook.mkv"])
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.isfile(original))

    def test_a_path_that_climbs_out_is_refused(self):
        outside = os.path.join(self.dir, "precious.mkv")
        with open(outside, "w") as handle:
            handle.write("x")
        nas_store.remove(self.nas, ["../precious.mkv",
                                    ".playstick/../../precious.mkv"])
        self.assertTrue(os.path.isfile(outside))

    def test_an_emptied_audio_directory_is_taken_away_too(self):
        self.on_nas(AUDIO % FILM)
        nas_store.remove(self.nas, [AUDIO % FILM])
        self.assertFalse(os.path.exists(
            os.path.join(self.nas, ".playstick", "audio", FILM)))


class Space(Fixture):
    def test_a_move_that_will_not_fit_is_refused_before_it_starts(self):
        # ENOSPC six gigabytes into a copy over CIFS costs an hour and leaves
        # debris, and it is entirely predictable.
        self.assertIsNone(nas_store.check_space(self.nas, 1024))
        complaint = nas_store.check_space(self.nas, 1 << 62)
        self.assertIn("needs about", complaint)


class Staging(Fixture):
    def test_the_debris_of_a_crash_is_cleared_but_a_resume_is_kept(self):
        self.staged(MEDIA % FILM)
        os.makedirs(os.path.join(self.staging, "b" * 16))
        self.assertEqual(nas_store.sweep_staging(self.staging, keep=[FILM]), 1)
        self.assertTrue(os.path.isdir(
            nas_store.staging_for(self.staging, FILM)))

    def test_the_poster_is_read_rather_than_moved(self):
        # The stick serves posters from its own database and has never opened
        # one off the mount.
        self.staged(".playstick/posters/%s.jpg" % FILM, body=b"jpegbytes")
        data, mime = nas_store.read_poster(self.staging, FILM)
        self.assertEqual(data, b"jpegbytes")
        self.assertEqual(mime, "image/jpeg")

    def test_artifact_rels_ignores_a_half_written_step(self):
        movie = {"media_rel": MEDIA % FILM,
                 "subtitles": [{"rel": SUB % FILM}, {}],
                 "phone_audio": [{"rel": AUDIO % FILM}]}
        self.assertEqual(sorted(nas_store.artifact_rels(movie)),
                         sorted(self.rels()))


if __name__ == "__main__":
    unittest.main()
