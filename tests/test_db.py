"""The store: the schema, the sync, the overrides, and what survives a re-sync.

These use the REAL Database, Library and Thumbs on a scratch file, not the
fakes -- the fakes exist so the HTTP tests can exercise routing without a
store, and a test about the store that used them would only be asserting that
the fake works.

THE PROPERTY THIS FILE EXISTS FOR is at the bottom: a sync must not throw away
what the device knows that the machine syncing it does not. Resume positions,
uploaded posters and metadata edits all live in tables that cascade from
`film`, so a sync implemented as delete-everything-and-reinsert would silently
wipe every one of them each time somebody pressed the button -- and would look
completely correct in the grid afterwards, which is what makes it worth a test
rather than a comment.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import LIBRARY_DIR, real_store          # noqa: E402


FILM = "0123456789abcdef"
OTHER = "fedcba9876543210"


def film(ident=FILM, rel="Ponyo/Ponyo.mkv", **extra):
    """One entry in the shape /api/admin/films has already validated into."""
    entry = {
        "id": ident,
        "source_rel": rel,
        "media_rel": rel,
        "title": "Ponyo",
        "sort_title": "ponyo",
        "year": 2008,
        "rating": "8.0",
        "plot": None,
        "duration": 6060.0,
        "width": 1280, "height": 720, "vcodec": "h264", "acodec": "aac",
        "tmdb_id": 12429,
        "hidden": 0,
        "genres": [],
        "subtitles": [],
        "audio": [],
    }
    entry.update(extra)
    return entry


class Schema(unittest.TestCase):
    def test_a_fresh_file_gets_every_table(self):
        db, _library, _thumbs = real_store("schema")
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertLessEqual(
            {"film", "film_genre", "subtitle", "audio_track", "thumb",
             "override", "playback", "meta"}, names)

    def test_migrating_twice_changes_nothing(self):
        """Called at every start, and a device restarts on every deploy."""
        db, _library, _thumbs = real_store("twice")
        self.assertEqual(db.migrate(), db.migrate())

    def test_foreign_keys_are_on(self):
        """Not the default, and every ON DELETE CASCADE depends on it. It is a
        per-CONNECTION pragma, so a thread that opened its own without it would
        leave orphans behind that no query would ever show."""
        db, _library, _thumbs = real_store("fk")
        self.assertEqual(db.one("PRAGMA foreign_keys")[0], 1)

    def test_it_is_in_wal(self):
        """A poster extraction landing a blob must not block six phones polling
        status."""
        db, _library, _thumbs = real_store("wal")
        self.assertEqual(db.one("PRAGMA journal_mode")[0].lower(), "wal")


class Sync(unittest.TestCase):
    def test_a_film_arrives_with_its_children(self):
        _db, library, _thumbs = real_store("children")
        library.sync([film(genres=["Animation", "Family"],
                           audio=[{"n": 1, "rel": "Ponyo/eng.m4a",
                                   "lang": "eng", "title": "", "channels": 2,
                                   "default": True, "offset": 0.25}])])
        item = library.get(FILM)
        self.assertEqual(item["title"], "Ponyo")
        self.assertEqual(item["genres"], ["Animation", "Family"])
        self.assertEqual(item["audio"][0]["lang"], "eng")
        self.assertEqual(item["audio"][0]["offset"], 0.25)

    def test_genres_keep_the_order_they_arrived_in(self):
        """TMDb's order, which puts the most characteristic genre first. A set
        would file every film under whatever sorted first alphabetically."""
        _db, library, _thumbs = real_store("genre-order")
        library.sync([film(genres=["Fantasy", "Adventure", "Animation"])])
        self.assertEqual(library.get(FILM)["genres"],
                         ["Fantasy", "Adventure", "Animation"])

    def test_the_order_films_were_sent_in_is_the_shelf_order(self):
        """The preparer files by a normalised title, so "The Fifth Element"
        sits under F. Re-sorting here on `title` would file every "The"
        together, which is the one thing the sort key exists to prevent."""
        _db, library, _thumbs = real_store("order")
        library.sync([
            film(OTHER, "Fifth/Fifth.mkv", title="The Fifth Element",
                 sort_title="fifth element"),
            film(FILM, "Ponyo/Ponyo.mkv", title="Ponyo", sort_title="ponyo"),
        ])
        order, _items, _a, _s, _e = library.snapshot()
        self.assertEqual(order, [OTHER, FILM])

    def test_a_resync_updates_rather_than_duplicating(self):
        _db, library, _thumbs = real_store("resync")
        library.sync([film()])
        library.sync([film(title="Ponyo on the Cliff")])
        order, items, _a, _s, _e = library.snapshot()
        self.assertEqual(len(order), 1)
        self.assertEqual(items[FILM]["title"], "Ponyo on the Cliff")

    def test_children_a_film_lost_are_gone(self):
        """A subtitle track the preparer stopped extracting must not linger:
        the path is handed to mpv, and one that is not there is a hard error at
        film start rather than a missing option."""
        _db, library, _thumbs = real_store("lost")
        library.sync([film(genres=["Animation"])])
        library.sync([film(genres=[])])
        self.assertEqual(library.get(FILM)["genres"], [])

    def test_reconcile_removes_a_film_that_is_no_longer_there(self):
        _db, library, _thumbs = real_store("reconcile")
        library.sync([film(FILM), film(OTHER, "Gone/Gone.mkv")])
        written, removed = library.sync([film(FILM)], reconcile=True)
        self.assertEqual((written, removed), (1, 1))
        self.assertEqual(library.snapshot()[0], [FILM])

    def test_without_reconcile_nothing_is_removed(self):
        """Correcting one film must not make the device conclude the other
        ninety-nine were deleted."""
        _db, library, _thumbs = real_store("no-reconcile")
        library.sync([film(FILM), film(OTHER, "Gone/Gone.mkv")])
        library.sync([film(FILM, title="Fixed")])
        self.assertEqual(len(library.snapshot()[0]), 2)

    def test_an_empty_reconciling_sync_empties_the_library(self):
        """The share really can lose every film -- somebody points the admin
        app at the wrong directory. It should say so rather than keeping a
        library of paths that no longer resolve."""
        _db, library, _thumbs = real_store("empty")
        library.sync([film()])
        self.assertEqual(library.sync([], reconcile=True), (0, 1))
        self.assertEqual(library.snapshot()[0], [])

    def test_a_path_outside_the_library_is_never_hydrated(self):
        """The second of the two checks. The HTTP layer refuses to STORE such a
        path; this refuses to USE one, so a database edited by hand -- or a bug
        in that first check -- still cannot put a path outside the share in
        front of mpv.
        """
        _db, library, _thumbs = real_store("escape")
        library.sync([film(media_rel="../../etc/passwd")])
        # It logs, loudly, because reaching this means the check on the way in
        # let something through -- which would be a bug worth seeing. Silenced
        # here only so the assertion below is what this test says.
        with mock.patch("playstick.library.log"):
            self.assertIsNone(library.get(FILM))
            self.assertEqual(library.snapshot()[0], [])

    def test_a_rating_survives_as_whatever_it_was(self):
        """Both spellings of "how this film scores" go in one TEXT column: a
        TMDb score and a .nfo certificate. The page formats a number
        differently from a word, so the distinction has to survive."""
        _db, library, _thumbs = real_store("rating")
        library.sync([film(FILM, rating="7.8"),
                      film(OTHER, "Cert/Cert.mkv", rating="PG")])
        items = library.snapshot()[1]
        self.assertEqual(items[FILM]["rating"], 7.8)
        self.assertEqual(items[OTHER]["rating"], "PG")


class Overrides(unittest.TestCase):
    def test_an_edit_wins_over_the_synced_value(self):
        _db, library, _thumbs = real_store("edit")
        library.sync([film()])
        item = library.apply_override(FILM, {"title": "Ponyo (Ghibli)"})
        self.assertEqual(item["title"], "Ponyo (Ghibli)")
        self.assertEqual(library.snapshot()[1][FILM]["title"], "Ponyo (Ghibli)")

    def test_clearing_an_edit_drops_back_to_what_the_sync_said(self):
        """Not to the last value anybody typed. This is why the overrides are
        their own table rather than columns on `film` -- a reset is a DELETE,
        and the base value is still sitting there underneath."""
        _db, library, _thumbs = real_store("reset")
        library.sync([film()])
        library.apply_override(FILM, {"title": "Wrong"})
        item = library.apply_override(FILM, {"title": None})
        self.assertEqual(item["title"], "Ponyo")
        self.assertEqual(library.overrides(), {})

    def test_editing_one_field_leaves_the_others_alone(self):
        _db, library, _thumbs = real_store("one-field")
        library.sync([film()])
        library.apply_override(FILM, {"title": "A"})
        library.apply_override(FILM, {"year": 1999})
        self.assertEqual(library.overrides()[FILM],
                         {"title": "A", "year": 1999})

    def test_a_list_valued_edit_round_trips(self):
        """genres is the one editable field that is not a scalar, and the value
        column is JSON precisely so it fits beside the ones that are."""
        _db, library, _thumbs = real_store("genres")
        library.sync([film()])
        item = library.apply_override(FILM, {"genres": ["Anime", "Family"]})
        self.assertEqual(item["genres"], ["Anime", "Family"])

    def test_a_path_bearing_field_is_never_stored(self):
        """The same rule EDITABLE has always enforced, now with a second reason
        to hold: /api/admin/films is where paths are set and it validates every
        one. An edit must not be a way around that."""
        _db, library, _thumbs = real_store("no-paths")
        library.sync([film()])
        library.apply_override(FILM, {"media_rel": "../../etc/passwd",
                                      "title": "Fine"})
        self.assertEqual(library.overrides()[FILM], {"title": "Fine"})
        self.assertTrue(library.get(FILM)["path"].startswith(
            os.path.realpath(LIBRARY_DIR)))

    def test_an_unknown_film_cannot_be_edited(self):
        _db, library, _thumbs = real_store("unknown")
        self.assertIsNone(library.apply_override(FILM, {"title": "Nope"}))

    def test_deleting_a_film_takes_its_edits_with_it(self):
        _db, library, _thumbs = real_store("cascade")
        library.sync([film()])
        library.apply_override(FILM, {"title": "Gone soon"})
        library.sync([], reconcile=True)
        self.assertEqual(library.overrides(), {})


class TheShelfOrder(unittest.TestCase):
    """`pos` is the order a child sees, and it must not depend on how a film
    arrived.

    It used to be the film's index within its own batch, which was right while
    the only kind of sync was the whole library at once. The admin app pushes
    ONE film at a time now -- each finished preparation, with reconcile off --
    and a batch of one has index zero. Every film added would have landed at the
    top of the grid, in reverse order of when it was added, with nothing
    reporting anything: the sync succeeds and the poster appears.
    """

    def films(self, library):
        return [r["title"] for r in library.rows()]

    def test_a_whole_library_files_by_sort_title(self):
        _db, library, _thumbs = real_store("shelf-full")
        library.sync([
            film("a" * 16, "z.mkv", title="Ponyo", sort_title="ponyo"),
            film("b" * 16, "y.mkv", title="The Fifth Element",
                 sort_title="fifth element"),
            film("c" * 16, "x.mkv", title="Hook", sort_title="hook"),
        ])
        # Under F, the way it would be on a shelf -- not under T, and not in
        # the order the batch happened to be in.
        self.assertEqual(self.films(library),
                         ["The Fifth Element", "Hook", "Ponyo"])

    def test_one_film_pushed_on_its_own_does_not_jump_to_the_front(self):
        _db, library, _thumbs = real_store("shelf-one")
        library.sync([
            film("a" * 16, "x.mkv", title="Alpha", sort_title="alpha"),
            film("b" * 16, "y.mkv", title="Zulu", sort_title="zulu"),
        ])
        # Exactly what a finished preparation sends: one film, reconcile off.
        library.sync([film("c" * 16, "z.mkv", title="Mike", sort_title="mike")],
                     reconcile=False)
        self.assertEqual(self.films(library), ["Alpha", "Mike", "Zulu"])

    def test_the_positions_are_a_dense_sequence(self):
        db, library, _thumbs = real_store("shelf-dense")
        library.sync([film("a" * 16, "x.mkv", sort_title="a"),
                      film("b" * 16, "y.mkv", sort_title="b"),
                      film("c" * 16, "z.mkv", sort_title="c")])
        library.sync([film("d" * 16, "w.mkv", sort_title="bb")],
                     reconcile=False)
        positions = [r["pos"] for r in db.query(
            "SELECT pos FROM film ORDER BY pos")]
        self.assertEqual(positions, [0, 1, 2, 3])


class WhatASyncMustNotDestroy(unittest.TestCase):
    """The reason `sync` is an upsert and not a replace.

    Everything here is state the DEVICE has and the machine syncing it does
    not. A sync that deleted and reinserted every film would cascade all of it
    away, and the grid would look perfect afterwards.
    """

    def _seed(self):
        db, library, thumbs = real_store("keep")
        library.sync([film()])
        library.apply_override(FILM, {"title": "Corrected"})
        thumbs.store(FILM, b"\xff\xd8\xffposter")
        db.write().execute(
            "INSERT INTO playback (film_id, position, plays, updated_at) "
            "VALUES (?, ?, ?, ?)", (FILM, 1234.5, 3, 0.0))
        return db, library, thumbs

    def test_a_resync_keeps_the_resume_position(self):
        db, library, _thumbs = self._seed()
        library.sync([film()], reconcile=True)
        row = db.one("SELECT position, plays FROM playback WHERE film_id = ?",
                     (FILM,))
        self.assertEqual((row["position"], row["plays"]), (1234.5, 3))

    def test_a_resync_keeps_an_uploaded_poster(self):
        """A sync sends no image bytes at all -- the admin app uploads a poster
        separately and only when its etag differs. So a sync that dropped the
        row would leave every film showing a placeholder until somebody
        noticed."""
        _db, library, thumbs = self._seed()
        library.sync([film()], reconcile=True)
        self.assertEqual(thumbs.get(FILM)[0], b"\xff\xd8\xffposter")

    def test_a_resync_keeps_a_metadata_edit(self):
        """The whole reason this database exists: the old overlay file lived on
        a read-only mount and lost every edit at restart."""
        _db, library, _thumbs = self._seed()
        library.sync([film()], reconcile=True)
        self.assertEqual(library.get(FILM)["title"], "Corrected")


class Thumbnails(unittest.TestCase):
    def test_bytes_round_trip_with_their_etag(self):
        _db, library, thumbs = real_store("thumb")
        library.sync([film()])
        tag = thumbs.store(FILM, b"\xff\xd8\xffhello")
        data, mime, stored = thumbs.get(FILM)
        self.assertEqual(data, b"\xff\xd8\xffhello")
        self.assertEqual(mime, "image/jpeg")
        self.assertEqual(stored, tag)

    def test_the_etag_follows_the_bytes(self):
        """It is what If-None-Match compares, so a replaced poster that kept
        its etag would stay wrong on every phone until its cache expired."""
        _db, library, thumbs = real_store("etag")
        library.sync([film()])
        first = thumbs.store(FILM, b"\xff\xd8\xffone")
        second = thumbs.store(FILM, b"\xff\xd8\xfftwo")
        self.assertNotEqual(first, second)
        self.assertEqual(thumbs.get(FILM)[0], b"\xff\xd8\xfftwo")

    def test_haves_answers_the_whole_grid_in_one_query(self):
        _db, library, thumbs = real_store("haves")
        library.sync([film(FILM), film(OTHER, "Other/Other.mkv")])
        thumbs.store(FILM, b"\xff\xd8\xffx")
        self.assertEqual(thumbs.haves(), {FILM})

    def test_clearing_forgets_it(self):
        _db, library, thumbs = real_store("clear")
        library.sync([film()])
        thumbs.store(FILM, b"\xff\xd8\xffx")
        self.assertTrue(thumbs.clear(FILM))
        self.assertFalse(thumbs.have(FILM))
        self.assertFalse(thumbs.clear(FILM))

    def test_a_poster_for_an_unknown_film_is_refused(self):
        """Foreign-keyed to `film`, so the table cannot collect posters for
        films that will never be shown."""
        import sqlite3
        _db, _library, thumbs = real_store("orphan")
        with self.assertRaises(sqlite3.IntegrityError):
            thumbs.store(FILM, b"\xff\xd8\xffx")

    def test_deleting_a_film_takes_its_poster_with_it(self):
        _db, library, thumbs = real_store("thumb-cascade")
        library.sync([film()])
        thumbs.store(FILM, b"\xff\xd8\xffx")
        library.sync([], reconcile=True)
        self.assertFalse(thumbs.have(FILM))


class AdminRows(unittest.TestCase):
    """`rows()` -- the one read that hands out stored paths, for the admin app
    to diff against what it sent."""

    def test_it_carries_the_paths_the_merged_view_hides(self):
        _db, library, _thumbs = real_store("rows")
        library.sync([film()])
        row = library.rows()[0]
        self.assertEqual(row["source_rel"], "Ponyo/Ponyo.mkv")
        self.assertEqual(row["media_rel"], "Ponyo/Ponyo.mkv")

    def test_it_reports_the_poster_etag_so_a_resync_can_skip_it(self):
        """What makes a re-sync of an unchanged library one small request
        instead of a hundred JPEGs."""
        _db, library, thumbs = real_store("rows-etag")
        library.sync([film()])
        tag = thumbs.store(FILM, b"\xff\xd8\xffx")
        row = library.rows()[0]
        self.assertEqual(row["thumb_etag"], tag)
        self.assertEqual(row["thumb_source"], "admin")

    def test_it_reports_the_edits_separately_from_the_values(self):
        """The admin app writes these back into the preparer's overrides file.
        It needs to know WHICH fields were edited, not just what the film reads
        as now -- a merged view would push the preparer's own derived title
        back at it as though somebody had chosen it."""
        _db, library, _thumbs = real_store("rows-over")
        library.sync([film()])
        library.apply_override(FILM, {"title": "Corrected"})
        row = library.rows()[0]
        self.assertEqual(row["title"], "Ponyo")
        self.assertEqual(row["overrides"], {"title": "Corrected"})


if __name__ == "__main__":
    unittest.main()
