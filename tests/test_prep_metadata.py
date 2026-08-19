"""Tests for how scripts/playstick-prep.py decides what a film IS.

Written after the shelf a child picks from offered "Red Hook Summer" -- its
poster, its plot, its rating -- for a file called Hook.1991.720p.BRrip.x264.mp4.
Thirty-three of the hundred and seventeen entries in that index were somebody
else's film, and none of it looked like a failure at any point: every step
succeeded and returned a plausible answer.

So the cases below are the real ones, under their own names, with the numbers
that were actually on the NAS. A metadata bug does not announce itself, and the
only defence is a test that knows what the right answer was.

No network: Tmdb._get is replaced with recorded response shapes. Independent of
tests/support.py -- nothing here needs the daemon, its environment, or a socket.
"""

import argparse
import importlib.util
import os
import re
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "playstick-prep.py")
DAEMON = os.path.join(ROOT, "src", "server", "playstick", "library.py")

# The hyphen in the filename keeps it out of `import`, which is the right name
# for a command and the wrong one for a module.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("playstick_prep", SCRIPT)
prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prep)


def tags(**kv):
    """An ffprobe result carrying only container tags."""
    return {"format": {"tags": dict(kv)}}


def args(**kv):
    defaults = {"prefer_metadata_titles": False, "refresh_posters": False}
    defaults.update(kv)
    return argparse.Namespace(**defaults)


def film(id, title, year, runtime=None, votes=500, overview="", original=None):
    """One TMDb search result, in the shape the API returns it."""
    return {
        "id": id,
        "title": title,
        "original_title": original or title,
        "release_date": "%d-01-01" % year if year else "",
        "vote_average": 7.0,
        "vote_count": votes,
        "overview": overview,
        "genre_ids": [],
        "runtime": runtime,
    }


class Recorded(prep.Tmdb):
    """A Tmdb that answers from a script instead of the network.

    `results` maps (query, year) to a result list; anything not listed is an
    empty search, which is what TMDb really returns for a year that is wrong.
    """

    def __init__(self, results, runtimes=None):
        prep.Tmdb.__init__(self, "KEY", None)
        self.results = results
        self.runtimes = runtimes or {}
        self.requests = []

    def search(self, query, year):
        self.requests.append((query, year))
        return list(self.results.get((query, year), []))

    def _get(self, path, params):
        if path.startswith("/movie/"):
            movie_id = int(path.rsplit("/", 1)[1])
            runtime = self.runtimes.get(movie_id)
            return {"id": movie_id, "genres": [],
                    "runtime": runtime} if runtime else {"id": movie_id}
        return None


# --- the year ---------------------------------------------------------------

class Years(unittest.TestCase):

    def test_creation_time_is_not_a_release_year(self):
        # The whole bug in one assertion. This mp4 has no other date on it:
        # 2012-10-08 is when YIFY muxed a film from 1991.
        info = tags(creation_time="2012-10-08T04:34:03.000000Z")
        self.assertNotIn("year", prep.metadata_from_tags(info))

    def test_a_real_date_tag_is_still_a_year(self):
        self.assertEqual(prep.metadata_from_tags(tags(date="1991"))["year"], 1991)
        self.assertEqual(prep.metadata_from_tags(tags(year="1991"))["year"], 1991)
        self.assertEqual(
            prep.metadata_from_tags(tags(originalyear="1991-05-02"))["year"], 1991)

    def test_a_tag_year_does_not_overwrite_the_filename(self):
        movie = {"title": "Hook", "year": 1991}
        prep.apply_metadata(movie, {"year": 2012, "metadata_source": "tags"}, args())
        self.assertEqual(movie["year"], 1991)

    def test_a_tag_year_still_fills_a_gap(self):
        movie = {"title": "Hook"}
        prep.apply_metadata(movie, {"year": 1991, "metadata_source": "tags"}, args())
        self.assertEqual(movie["year"], 1991)

    def test_an_nfo_year_still_wins(self):
        # Curated by a human or by Kodi, which is the case "later sources win"
        # was written for and the one that keeps working.
        movie = {"title": "Hook", "year": 1990}
        prep.apply_metadata(movie, {"year": 1991, "metadata_source": "nfo"}, args())
        self.assertEqual(movie["year"], 1991)

    def test_the_folder_supplies_a_year_the_filename_lacks(self):
        rel = "Die.Hard.1988.1080p.bdrip.x265.5.1.AAC-FINKLEROY/die_hard.mkv"
        self.assertIsNone(prep.guess_year(os.path.basename(rel)))
        self.assertEqual(prep.year_from_path(rel), 1988)

    def test_the_filename_beats_the_folder(self):
        # A box set is the shape that makes "just use the folder" wrong.
        self.assertEqual(
            prep.year_from_path("The.Complete.Matrix.Trilogy.1080p/The.Matrix.1999.mkv"),
            1999)

    def test_a_season_folder_supplies_nothing(self):
        self.assertIsNone(prep.year_from_path("Detectorists/Season 2/03.mkv"))

    def test_a_title_that_is_a_number_is_not_a_date(self):
        self.assertFalse(prep.plausible_year(2049))     # Blade Runner
        self.assertTrue(prep.plausible_year(1917))
        self.assertFalse(prep.plausible_year(1850))


