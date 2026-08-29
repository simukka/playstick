"""The preparer, asked about one film instead of a library.

The admin app no longer runs the whole library and lets a cache skip what is
done -- it prepares exactly the films somebody selected, one at a time, from
sources that are mostly nowhere near the share. Three things had to change in
the preparer for that, and each of them has a way of being wrong that nothing
would report:

`collect_one` lets the caller name the film. The id is what a box in another
room knows it by, and three things over there hang off it -- the poster on the
grid, every correction anybody has made, and where each film got to. So the
cases below check that the default is still the old answer (a library already
on a stick must not be re-keyed) and that a caller's answer wins even on a cache
hit, which is the path that would otherwise hand back a remembered id.

`match_tmdb` is a pure extraction. It is tested because the rule it carries --
that a match has to survive the title, the year AND the runtime -- is the rule
this project has already been burned by once, and the whole reason to extract it
was to stop a second copy of it existing.

`place_media` is new, and it closes a gap the restructure opened. A film that
needs no encode used to be indexed by its path relative to the library, which
was right while the library and the share were the same directory. From a source
on somebody's desk it is a path the stick cannot resolve, and the symptom is a
film that syncs, appears on the grid, and does not play.

No ffmpeg: prep.run is a stub that writes what the real one would have written.
"""

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "src", "prep", "playstick_prep.py")

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("playstick_prep", SCRIPT)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


def args(**kv):
    defaults = {
        "force": False, "transcode": "auto", "dry_run": False,
        "transcode_timeout": 60, "preset": "veryfast", "crf": 21,
        "width": 1280, "height": 720, "max_fps": 30,
        "max_bitrate": 4_000_000, "audio_bitrate": "160k",
        "no_phone_audio": True, "no_posters": True,
        "verify": "none", "dedupe": False, "prefer_metadata_titles": False,
        "probe_timeout": 5,
    }
    defaults.update(kv)
    return argparse.Namespace(**defaults)


