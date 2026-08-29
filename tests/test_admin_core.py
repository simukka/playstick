"""Tests for the Qt-free core of the admin app: the overrides writer, the
library model's "what is new" scan, the device HTTP client, and the sync that
carries a prepared library onto the device.

These import only the core package, never the views, so no display is needed.

The sync is the piece worth the most attention here. The device does not read
the preparer's index -- it has its own database and the share it would read the
index from is mounted read-only -- so this is the only thing that connects the
machine that prepares films to the machine that plays them. If it silently
sends nothing, everything still looks fine on this side and the grid on the
stick never changes.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src", "admin"))
sys.path.insert(0, os.path.join(ROOT, "src", "prep"))

from playstick_admin.core import overrides as ov          # noqa: E402
from playstick_admin.core import library_model             # noqa: E402
from playstick_admin.core import posters                   # noqa: E402
from playstick_admin.core import sync                      # noqa: E402
from playstick_admin.core.device_client import DeviceClient, DeviceError  # noqa: E402
import playstick_prep as prep                              # noqa: E402


class OverridesWriter(unittest.TestCase):
    def test_write_then_prep_reads_it(self):
        with tempfile.TemporaryDirectory() as d:
            ov.set_entry(d, "abc", source_rel="Hook/Hook.mkv", tmdb_id=2493,
                         title="Hook", year=1991, genres=["Adventure"])
            # The preparer's own reader must understand what the app wrote.
            loaded = prep.load_overrides(d)
            self.assertIn("abc", loaded)
            self.assertEqual(loaded["abc"]["tmdb_id"], 2493)
            self.assertEqual(loaded["abc"]["title"], "Hook")

    def test_empty_values_clear_fields(self):
        with tempfile.TemporaryDirectory() as d:
            ov.set_entry(d, "abc", title="Hook", year=1991)
            ov.set_entry(d, "abc", year=None)          # clear the year
            entry = ov.load(d)["abc"]
            self.assertNotIn("year", entry)
            self.assertEqual(entry["title"], "Hook")

    def test_clear_entry(self):
        with tempfile.TemporaryDirectory() as d:
            ov.set_entry(d, "abc", title="Hook")
            ov.clear_entry(d, "abc")
            self.assertEqual(ov.load(d), {})


class ScanNew(unittest.TestCase):
    def _make_film(self, root, rel):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # A sparse file over the 100 MB floor discover() applies, without
        # actually writing 100 MB to disk.
        with open(path, "wb") as f:
            f.truncate(110 * 1024 * 1024)
        return path

    def test_finds_unprepared_and_skips_known(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_film(d, "Hook (1991)/Hook.mkv")
            self._make_film(d, "Ponyo (2008)/Ponyo.mkv")
            new = library_model.scan_new(d, d)
            rels = {f["rel"] for f in new}
            self.assertEqual(rels, {"Hook (1991)/Hook.mkv", "Ponyo (2008)/Ponyo.mkv"})
            # Mark one as already seen via the prep-state cache.
            state_dir = os.path.join(d, prep.WORK_DIR)
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, prep.STATE_NAME), "w") as f:
                json.dump({"Hook (1991)/Hook.mkv": {}}, f)
            new = library_model.scan_new(d, d)
            rels = {f["rel"] for f in new}
            self.assertEqual(rels, {"Ponyo (2008)/Ponyo.mkv"})

    def test_missing_library_is_empty(self):
        self.assertEqual(library_model.scan_new("/no/such/dir", "/tmp"), [])


# What the stub device below claims to already hold, and what reached it. Class
# attributes because BaseHTTPRequestHandler is instantiated per request; the
# tests reset them in setUp.
class Device:
    films = []          # what GET /api/admin/library answers with
    synced = []         # bodies POSTed to /api/admin/films
    thumbs = []         # (film_id, bytes) PUT to /api/admin/thumb/<id>
    thumb_gets = []     # film ids fetched from /api/thumb/<id>
    # What /api/thumb/<id> hands back. The daemon answers a film with no poster
    # with the placeholder SVG rather than a 404 -- the grid never waits -- so
    # a test can put that here and check it is not mistaken for a poster.
    serves = (b"\xff\xd8\xffposter", "image/jpeg")

    @classmethod
    def reset(cls, films=()):
        cls.films = list(films)
        cls.synced = []
        cls.thumbs = []
        cls.thumb_gets = []
        cls.serves = (b"\xff\xd8\xffposter", "image/jpeg")


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        if self.path == "/api/status":
            self._send({"state": "idle", "title": ""})
        elif self.path == "/api/admin/library":
            self._send({"films": Device.films, "library": "/srv/movies"})
        elif self.path.startswith("/api/thumb/"):
            Device.thumb_gets.append(self.path.rsplit("/", 1)[-1])
            data, kind = Device.serves
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send({})

    def do_POST(self):
        body = json.loads(self._read() or b"{}")
        if self.path == "/api/admin/films":
            Device.synced.append(body)
            return self._send({"written": len(body.get("films") or []),
                               "removed": 0})
        self._send({"ok": True, "echo": body})

    def do_PUT(self):
        data = self._read()
        Device.thumbs.append((self.path.rsplit("/", 1)[-1], data))
        self._send({"id": self.path.rsplit("/", 1)[-1],
                    "etag": sync.poster_etag_of(data), "bytes": len(data)})


# One stub device for the whole module. Per-class would be tidier and is wrong:
# unittest runs classes in alphabetical order, so DeviceClientTests would tear
# the server down before the sync classes below ever reached it.
_SERVER = HTTPServer(("127.0.0.1", 0), _StubHandler)
threading.Thread(target=_SERVER.serve_forever, daemon=True).start()
atexit.register(_SERVER.shutdown)
BASE = "http://%s:%d" % _SERVER.server_address


class DeviceClientTests(unittest.TestCase):
    def setUp(self):
        Device.reset()
        self.base = BASE

    def test_status(self):
        client = DeviceClient(self.base)
        self.assertEqual(client.status()["state"], "idle")

    def test_play_posts_body(self):
        client = DeviceClient(self.base)
        result = client.play("abc123")
        self.assertTrue(result["ok"])
        self.assertEqual(result["echo"], {"id": "abc123"})

    def test_no_url_raises(self):
        with self.assertRaises(DeviceError):
            DeviceClient("").status()

    def test_thumb_url(self):
        client = DeviceClient(self.base)
        self.assertEqual(client.thumb_url("abc"), self.base + "/api/thumb/abc")

    def test_a_poster_goes_up_as_bytes_and_not_as_json(self):
        """base64 would inflate every poster by a third on the link whose
        slowness is the whole reason the etag comparison exists."""
        Device.reset()
        DeviceClient(self.base).put_thumb("abc123", b"\xff\xd8\xffposter")
        self.assertEqual(Device.thumbs, [("abc123", b"\xff\xd8\xffposter")])


class SyncPush(unittest.TestCase):
    """Carrying a prepared library onto the device."""

    def setUp(self):
        Device.reset()
        self.dir = tempfile.mkdtemp(prefix="playstick-sync-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.client = DeviceClient(BASE)

    def write_index(self, movies):
        with open(os.path.join(self.dir, "playstick-library.json"), "w") as f:
            json.dump({"schema": 1, "movies": movies}, f)

    def write_poster(self, rel, data):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_the_index_becomes_the_payload(self):
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "rel": ".playstick/media/x.mp4", "title": "Hook",
                           "year": 1991, "genres": ["Adventure"]}])
        result = sync.push(self.client, self.dir)
        self.assertEqual(result["written"], 1)
        sent = Device.synced[0]["films"][0]
        self.assertEqual(sent["id"], "a" * 16)
        self.assertEqual(sent["title"], "Hook")
        # `rel` is what to PLAY -- the transcode -- and `source_rel` is the
        # original. Swapping them would play the wrong file or none at all.
        self.assertEqual(sent["media_rel"], ".playstick/media/x.mp4")
        self.assertEqual(sent["source_rel"], "Hook/Hook.mkv")

    def test_no_index_is_a_sentence_rather_than_an_empty_sync(self):
        """An empty payload with reconcile set would delete the device's whole
        library because somebody had not run the preparer yet."""
        with self.assertRaises(DeviceError):
            sync.push(self.client, self.dir)
        self.assertEqual(Device.synced, [])

    def test_a_poster_the_device_lacks_is_uploaded(self):
        self.write_poster(".playstick/posters/a.jpg", b"\xff\xd8\xffposter")
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "rel": "Hook/Hook.mkv", "title": "Hook",
                           "poster": ".playstick/posters/a.jpg"}])
        result = sync.push(self.client, self.dir)
        self.assertEqual(result["posters"], 1)
        self.assertEqual(Device.thumbs[0][1], b"\xff\xd8\xffposter")

    def test_a_poster_the_device_already_has_is_skipped(self):
        """The property that makes a re-sync of an unchanged library one small
        request instead of a hundred JPEGs."""
        data = b"\xff\xd8\xffposter"
        self.write_poster(".playstick/posters/a.jpg", data)
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "rel": "Hook/Hook.mkv", "title": "Hook",
                           "poster": ".playstick/posters/a.jpg"}])
        Device.films = [{"id": "a" * 16, "thumb_etag": sync.poster_etag_of(data)}]
        result = sync.push(self.client, self.dir)
        self.assertEqual((result["posters"], result["skipped"]), (0, 1))
        self.assertEqual(Device.thumbs, [])

    def test_a_changed_poster_is_uploaded_again(self):
        """The other half. A poster corrected on this machine that never
        reached the device is a wrong tile that nothing would ever fix."""
        self.write_poster(".playstick/posters/a.jpg", b"\xff\xd8\xffnew")
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "rel": "Hook/Hook.mkv", "title": "Hook",
                           "poster": ".playstick/posters/a.jpg"}])
        Device.films = [{"id": "a" * 16, "thumb_etag": "0" * 16}]
        self.assertEqual(sync.push(self.client, self.dir)["posters"], 1)

    def test_the_etag_matches_what_the_daemon_computes(self):
        """These two are comparing answers across a network. A difference in
        how they are derived would mean every poster uploaded on every sync,
        forever, with nothing reporting anything wrong."""
        sys.path.insert(0, os.path.join(ROOT, "src", "server"))
        from playstick.thumbs import etag_for
        data = b"\xff\xd8\xffsome bytes"
        path = self.write_poster("p.jpg", data)
        self.assertEqual(sync.poster_etag(path), etag_for(data))


class SyncPull(unittest.TestCase):
    """Carrying the device's edits back into the preparer's overrides file."""

    def setUp(self):
        Device.reset()
        self.dir = tempfile.mkdtemp(prefix="playstick-pull-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.client = DeviceClient(BASE)

    def test_an_edit_made_on_the_device_reaches_the_preparer(self):
        """Without this the next prep run derives the old title from the
        filename again, and the correction is lost at the next sync."""
        Device.films = [{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                         "overrides": {"title": "Hook", "year": 1991}}]
        self.assertEqual(sync.pull_overrides(self.client, self.dir), 1)
        entry = ov.load(self.dir)["a" * 16]
        self.assertEqual(entry["title"], "Hook")
        self.assertEqual(entry["year"], 1991)
        # And the preparer's own reader must understand what was written.
        self.assertEqual(prep.load_overrides(self.dir)["a" * 16]["title"], "Hook")

    def test_a_pinned_tmdb_match_is_not_lost(self):
        """tmdb_id is this app's field; the device has no column for it. A pull
        that replaced the entry would throw away a hand-picked match every time
        somebody fixed a title on the stick."""
        ov.set_entry(self.dir, "a" * 16, tmdb_id=2493, title="Old")
        Device.films = [{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                         "overrides": {"title": "New"}}]
        sync.pull_overrides(self.client, self.dir)
        entry = ov.load(self.dir)["a" * 16]
        self.assertEqual(entry["tmdb_id"], 2493)
        self.assertEqual(entry["title"], "New")

    def test_a_field_the_device_has_no_edit_for_is_left_alone(self):
        """"Nobody corrected this there" is not the same as "somebody cleared
        it there", and treating them alike would delete a pin made here."""
        ov.set_entry(self.dir, "a" * 16, plot="A boy who would not grow up.")
        Device.films = [{"id": "a" * 16, "overrides": {"title": "Hook"}}]
        sync.pull_overrides(self.client, self.dir)
        self.assertEqual(ov.load(self.dir)["a" * 16]["plot"],
                         "A boy who would not grow up.")

    def test_a_device_with_no_edits_writes_nothing(self):
        Device.films = [{"id": "a" * 16, "overrides": {}}]
        self.assertEqual(sync.pull_overrides(self.client, self.dir), 0)
        self.assertEqual(ov.load(self.dir), {})