# --- what to ask TMDb for ---------------------------------------------------

class SearchTitles(unittest.TestCase):

    def test_the_edition_is_stripped_for_the_search_only(self):
        # TMDb has one entry for Alien and none for that cut of it. Asking for
        # the edition is how the old code came back with Aliens.
        self.assertEqual(prep.search_title("Alien Director's Cut"), "Alien")
        self.assertEqual(prep.search_title("Deep Cover 1992 Remastered"), "Deep Cover")
        self.assertEqual(prep.search_title("The Abyss - Theatrical Cut"), "The Abyss")

    def test_a_half_open_bracket_is_closed(self):
        # What a truncating TAG_RE leaves behind.
        self.assertEqual(prep.search_title("Sphere (1998"), "Sphere")
        self.assertEqual(prep.search_title("Valkyrie (2008"), "Valkyrie")

    def test_a_year_that_is_the_title_survives(self):
        self.assertEqual(prep.search_title("2001 A Space Odyssey"),
                         "2001 A Space Odyssey")
        self.assertEqual(prep.search_title("1917"), "1917")
        self.assertEqual(prep.search_title("Blade Runner 2049"), "Blade Runner 2049")

    def test_an_ordinary_title_is_left_alone(self):
        for title in ("Hook", "American Psycho", "The Emperor's New Groove",
                      "Star Wars Episode 4 A New Hope", "Dune Part Two",
                      "Pirates of the Caribbean Curse of the Black Pearl"):
            self.assertEqual(prep.search_title(title), title)

    def test_clean_title_is_untouched_by_any_of_it(self):
        # search_title exists precisely so that clean_title does not have to
        # change: it sets what a child sees, and the shelf should keep saying
        # "Alien Director's Cut" because that is what the file is.
        self.assertEqual(prep.clean_title("Alien Director's Cut 1979.720p.BrRip.mp4"),
                         "Alien Director's Cut")

    def test_clean_title_still_matches_the_daemons_copy(self):
        """The comment above TAG_RE says these two are kept byte-for-byte in
        step. This is that comment, enforced."""
        with open(DAEMON, "r", encoding="utf-8") as handle:
            theirs = handle.read()
        with open(SCRIPT, "r", encoding="utf-8") as handle:
            ours = handle.read()

        def body(text):
            match = re.search(r"def clean_title\(filename\):(.*?)\n\n\n", text,
                              re.DOTALL)
            self.assertIsNotNone(match, "clean_title not found")
            # The docstrings differ on purpose; the code must not.
            lines = [l.strip() for l in match.group(1).splitlines()
                     if l.strip() and not l.strip().startswith(('"', "#"))]
            return lines

        self.assertEqual(body(ours), body(theirs))


# --- what a container tag is allowed to rename a film to --------------------