class Quiet(unittest.TestCase):
    def setUp(self):
        self._verbosity = prep._verbosity
        prep._verbosity = -1
        self.addCleanup(setattr, prep, "_verbosity", self._verbosity)
        self.tmp = tempfile.mkdtemp(prefix="playstick-single-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def file(self, *parts, body=b"a film"):
        path = os.path.join(self.tmp, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(body)
        return path


class WhoNamesTheFilm(Quiet):
    def test_the_default_is_still_the_old_answer(self):
        # A library already prepared and already on a stick must come back with
        # the same sixteen characters, or every poster, correction and resume
        # position over there is quietly orphaned.
        path = self.file("Hook (1991)", "Hook.mkv")
        movie = prep.collect_one(path, "Hook (1991)/Hook.mkv", args(), {})
        self.assertEqual(movie["id"], prep.file_id("Hook (1991)/Hook.mkv"))

    def test_collect_still_derives_rel_from_the_library(self):
        path = self.file("Hook (1991)", "Hook.mkv")
        by_library = prep.collect(path, self.tmp, args(), {})
        by_hand = prep.collect_one(path, "Hook (1991)/Hook.mkv", args(), {})
        self.assertEqual(by_library["id"], by_hand["id"])
        self.assertEqual(by_library["source_rel"], by_hand["source_rel"])

    def test_a_callers_id_wins_over_a_remembered_one(self):
        # The cache stores the whole derived movie dict, id included, and a hit
        # returns it without probing. A film reached through a new source -- or
        # one that has since been copied onto the share -- gets a different id,
        # and handing back the remembered one would push a row the stick
        # already has under another name.
        path = self.file("Hook.mkv")
        stat = os.stat(path)
        state = {"Hook.mkv": {
            "fingerprint": [int(stat.st_size), int(stat.st_mtime)],
            "verify_mode": "none",
            "meta_version": prep.META_VERSION,
            "movie": {"id": "a" * 16, "source_rel": "Hook.mkv", "title": "Hook",
                      "audio_streams_info": []},
        }}
        cached = prep.collect_one(path, "Hook.mkv", args(), state, ident="b" * 16)
        self.assertEqual(cached["title"], "Hook")   # it really was a cache hit
        self.assertEqual(cached["id"], "b" * 16)

    def test_an_unreadable_file_still_carries_the_callers_id(self):
        path = self.file("broken.mkv")
        movie = prep.collect_one(path, "broken.mkv", args(), {}, ident="c" * 16)
        self.assertTrue(movie["unreadable"])
        self.assertEqual(movie["id"], "c" * 16)


class Tmdb(Quiet):
    """The confidence gate, exercised through the extracted helper."""

    class Stub:
        def __init__(self, meta=None, why=None, by_id=None):
            self.meta, self.why, self.by_id = meta, why, by_id
            self.searched = self.fetched = None

        def lookup(self, title, year, duration):
            self.searched = (title, year, duration)
            return self.meta, self.why

        def fetch_by_id(self, tmdb_id):
            self.fetched = tmdb_id
            return self.by_id, None

    def test_a_pinned_match_skips_the_search_entirely(self):
        stub = self.Stub(by_id={"title": "Hook", "year": 1991})
        movie = {"id": "x" * 16, "title": "Red Hook Summer", "duration": 8520}
        why, matched = prep.match_tmdb(
            movie, stub, {"x" * 16: {"tmdb_id": 2493}}, args())
        self.assertEqual(stub.fetched, 2493)
        self.assertIsNone(stub.searched)
        self.assertIsNone(why)
        self.assertTrue(matched)

    def test_an_unconfident_match_is_reported_not_applied(self):
        stub = self.Stub(meta=None, why="best was Red Hook Summer (121 min)")
        movie = {"id": "x" * 16, "title": "Hook", "year": 1991, "duration": 8520}
        why, matched = prep.match_tmdb(movie, stub, {}, args())
        self.assertFalse(matched)
        self.assertIn("Red Hook Summer", why)
        self.assertEqual(movie["title"], "Hook")      # unchanged

    def test_a_runtime_that_disagrees_becomes_a_note(self):
        # The one signal that owes nothing to anybody's spelling.
        stub = self.Stub(meta={"title": "Hook", "expected_runtime": 8520})
        movie = {"id": "x" * 16, "title": "Hook", "duration": 4000}
        prep.match_tmdb(movie, stub, {}, args())
        self.assertTrue(any("cut, or incomplete" in n
                            for n in movie.get("verify_notes") or []))

    def test_an_extended_edition_is_a_different_note(self):
        stub = self.Stub(meta={"title": "Hook", "expected_runtime": 6000})
        movie = {"id": "x" * 16, "title": "Hook", "duration": 9000}
        prep.match_tmdb(movie, stub, {}, args())
        self.assertTrue(any("extended edition" in n
                            for n in movie.get("verify_notes") or []))


class PlacingTheFilm(Quiet):
    def setUp(self):
        super().setUp()
        self.nas = os.path.join(self.tmp, "nas")
        os.makedirs(self.nas)
        self.calls = []

    def remuxer(self, ok=True):
        """What ffmpeg -c copy would have written. The destination is last in
        the argv, which is the only thing about it this needs to know."""
        def stub(argv, timeout=None, progress=None):
            self.calls.append(argv)
            if ok:
                with open(argv[-1], "wb") as handle:
                    handle.write(b"remuxed")
            return type("R", (), {"returncode": 0 if ok else 1,
                                  "stdout": "", "stderr": "" if ok else "no"})()
        return stub

    def film(self, source, ident="d" * 16):
        return {"id": ident, "title": "Hook", "source": source,
                "source_rel": os.path.basename(source), "duration": 8520.0,
                "audio_index": 1}

    def test_an_encode_is_left_alone(self):
        movie = self.film(self.file("Hook.mkv"))
        movie["media_rel"] = ".playstick/media/d.mp4"
        prep.run = self.remuxer()
        self.assertEqual(prep.place_media(movie, args(), self.nas),
                         ".playstick/media/d.mp4")
        self.assertEqual(self.calls, [])          # nothing copied

    def test_a_film_already_on_the_share_is_not_copied(self):
        # The whole reason a film on the share is cheap: it plays from where it
        # lies, and its path relative to the share is one the stick can resolve.
        source = self.file("nas", "Hook (1991)", "Hook.mkv")
        movie = self.film(source)
        prep.run = self.remuxer()
        rel = prep.place_media(movie, args(), self.nas)
        self.assertEqual(rel, "Hook (1991)/Hook.mkv")
        self.assertEqual(self.calls, [])
        self.assertEqual(movie["media_rel"], "Hook (1991)/Hook.mkv")

    def test_a_film_from_elsewhere_is_remuxed_onto_the_share(self):
        movie = self.film(self.file("incoming", "Hook.mkv"))
        prep.run = self.remuxer()
        rel = prep.place_media(movie, args(), self.nas)
        self.assertEqual(rel, ".playstick/media/%s.mp4" % ("d" * 16))
        landed = os.path.join(self.nas, rel)
        self.assertTrue(os.path.isfile(landed))
        # Staged and renamed, never written in place: the stick mounts the
        # share with actimeo=60 and would serve a stale size for a minute.
        self.assertFalse(os.path.exists(landed + ".part"))
        self.assertIn("copy", self.calls[0])

    def test_a_container_that_will_not_hold_it_falls_back_to_the_bytes(self):
        # Refusing the film over a container preference would be choosing
        # tidiness over the film. mpv plays matroska perfectly well.
        source = self.file("incoming", "Hook.mkv", body=b"original bytes")
        movie = self.film(source)
        prep.run = self.remuxer(ok=False)
        rel = prep.place_media(movie, args(), self.nas)
        self.assertEqual(rel, ".playstick/media/%s.mkv" % ("d" * 16))
        with open(os.path.join(self.nas, rel), "rb") as handle:
            self.assertEqual(handle.read(), b"original bytes")

    def test_a_second_run_does_not_copy_it_again(self):
        movie = self.film(self.file("incoming", "Hook.mkv"))
        prep.run = self.remuxer()
        prep.place_media(movie, args(), self.nas)
        again = self.film(movie["source"])
        prep.place_media(again, args(), self.nas)
        self.assertEqual(len(self.calls), 1)

    def test_the_rel_it_returns_is_what_the_daemon_will_accept(self):
        # Every path in a push is re-validated against the stick's own library
        # root, and anything absolute or escaping is refused outright.
        movie = self.film(self.file("incoming", "Hook.mkv"))
        prep.run = self.remuxer()
        rel = prep.place_media(movie, args(), self.nas)
        self.assertFalse(os.path.isabs(rel))
        self.assertNotIn("..", rel.split("/"))


class ThePayloadHasOneTranslation(Quiet):
    def test_device_film_matches_device_films(self):
        # device_films() is the whole-library path and device_film() is the
        # per-film one. They are the same function or they are a bug waiting
        # for somebody to rename a key in build_index().
        entry = {
            "id": "e" * 16, "source_rel": "Hook/Hook.mkv",
            "rel": ".playstick/media/e.mp4", "title": "Hook", "year": 1991,
            "genres": ["Adventure"],
            "subtitles": [{"rel": ".playstick/subs/e.eng.srt", "lang": "eng"}],
            "audio": [{"n": 0, "rel": ".playstick/audio/e/0.eng.m4a",
                       "lang": "eng", "default": True}],
        }
        self.assertEqual(prep.device_films({"movies": [entry]}),
                         [prep.device_film(entry)])

    def test_an_entry_with_no_id_is_refused_rather_than_half_sent(self):
        self.assertIsNone(prep.device_film({"source_rel": "Hook.mkv"}))
        self.assertIsNone(prep.device_film({"id": "f" * 16}))


if __name__ == "__main__":
    unittest.main()