class MergedMovies(unittest.TestCase):
    """Which of the two places a film's fields come from.

    The device holds the film table and the edits; the index on this machine
    holds what only a prep run knows. Getting the precedence wrong here is the
    kind of bug that shows a title somebody fixed weeks ago.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="playstick-merge-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write_index(self, movies):
        with open(os.path.join(self.dir, "playstick-library.json"), "w") as f:
            json.dump({"schema": 1, "movies": movies}, f)

    def test_with_no_device_it_is_the_index_and_the_local_overrides(self):
        """The fallback, and it is not a nicety: preparing a library is most of
        a day and must not require the stick to be plugged in."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook", "year": 1991}])
        ov.set_entry(self.dir, "a" * 16, title="Hook (1991)")
        movies = library_model.merged_movies(self.dir)
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0]["title"], "Hook (1991)")
        self.assertEqual(movies[0]["source"], "index")
        self.assertTrue(movies[0]["has_override"])

    def test_the_device_wins_over_the_index(self):
        """What the stick says a film is called is what a child is looking at.
        An index that still has the old title is the stale one."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Red Hook Summer", "year": 2012}])
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv", "title": "Hook",
             "year": 1991, "overrides": {"title": "Hook"}, "thumb_etag": ""}])
        self.assertEqual(movies[0]["title"], "Hook")
        self.assertEqual(movies[0]["year"], 1991)
        self.assertEqual(movies[0]["source"], "device")

    def test_prep_facts_survive_underneath(self):
        """The device has no column for a verify note or a match score, and a
        merge that dropped them would empty the Notes column."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook", "notes": ["short by 3s"],
                           "tmdb_score": 0.91, "width": 1280}])
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv", "title": "Hook",
             "overrides": {}, "thumb_etag": ""}])
        self.assertEqual(movies[0]["notes"], ["short by 3s"])
        self.assertEqual(movies[0]["tmdb_score"], 0.91)
        self.assertEqual(movies[0]["width"], 1280)

    def test_a_film_the_stick_has_never_heard_of_is_shown_and_marked(self):
        """The single most useful thing this tab can say: it is prepared here
        and it is not over there, which means the sync has not been run."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook"},
                          {"id": "b" * 16, "source_rel": "Ponyo/Ponyo.mkv",
                           "title": "Ponyo"}])
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv", "title": "Hook",
             "overrides": {}, "thumb_etag": ""}])
        by_title = {m["title"]: m for m in movies}
        self.assertTrue(by_title["Ponyo"]["unsynced"])
        self.assertFalse(by_title["Hook"].get("unsynced"))

    def test_the_edited_title_is_the_one_shown(self):
        """/api/admin/library reports the base row and its edits separately --
        deliberately, so this app can tell a chosen title from a derived one --
        which means the join is this module's job.

        Skipping it reads as a film marked "edited" still showing the title the
        edit replaced. That is convincing enough to survive being looked at,
        and it is what this assertion is here to stop.
        """
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Red Hook Summer"}])
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
             "title": "Red Hook Summer", "year": 2012,
             "overrides": {"title": "Hook", "year": 1991}, "thumb_etag": ""}])
        self.assertEqual(movies[0]["title"], "Hook")
        self.assertEqual(movies[0]["year"], 1991)
        # ...and the base is still reachable, because writing the edit back
        # into the preparer's overrides file needs to know which is which.
        self.assertEqual(movies[0]["device_overrides"],
                         {"title": "Hook", "year": 1991})

    def test_an_edit_made_elsewhere_still_reads_as_an_edit(self):
        """has_override comes from the DEVICE's answer, not this machine's
        overrides file -- an edit made from another copy of this app is still
        an edit, and the star in the title column should say so."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook"}])
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv", "title": "Fixed",
             "overrides": {"title": "Fixed"}, "thumb_etag": ""}])
        self.assertTrue(movies[0]["has_override"])
        self.assertEqual(movies[0]["device_overrides"], {"title": "Fixed"})

    def test_a_pinned_tmdb_match_comes_from_the_local_file(self):
        """The device stores tmdb_id as a plain column from the sync and has no
        notion of one having been CHOSEN. That distinction is this app's, so it
        has to survive being shown device rows."""
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook"}])
        ov.set_entry(self.dir, "a" * 16, tmdb_id=2493)
        movies = library_model.merged_movies(self.dir, [
            {"id": "a" * 16, "source_rel": "Hook/Hook.mkv", "title": "Hook",
             "overrides": {}, "thumb_etag": ""}])
        self.assertEqual(movies[0]["override_tmdb_id"], 2493)

    def test_an_empty_device_with_a_prepared_index_shows_everything_unsynced(self):
        self.write_index([{"id": "a" * 16, "source_rel": "Hook/Hook.mkv",
                           "title": "Hook"}])
        movies = library_model.merged_movies(self.dir, [])
        self.assertEqual(len(movies), 1)
        self.assertTrue(movies[0]["unsynced"])


