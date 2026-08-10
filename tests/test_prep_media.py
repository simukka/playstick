"""Tests for what scripts/playstick-prep.py CALLS the encode it made.

Written after a library came back with 033fa22cc64e9f97-f1-the-movie.mp4 and
033fa22cc64e9f97-f1.mp4 side by side: the same film, encoded twice, four
gigabytes apart. Nothing had gone wrong in the sense of returning an error. The
encode used to be named after the film's id AND a slug of its title, and the
title is re-derived on every run -- from the filename, then container tags, then
an .nfo, then TMDb. Anything that improves the title renames the encode, and the
"is it already there?" check then looks for a name nobody wrote.

That is the failure mode worth a test file of its own: it is invisible. There is
no error, no warning and no wrong picture on the projector -- only a share
filling up twice as fast as it should, and hours of a machine spent re-making
something it already had.

So the cases below hold the naming contract from both ends: one encode per film,
named for the id alone, and everything else that shares the id -- another film's
file, a poster, a subtitle, a half-written .part -- left exactly where it was.

No ffmpeg: prep.run is replaced by a stub that writes the staging file the real
one would have written. Independent of tests/support.py -- nothing here needs the
daemon, its environment, or a socket.
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "playstick-prep.py")

# The hyphen in the filename keeps it out of `import`, which is the right name
# for a command and the wrong one for a module.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("playstick_prep", SCRIPT)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)

# The id from the library this file exists because of. It is a sha1 of the
# source's path relative to the library root, and nothing about the film's
# metadata can move it.
FILM = "033fa22cc64e9f97"
OTHER = "b41d0e2f7a5c8991"

# The name the bug left behind, and the two it would have written next.
OLD = "%s-f1-the-movie.mp4" % FILM
NEWER_OLD = "%s-f1.mp4" % FILM
CANON = "%s.mp4" % FILM


def args(**kv):
    """Everything do_transcode() and build_transcode_argv() read."""
    defaults = {
        "force": False, "transcode": "auto", "dry_run": False,
        "transcode_timeout": 60, "preset": "veryfast", "crf": 21,
        "width": 1280, "height": 720, "max_fps": 30,
        "max_bitrate": 4_000_000, "audio_bitrate": "160k",
        "no_phone_audio": True, "no_posters": True,
    }
    defaults.update(kv)
    return argparse.Namespace(**defaults)


class MediaFiles(unittest.TestCase):

    def setUp(self):
        # Even warn() is silenced: several of these deliberately provoke one,
        # and a passing suite should not look like a failing run.
        self._verbosity = prep._verbosity
        prep._verbosity = -1
        self.addCleanup(setattr, prep, "_verbosity", self._verbosity)

        self.tmp = tempfile.mkdtemp(prefix="playstick-media-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.media = os.path.join(self.tmp, prep.WORK_DIR, "media")
        os.makedirs(self.media)
        self.paths = {"library": self.tmp, "output": self.tmp}
        self.encodes = []          # one argv per encode actually attempted

    # -- fixtures ----------------------------------------------------------

    def film(self, title="F1", ident=FILM):
        return {
            "id": ident,
            "title": title,
            "source": os.path.join(self.tmp, "F1 The Movie (2025).mkv"),
            "source_rel": "F1 The Movie (2025).mkv",
            "sort_title": "f1",
            "transcode_reasons": ["1920x1080 is larger than the display's 1280x720"],
            "duration": 5460.0,
            "audio_index": 1,
            "fps": 24.0,
        }

    def write(self, name, body, age=0):
        """A file in media/, with an mtime `age` seconds in the past."""
        path = os.path.join(self.media, name)
        with open(path, "wb") as handle:
            handle.write(body)
        when = 1_700_000_000 - age
        os.utime(path, (when, when))
        return path

    def encoder(self, ok=True, body=b"new encode"):
        """A stand-in for run() that writes what ffmpeg would have written.

        build_transcode_argv() puts the staging path last, which is the only
        thing about the argv this needs to know.
        """
        def stub(argv, timeout=None, progress=None):
            self.encodes.append(argv)
            if ok:
                with open(argv[-1], "wb") as handle:
                    handle.write(body)
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "boom")
        return mock.patch.object(prep, "run", stub)

    def names(self):
        return sorted(os.listdir(self.media))

    def body(self, name):
        with open(os.path.join(self.media, name), "rb") as handle:
            return handle.read()

    # -- the bug -----------------------------------------------------------

    def test_a_film_whose_title_changed_adopts_the_encode_it_already_has(self):
        """The reported case: 033fa22cc64e9f97-f1-the-movie.mp4 was on the NAS,
        the title normalised to "F1", and the run encoded the whole film again
        rather than seeing the four gigabytes already sitting there."""
        self.write(OLD, b"the encode we already paid for")
        movie = self.film(title="F1")
        with self.encoder():
            self.assertTrue(prep.do_transcode(movie, args(), self.paths))

        self.assertEqual(self.encodes, [], "it encoded a film it already had")
        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"the encode we already paid for")
        self.assertEqual(movie["media_rel"],
                         os.path.join(prep.WORK_DIR, "media", CANON))
        self.assertTrue(movie["prepared"])

    def test_the_duplicate_the_bug_already_made_is_collapsed(self):
        """A library that has already been through this keeps both files. The
        newer one is the one the current settings produced."""
        self.write(OLD, b"first encode", age=90_000)
        self.write(CANON, b"second encode")
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.encodes, [])
        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"second encode")

    def test_the_newest_of_two_old_names_survives_under_the_new_one(self):
        self.write(OLD, b"older", age=90_000)
        self.write(NEWER_OLD, b"newer", age=10)
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"newer")

    def test_three_generations_collapse_to_one(self):
        self.write(OLD, b"one", age=300_000)
        self.write(NEWER_OLD, b"two", age=200_000)
        self.write(CANON, b"three", age=100_000)
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"three")

    # -- what must not be touched ------------------------------------------

    def test_another_films_encode_is_not_collateral(self):
        self.write("%s-some-other-film.mp4" % OTHER, b"not ours")
        self.write(OLD, b"ours")
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.names(), sorted([CANON, "%s-some-other-film.mp4" % OTHER]))
        self.assertEqual(self.body("%s-some-other-film.mp4" % OTHER), b"not ours")

    def test_a_name_that_only_shares_the_prefix_is_a_different_file(self):
        """The separator is load-bearing. "<id>abc.mp4" is not this film's."""
        self.write("%sabc.mp4" % FILM, b"somebody else's")
        self.write(CANON, b"ours")
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.names(), sorted([CANON, "%sabc.mp4" % FILM]))

    def test_only_mp4s_are_claimed(self):
        """Nothing else writes into media/, but the rule that decides what to
        delete should be narrow regardless of what turns up there."""
        self.write("%s.nfo" % FILM, b"notes")
        self.write(OLD, b"ours", age=90_000)
        self.write(CANON, b"newer")
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        self.assertEqual(self.names(), sorted([CANON, "%s.nfo" % FILM]))

    def test_the_other_artefacts_keyed_on_the_same_id_are_left_alone(self):
        """Posters, subtitles and phone audio never carried the title, so this
        change should be invisible to them -- including for the same film."""
        work = os.path.join(self.tmp, prep.WORK_DIR)
        others = [os.path.join(work, "posters", "%s.jpg" % FILM),
                  os.path.join(work, "subs", "%s.eng.srt" % FILM),
                  os.path.join(work, "audio", FILM, "0.eng.m4a")]
        for path in others:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"keep me")

        self.write(OLD, b"ours")
        with self.encoder():
            prep.do_transcode(self.film(), args(), self.paths)

        for path in others:
            self.assertTrue(os.path.isfile(path), path)

    # -- encoding for real --------------------------------------------------

    def test_a_first_encode_is_named_for_the_id_alone(self):
        movie = self.film(title="F1 The Movie")
        with self.encoder(body=b"fresh"):
            self.assertTrue(prep.do_transcode(movie, args(), self.paths))

        self.assertEqual(len(self.encodes), 1)
        self.assertEqual(self.names(), [CANON])
        self.assertNotIn("movie", self.names()[0])
        self.assertEqual(self.body(CANON), b"fresh")
        self.assertEqual(movie["media_rel"],
                         os.path.join(prep.WORK_DIR, "media", CANON))

    def test_a_failed_encode_leaves_nothing_behind(self):
        with self.encoder(ok=False):
            self.assertFalse(prep.do_transcode(self.film(), args(), self.paths))

        self.assertEqual(self.names(), [])

    def test_force_re_encodes_and_still_leaves_one_file(self):
        self.write(OLD, b"the old encode")
        with self.encoder(body=b"forced"):
            self.assertTrue(prep.do_transcode(self.film(), args(force=True),
                                              self.paths))

        self.assertEqual(len(self.encodes), 1)
        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"forced")

    def test_a_force_that_fails_does_not_cost_the_encode_already_there(self):
        """--force means re-encode, not "delete what you have and hope". A run
        stopped at hour two must leave the library exactly as it found it."""
        self.write(OLD, b"the old encode")
        with self.encoder(ok=False):
            self.assertFalse(prep.do_transcode(self.film(), args(force=True),
                                               self.paths))

        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"the old encode")

    # -- awkward filesystems ------------------------------------------------

    def test_a_rename_that_cannot_happen_is_still_not_a_reason_to_re_encode(self):
        """A read-only share, or one that will not rename a file being read.
        Using the old name is worse than tidy; encoding it again is worse than
        that, and is the bug."""
        self.write(OLD, b"the old encode")
        movie = self.film()
        with mock.patch.object(prep.os, "replace",
                               side_effect=OSError("read-only file system")):
            with self.encoder():
                self.assertTrue(prep.do_transcode(movie, args(), self.paths))

        self.assertEqual(self.encodes, [])
        self.assertEqual(self.names(), [OLD])
        self.assertEqual(movie["media_rel"],
                         os.path.join(prep.WORK_DIR, "media", OLD))

    def test_a_leftover_staging_file_is_not_an_encode(self):
        """A run killed with SIGKILL leaves a .part; an interrupted one cleans
        up after itself. Either way it is half a file and must not be adopted."""
        self.write("%s.mp4.part" % NEWER_OLD[:-4], b"half a film")
        self.write("%s.part" % CANON, b"half a film")
        with self.encoder(body=b"whole"):
            self.assertTrue(prep.do_transcode(self.film(), args(), self.paths))

        self.assertEqual(len(self.encodes), 1)
        self.assertEqual(self.names(), [CANON])
        self.assertEqual(self.body(CANON), b"whole")

    def test_the_survivor_does_not_depend_on_the_directory_order(self):
        """Two files with the same mtime is what copying a directory onto a NAS
        produces. Whatever gets picked, it has to be the same one every run --
        otherwise the library flips between two encodes and neither ever wins."""
        picked = []
        for _ in range(2):
            for name in self.names():
                os.unlink(os.path.join(self.media, name))
            self.write(OLD, b"a")
            self.write(NEWER_OLD, b"b")
            with self.encoder():
                prep.do_transcode(self.film(), args(), self.paths)
            picked.append(self.body(CANON))

        self.assertEqual(self.names(), [CANON])
        self.assertEqual(picked[0], picked[1])

    def test_a_missing_media_directory_is_not_an_error(self):
        shutil.rmtree(self.media)
        self.assertEqual(prep.existing_encodes(self.media, FILM), [])
        with self.encoder():
            self.assertTrue(prep.do_transcode(self.film(), args(), self.paths))
        self.assertEqual(self.names(), [CANON])

    # -- the rest of the run ------------------------------------------------

    def test_a_dry_run_removes_nothing(self):
        """It reports what it would do. Deleting an encode is not reporting."""
        self.write(OLD, b"one", age=90_000)
        self.write(CANON, b"two")
        with self.encoder():
            prep.prepare_one(self.film(), args(dry_run=True, transcode="always"),
                             self.paths, None)

        self.assertEqual(self.encodes, [])
        self.assertEqual(self.names(), sorted([OLD, CANON]))

    def test_the_index_points_the_daemon_at_the_canonical_name(self):
        """`rel` is joined onto PLAYSTICK_LIBRARY by the daemon, with forward
        slashes, and is the whole reason the rename has to reach the index."""
        self.write(OLD, b"the old encode")
        movie = self.film()
        with self.encoder():
            prep.do_transcode(movie, args(), self.paths)
        index = prep.build_index([movie], [], args(), self.paths)

        self.assertEqual(index["movies"][0]["rel"],
                         ".playstick/media/%s.mp4" % FILM)
        self.assertEqual(index["movies"][0]["source_rel"],
                         "F1 The Movie (2025).mkv")

    def test_an_unencoded_film_still_points_at_its_source(self):
        """Nothing about this change should make a film that never needed an
        encode start claiming one."""
        movie = self.film()
        index = prep.build_index([movie], [], args(), self.paths)
        self.assertEqual(index["movies"][0]["rel"], "F1 The Movie (2025).mkv")


class EncodeNames(unittest.TestCase):
    """The naming rule on its own, without a filesystem."""

    def test_the_name_is_the_id_and_nothing_else(self):
        self.assertEqual(prep.encode_name(FILM), "033fa22cc64e9f97.mp4")

    def test_the_id_does_not_move_when_the_title_does(self):
        """The premise the whole fix rests on: file_id() is a sha1 of the path,
        so improving a film's metadata cannot rename its encode."""
        self.assertEqual(prep.file_id("F1 The Movie (2025).mkv"),
                         prep.file_id("F1 The Movie (2025).mkv"))
        self.assertNotEqual(prep.file_id("a.mkv"), prep.file_id("b.mkv"))


if __name__ == "__main__":
    unittest.main()