class TagTitles(unittest.TestCase):

    # Every one of these was sitting in a real file in the library.
    JUNK = [
        "Boss.Level.2020.1080p.WEB-DL.DD5.1.H264-FGT",
        "Contact - YIFY",
        "Heat - YIFY",
        "Enemy of the State 1998 1080p Blu-ray Remux MPEG-2 DTS-HD MA 5.1-carlbob",
        "Jackie Brown (1997) 1080p H265 ita eng AC3 5.1 sub ita NUita-Licdom",
        "Re-Encode by Bsgr13. Enjoy with SPIDERMAN 3 2007 !",
        "Street Kings (2008) 1080p-H264-AC 3 (DTS 5.1) Remastered & nickarad",
        "The Girl with the Dragon Tattoo 2011 1080p Blu-ray Remux AVC DTS-HD MA 5.1",
        "The Recruit (2003)",
        "Valkyrie (2008)",
        "Abyss, The (1989, Ext, UHDRip)",
    ]
    REAL = [
        "2001: A Space Odyssey", "1917", "Blade Runner 2049", "WALL-E",
        "THX 1138", "The Emperor's New Groove", "Home Alone", "Se7en",
        "Star Wars: Episode IV - A New Hope",
    ]

    def test_release_strings_are_recognised(self):
        for title in self.JUNK:
            self.assertTrue(prep.title_is_release_string(title), title)

    def test_real_titles_are_not(self):
        for title in self.REAL:
            self.assertFalse(prep.title_is_release_string(title), title)

    def test_a_release_string_does_not_become_the_title(self):
        # The old rule was "the longer string wins", and this one is longer.
        movie = {"title": "Spiderman 3"}
        prep.apply_metadata(movie, {
            "title": "Re-Encode by Bsgr13. Enjoy with SPIDERMAN 3 2007 !",
            "metadata_source": "tags"}, args())
        self.assertEqual(movie["title"], "Spiderman 3")

    def test_a_genuinely_better_tag_title_still_wins(self):
        movie = {"title": "Contac"}
        prep.apply_metadata(movie, {"title": "Contact",
                                    "metadata_source": "tags"}, args())
        self.assertEqual(movie["title"], "Contact")

    def test_prefer_metadata_titles_remains_the_escape_hatch(self):
        movie = {"title": "Spiderman 3"}
        prep.apply_metadata(movie, {"title": "Heat - YIFY",
                                    "metadata_source": "tags"},
                            args(prefer_metadata_titles=True))
        self.assertEqual(movie["title"], "Heat - YIFY")

    def test_a_verified_tmdb_title_wins_even_when_shorter(self):
        movie = {"title": "Star Wars Episode 4 A New Hope"}
        prep.apply_metadata(movie, {"title": "Star Wars",
                                    "metadata_source": "tmdb"}, args())
        self.assertEqual(movie["title"], "Star Wars")


# --- scoring one title against another --------------------------------------

class TitleScores(unittest.TestCase):

    def test_the_same_film_scores_full_marks(self):
        self.assertEqual(prep.title_match_score("Hook", "Hook"), 100)
        self.assertEqual(prep.title_match_score("The Break Up", "The Break-Up"), 100)
        self.assertEqual(prep.title_match_score("fifth.element,.the",
                                                "The Fifth Element"), 100)

    def test_a_coincidental_word_scores_far_below_a_real_prefix(self):
        # The distinction the whole thing turns on: "Star Wars" really is the
        # start of that title, and "Hook" is just a word inside this one.
        real = prep.title_match_score("Star Wars Episode 4 A New Hope", "Star Wars")
        chance = prep.title_match_score("Hook", "Red Hook Summer")
        self.assertGreater(real, chance + 20)
        self.assertLess(chance, 50)

    def test_near_misses_that_must_still_match(self):
        for want, got in [
                ("Harry Potter and the Sorcerers Stone",
                 "Harry Potter and the Philosopher's Stone"),
                ("Guy Ritchies The Covenant", "Guy Ritchie's The Covenant"),
                ("Contac", "Contact"),
                ("Pirates of the Caribbean Curse of the Black Pearl",
                 "Pirates of the Caribbean: The Curse of the Black Pearl"),
                ("Indiana Jones And The Raiders Of The Lost Ark",
                 "Raiders of the Lost Ark")]:
            self.assertGreaterEqual(prep.title_match_score(want, got), 60,
                                    "%s / %s" % (want, got))

    def test_different_films_that_share_a_word(self):
        for want, got in [("Soldier", "Tinker Tailor Soldier Spy"),
                          ("Dogma", "Tina Modotti: Dogma and Passion"),
                          ("Die Hard", "Don't Die Too Hard!")]:
            self.assertLess(prep.title_match_score(want, got), 70,
                            "%s / %s" % (want, got))


# --- the lookup -------------------------------------------------------------