class PosterCache(unittest.TestCase):
    """Posters fetched off the device, kept by etag."""

    def setUp(self):
        Device.reset()
        self.dir = tempfile.mkdtemp(prefix="playstick-cache-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # posters.cache_dir() reads the environment on every call, so pointing
        # it at scratch needs no patching of the module.
        old = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self.dir
        self.addCleanup(lambda: os.environ.__setitem__("XDG_CACHE_HOME", old)
                        if old is not None
                        else os.environ.pop("XDG_CACHE_HOME", None))
        self.client = DeviceClient(BASE)

    def test_a_poster_is_fetched_once_and_then_served_from_disk(self):
        """The point of the whole arrangement: the second time the tab is
        opened it makes no requests at all."""
        path = posters.fetch(self.client, "a" * 16, "etag1")
        self.assertEqual(open(path, "rb").read(), b"\xff\xd8\xffposter")
        self.assertEqual(Device.thumb_gets, ["a" * 16])
        again = posters.fetch(self.client, "a" * 16, "etag1")
        self.assertEqual(again, path)
        self.assertEqual(Device.thumb_gets, ["a" * 16])       # not asked twice

    def test_a_changed_poster_has_a_different_key(self):
        """Keyed by etag rather than film id, so nothing has to be invalidated
        and a stale file cannot be served."""
        first = posters.fetch(self.client, "a" * 16, "etag1")
        Device.serves = (b"\xff\xd8\xffdifferent", "image/jpeg")
        second = posters.fetch(self.client, "a" * 16, "etag2")
        self.assertNotEqual(first, second)
        self.assertEqual(open(second, "rb").read(), b"\xff\xd8\xffdifferent")

    def test_the_placeholder_is_not_mistaken_for_a_poster(self):
        """A film with no poster gets the daemon's SVG rather than a 404 -- the
        grid never waits. An SVG is not a poster and must not be cached as one,
        or the row would show a grey letter forever."""
        Device.serves = (b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                         "image/svg+xml")
        self.assertIsNone(posters.fetch(self.client, "a" * 16, "etag1"))

    def test_no_etag_is_never_fetched(self):
        """A film the device says it has no poster for. Asking anyway would be
        one wasted request per unpostered film on every refresh."""
        self.assertIsNone(posters.cached(""))
        self.assertEqual(Device.thumb_gets, [])

    def test_sweep_leaves_a_small_cache_alone(self):
        """Deleting one still in use costs a refetch; keeping a stale one costs
        a few kilobytes. Below the limit the trade is obvious."""
        posters.fetch(self.client, "a" * 16, "etag1")
        self.assertEqual(posters.sweep(set()), 0)
        self.assertIsNotNone(posters.cached("etag1"))

    def test_sweep_drops_what_nothing_refers_to_once_it_is_large(self):
        for i in range(5):
            posters.store("etag%d" % i, b"\xff\xd8\xffx")
        self.assertEqual(posters.sweep({"etag0"}, limit=2), 4)
        self.assertIsNotNone(posters.cached("etag0"))
        self.assertIsNone(posters.cached("etag3"))

    def test_a_missing_cache_directory_is_not_an_error(self):
        """sweep runs on every reload, including the first one ever."""
        self.assertEqual(posters.sweep({"a"}), 0)


if __name__ == "__main__":
    unittest.main()