HOOK = film(879, "Hook", 1991, votes=4000)
RED_HOOK = film(84328, "Red Hook Summer", 2012, votes=100)
PLAY_HOOKY = film(294979, "Play Hooky", 2012, votes=5)

HOOK_SECONDS = 8505          # what ffprobe measured on the real file
HOOK_RUNTIME = 142


class Lookup(unittest.TestCase):

    def test_hook_is_hook(self):
        api = Recorded({("Hook", 1991): [HOOK]}, {879: HOOK_RUNTIME})
        meta, why = api.lookup("Hook", 1991, HOOK_SECONDS)
        self.assertIsNone(why)
        self.assertEqual(meta["tmdb_id"], 879)
        self.assertEqual(meta["title"], "Hook")
        self.assertEqual(meta["year"], 1991)

    def test_the_recorded_wrong_answer_is_refused(self):
        """The regression, replayed exactly.

        This is the result set TMDb really returned for the request the old
        code really made, and the point of the assertion is that the search
        having been asked the wrong question is survivable: nothing here is
        confident enough to put on a shelf, so nothing goes on it.
        """
        api = Recorded({("Hook", 2012): [RED_HOOK, PLAY_HOOKY]},
                       {84328: 121, 294979: 90})
        meta, why = api.lookup("Hook", 2012, HOOK_SECONDS)
        self.assertEqual(meta, {})
        self.assertIn("best was", why)

    def test_a_wrong_year_is_survivable_when_the_film_is_findable(self):
        # No results under the bad year, so the unfiltered search decides --
        # and the exact title beats the popular stranger.
        api = Recorded({("Hook", None): [RED_HOOK, HOOK]}, {879: HOOK_RUNTIME})
        meta, why = api.lookup("Hook", 2012, HOOK_SECONDS)
        self.assertIsNone(why)
        self.assertEqual(meta["tmdb_id"], 879)

    def test_a_year_that_agrees_stops_the_second_search(self):
        api = Recorded({("Hook", 1991): [HOOK]}, {879: HOOK_RUNTIME})
        api.lookup("Hook", 1991, HOOK_SECONDS)
        self.assertEqual(api.requests, [("Hook", 1991)])

    def test_a_year_that_finds_nothing_by_name_tries_again_without_it(self):
        api = Recorded({("Hook", 1991): [PLAY_HOOKY], ("Hook", None): [HOOK]},
                       {879: HOOK_RUNTIME})
        meta, why = api.lookup("Hook", 1991, HOOK_SECONDS)
        self.assertEqual(api.requests, [("Hook", 1991), ("Hook", None)])
        self.assertEqual(meta["tmdb_id"], 879)

    def test_the_runtime_settles_a_tie(self):
        # Two films of the same name and year; only one is 142 minutes long.
        short = film(1, "Hook", 1991, votes=4000)
        api = Recorded({("Hook", 1991): [short, film(2, "Hook", 1991, votes=10)]},
                       {1: 95, 2: HOOK_RUNTIME})
        meta, _ = api.lookup("Hook", 1991, HOOK_SECONDS)
        self.assertEqual(meta["tmdb_id"], 2)

    def test_an_extended_cut_is_not_thrown_away(self):
        # The Abyss on this shelf runs half an hour over what TMDb lists. An
        # exact title and an exact year say "same film, longer cut", and the
        # runtime must not be allowed to overrule both.
        api = Recorded({("The Abyss", 1989): [film(9, "The Abyss", 1989)]},
                       {9: 140})
        meta, why = api.lookup("The Abyss", 1989, 171 * 60)
        self.assertIsNone(why)
        self.assertEqual(meta["tmdb_id"], 9)

    def test_a_wrong_runtime_disqualifies_an_unconfident_match(self):
        api = Recorded({("Hook", None): [RED_HOOK]}, {84328: 121})
        meta, why = api.lookup("Hook", None, HOOK_SECONDS)
        self.assertEqual(meta, {})

    def test_nothing_at_all_is_reported_rather_than_guessed(self):
        meta, why = Recorded({}).lookup("A Film That Does Not Exist", 1999)
        self.assertEqual(meta, {})
        self.assertIn("nothing", why)

    def test_popularity_cannot_outvote_the_title(self):
        # Red Hook Summer with every vote on TMDb still loses to Hook, because
        # popularity is worth two points and being the right film is worth a
        # hundred. Sorting by popularity is what caused all of this.
        loud = dict(RED_HOOK, vote_count=999999)
        api = Recorded({("Hook", None): [loud, HOOK]}, {879: HOOK_RUNTIME})
        meta, _ = api.lookup("Hook", None, HOOK_SECONDS)
        self.assertEqual(meta["tmdb_id"], 879)

    def test_the_real_near_misses_are_accepted(self):
        cases = [
            ("Harry Potter and the Sorcerers Stone", 2001,
             film(671, "Harry Potter and the Philosopher's Stone", 2001), 152),
            ("Indiana Jones And The Raiders Of The Lost Ark", 1981,
             film(85, "Raiders of the Lost Ark", 1981), 115),
            ("Pirates of the Caribbean Curse of the Black Pearl", 2003,
             film(22, "Pirates of the Caribbean: The Curse of the Black Pearl",
                  2003), 143),
            ("Star Wars Episode 4 A New Hope", 1977, film(11, "Star Wars", 1977), 121),
            ("Contac", 1997, film(686, "Contact", 1997), 150),
        ]
        for title, year, candidate, runtime in cases:
            api = Recorded({(prep.search_title(title), year): [candidate]},
                           {candidate["id"]: runtime})
            meta, why = api.lookup(title, year, runtime * 60)
            self.assertIsNone(why, "%s -> %s" % (title, why))
            self.assertEqual(meta["tmdb_id"], candidate["id"], title)

    def test_the_real_wrong_answers_are_refused(self):
        # Every row is a match the old code made, with the year it searched
        # under, the candidate it accepted and that candidate's real runtime
        # against the real length of the file on the NAS.
        cases = [
            # title, searched year, wrong candidate, its runtime, file minutes
            ("Die Hard", 2025, film(15449, "Don't Die Too Hard!", 2001), 90, 132),
            ("Iron Man", 2012, film(41428, "Tetsuo: The Iron Man", 1989), 67, 126),
            ("Soldier", 2011, film(49517, "Tinker Tailor Soldier Spy", 2011), 127, 99),
            ("Dogma", 2013, film(215071, "Tina Modotti: Dogma and Passion", 2013),
             60, 130),
            ("Munich", 2015, film(321779, "Lost in Munich", 2015), 105, 164),
            ("The Break Up", 2012, film(674734, "The Break-Up Tour", 2012), 80, 106),
            ("Jumanji", None, film(1260649, "Jumanji: Open World", 2026), 100, 104),
            ("Home Alone", None, film(772, "Home Alone 2: Lost in New York", 1992),
             120, 103),
        ]
        for title, year, candidate, runtime, minutes in cases:
            api = Recorded({(title, year): [candidate], (title, None): [candidate]},
                           {candidate["id"]: runtime})
            meta, why = api.lookup(title, year, minutes * 60)
            self.assertEqual(meta, {},
                             "%s accepted %s" % (title, meta.get("title")))
            self.assertTrue(why)

    def test_an_accepted_match_carries_its_score(self):
        api = Recorded({("Hook", 1991): [HOOK]}, {879: HOOK_RUNTIME})
        meta, _ = api.lookup("Hook", 1991, HOOK_SECONDS)
        self.assertGreaterEqual(meta["tmdb_score"], prep.Tmdb.ACCEPT)


class Reporting(unittest.TestCase):

    def test_the_index_carries_the_tmdb_id(self):
        # Without it a wrong match cannot be traced back to the page it came
        # from, which is how this took a while to find.
        movie = {"id": "abc", "title": "Hook", "sort_title": "hook",
                 "source_rel": "Hook (1991)/Hook.mp4", "tmdb_id": 879,
                 "tmdb_score": 155}
        index = prep.build_index([movie], [], args(), {"library": "/l", "output": "/l"})
        entry = index["movies"][0]
        self.assertEqual(entry["tmdb_id"], 879)
        self.assertEqual(entry["tmdb_score"], 155)


class StateCache(unittest.TestCase):

    def test_the_cache_version_is_stamped_and_checked(self):
        """A fix to these rules is worthless if the library it is meant to fix
        never re-derives anything. An entry written before the fix must not
        satisfy the cache."""
        with open(SCRIPT, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"meta_version": META_VERSION', source)
        self.assertIn('cached.get("meta_version") == META_VERSION', source)
        self.assertGreaterEqual(prep.META_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
