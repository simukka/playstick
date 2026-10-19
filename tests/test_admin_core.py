"""The admin app's core against a real HTTP server: the client, and the push.

These import only the core package, never the views, so no display is needed.
What they add over the other admin test files is a socket -- `DeviceClient` and
the daemon's routes really do speak to each other here, over a stub server, so
a request shape that is wrong fails as a request rather than passing against a
convenient fake.

The push is the piece worth the most attention. The stick reads nothing but
films off the share; everything it knows about them arrives through these calls.
If they silently send nothing, everything still looks fine on this side and the
grid in the other room never changes.
"""

import atexit
import json
import logging
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

from playstick_admin.core import catalog                    # noqa: E402
from playstick_admin.core import device_library             # noqa: E402
from playstick_admin.core import device_state                # noqa: E402
from playstick_admin import log as log_setup                 # noqa: E402
from playstick_admin.core import posters                    # noqa: E402
from playstick_admin.core import prepared                   # noqa: E402
from playstick_admin.core import sync                       # noqa: E402
from playstick_admin.core.db import Database                # noqa: E402
from playstick_admin.core.device_client import DeviceClient, DeviceError  # noqa: E402
from playstick_admin.core import device_client               # noqa: E402
import playstick_prep as prep                               # noqa: E402


class Device:
    films = []          # what GET /api/admin/library answers with
    synced = []         # bodies POSTed to /api/admin/films
    deleted = []        # bodies POSTed to /api/admin/films/delete
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
        cls.deleted = []
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
        if self.path == "/api/admin/films/delete":
            Device.deleted.append(body)
            return self._send({"removed": len(body.get("ids") or [])})
        self._send({"ok": True, "echo": body})

    def do_PUT(self):
        data = self._read()
        Device.thumbs.append((self.path.rsplit("/", 1)[-1], data))
        self._send({"id": self.path.rsplit("/", 1)[-1],
                    "etag": prepared.etag_of(data), "bytes": len(data)})


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


class Pushing(unittest.TestCase):
    """One film, over a real socket, in the shape the daemon validates.

    The payload is built from the admin database and handed through
    prep.device_film, so what these check is the whole path from a row here to a
    request the stick would accept -- including the part where every path in it
    has to be relative and resolve under the stick's own library root.
    """

    def setUp(self):
        Device.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "admin.db"))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self.client = DeviceClient(BASE)
        self.ident = "0123456789abcdef"
        self._add(self.ident)

    def _add(self, ident, title="Ponyo", poster=b"\xff\xd8\xffposter"):
        self.db.write().execute(
            "INSERT INTO item (id, rel, abs_path, seen_at) VALUES (?,?,?,0)",
            (ident, "%s.mkv" % title, "/x/%s.mkv" % title))
        prepared.record(
            self.db, ident,
            {"title": title, "sort_title": title.lower(), "year": 2008,
             "rating": 8.0, "duration": 6060.0, "width": 1280, "height": 720,
             "vcodec": "h264", "acodec": "aac", "tmdb_id": 12429,
             "genres": ["Animation", "Family"],
             "subtitles": [{"rel": ".playstick/subs/%s.eng.srt" % ident,
                            "lang": "eng"}],
             "phone_audio": [{"n": 1, "rel": ".playstick/audio/%s/1.eng.m4a" % ident,
                              "lang": "eng", "title": "", "channels": 2,
                              "default": True, "offset": 0.25}]},
            ".playstick/media/%s.mp4" % ident, "%s.mkv" % title,
            poster=(poster, "image/jpeg", "tmdb") if poster else None)

    def test_the_payload_carries_the_children(self):
        sync.push_one(self.client, self.db, self.ident)
        sent = Device.synced[0]["films"][0]
        self.assertEqual(sent["genres"], ["Animation", "Family"])
        self.assertEqual(sent["subtitles"][0]["lang"], "eng")
        self.assertEqual(sent["audio"][0]["offset"], 0.25)
        self.assertTrue(sent["audio"][0]["default"])
        # Text on the way out of the database, coerced back by the daemon.
        self.assertEqual(sent["rating"], "8.0")

    def test_every_path_is_relative_and_stays_under_the_root(self):
        # The daemon re-validates all of these and refuses the whole request on
        # one bad path, so a payload that fails here is a library that will not
        # sync at all.
        sync.push_one(self.client, self.db, self.ident)
        sent = Device.synced[0]["films"][0]
        paths = ([sent["source_rel"], sent["media_rel"]]
                 + [x["rel"] for x in sent["subtitles"] + sent["audio"]])
        for path in paths:
            self.assertFalse(os.path.isabs(path), path)
            self.assertNotIn("..", path.split("/"), path)

    def test_one_film_never_reconciles(self):
        """reconcile deletes what a batch does not mention. On a batch of one
        that is the whole library."""
        sync.push_one(self.client, self.db, self.ident)
        self.assertFalse(Device.synced[0]["reconcile"])

    def test_the_poster_follows_the_film_and_never_precedes_it(self):
        # A poster is foreign-keyed to its film on the device, so uploading one
        # for a film it has not been told about is a 404 rather than a row.
        sync.push_one(self.client, self.db, self.ident)
        self.assertEqual(len(Device.synced), 1)
        self.assertEqual(Device.thumbs[0][0], self.ident)

    def test_a_poster_the_stick_already_has_is_not_sent_again(self):
        # On a RE-push. The device names each poster by sha1(bytes)[:16] and so
        # does this side, so an unchanged one is a comparison and no image data.
        data = b"\xff\xd8\xffposter"
        prepared.mark_pushed(self.db, self.ident)
        Device.films = [{"id": self.ident,
                         "thumb_etag": prepared.etag_of(data)}]
        sync.push_one(self.client, self.db, self.ident)
        self.assertEqual(Device.thumbs, [])

    def test_a_changed_poster_is_sent_on_a_re_push(self):
        prepared.mark_pushed(self.db, self.ident)
        Device.films = [{"id": self.ident, "thumb_etag": "somethingelse"}]
        sync.push_one(self.client, self.db, self.ident)
        self.assertEqual(Device.thumbs[0][0], self.ident)

    def test_a_first_push_does_not_ask_what_posters_the_stick_has(self):
        """There is no per-film route for that -- asking means fetching the
        whole film table. Doing it once per film in a queue of two hundred
        would be two hundred five-hundred-row payloads, every one of them to be
        told the stick has no poster for a film it heard about a second ago."""
        asked = []
        real = self.client.admin_library
        self.client.admin_library = lambda: (asked.append(1), real())[1]
        sync.push_one(self.client, self.db, self.ident)
        self.assertEqual(asked, [])
        self.assertEqual(Device.thumbs[0][0], self.ident)

    def test_a_pushed_film_stops_being_pending(self):
        self.assertEqual(len(prepared.unpushed(self.db)), 1)
        sync.push_one(self.client, self.db, self.ident)
        self.assertEqual(prepared.unpushed(self.db), [])

    def test_push_pending_sends_only_what_is_waiting(self):
        other = "fedcba9876543210"
        self._add(other, title="Totoro")
        prepared.mark_pushed(self.db, self.ident)
        result = sync.push_pending(self.client, self.db)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(Device.synced[0]["films"][0]["id"], other)

    def test_re_sending_everything_reconciles(self):
        self._add("fedcba9876543210", title="Totoro")
        result = sync.push_all(self.client, self.db, reconcile=True)
        self.assertTrue(Device.synced[0]["reconcile"])
        self.assertEqual(result["written"], 2)
        # In shelf order, so the daemon's own renumbering agrees with what this
        # machine thinks the order is.
        titles = [f["title"] for f in Device.synced[0]["films"]]
        self.assertEqual(titles, sorted(titles))

    def test_removing_a_film_is_targeted_and_not_a_reconcile(self):
        # Phrasing it as "everything except these" would also delete every film
        # the stick has that this machine does not know about.
        sync.remove_one(self.client, self.db, self.ident)
        self.assertEqual(Device.deleted, [{"ids": [self.ident]}])
        self.assertEqual(Device.synced, [])

    def test_a_film_that_was_never_prepared_here_is_refused(self):
        with self.assertRaises(DeviceError):
            sync.push_one(self.client, self.db, "a" * 16)


class PullingEditsBack(unittest.TestCase):
    """What somebody corrected from the sofa, brought here before a
    preparation overwrites it.

    Without this a preparation derives the title from the filename again and
    pushes it back over the correction -- the edit survives on the stick only
    until the next push, which is exactly the failure the old two-file
    arrangement had.
    """

    def setUp(self):
        Device.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Database(os.path.join(self._tmp.name, "admin.db"))
        self.db.migrate()
        self.addCleanup(self.db.close)
        self.client = DeviceClient(BASE)
        self.ident = "0123456789abcdef"
        self.db.write().execute(
            "INSERT INTO item (id, rel, abs_path, seen_at) VALUES (?,?,?,0)",
            (self.ident, "Ponyo.mkv", "/x/Ponyo.mkv"))

    def test_an_edit_on_the_stick_lands_here(self):
        Device.films = [{"id": self.ident, "title": "Ponyo",
                         "overrides": {"title": "Ponyo on the Cliff",
                                       "year": 2008}}]
        self.assertEqual(sync.pull_overrides(self.client, self.db), 1)
        entry = prepared.overrides(self.db, self.ident)
        self.assertEqual(entry["title"], "Ponyo on the Cliff")

    def test_a_pull_does_not_lose_a_hand_picked_match(self):
        # tmdb_id is this app's own idea. The device has no column for it, so a
        # pull that replaced the entry would silently unpin every match anybody
        # had chosen by hand.
        prepared.set_override(self.db, self.ident, tmdb_id=12429)
        Device.films = [{"id": self.ident, "overrides": {"title": "Ponyo"}}]
        sync.pull_overrides(self.client, self.db)
        entry = prepared.overrides(self.db, self.ident)
        self.assertEqual(entry["tmdb_id"], 12429)
        self.assertEqual(entry["title"], "Ponyo")

    def test_a_film_with_no_edits_is_left_alone(self):
        Device.films = [{"id": self.ident, "title": "Ponyo", "overrides": {}}]
        self.assertEqual(sync.pull_overrides(self.client, self.db), 0)

    def test_pulling_twice_reports_no_second_change(self):
        Device.films = [{"id": self.ident, "overrides": {"title": "Ponyo"}}]
        self.assertEqual(sync.pull_overrides(self.client, self.db), 1)
        self.assertEqual(sync.pull_overrides(self.client, self.db), 0)


class WhatTheLibraryTabShows(unittest.TestCase):
    """The device's rows joined to their edits, and to what only a preparation
    knows."""

    def test_an_edited_film_shows_the_edit_and_not_what_it_replaced(self):
        # /api/admin/library reports the base row and its edits separately, so
        # the join is this side's job. Skipping it reads as a film marked
        # "edited" still showing the title the edit replaced -- convincing
        # enough to survive being looked at.
        rows = device_library.device_movies([
            {"id": "a" * 16, "title": "Ponyo", "sort_title": "ponyo",
             "overrides": {"title": "Ponyo on the Cliff"}}])
        self.assertEqual(rows[0]["title"], "Ponyo on the Cliff")
        self.assertTrue(rows[0]["has_override"])

    def test_prep_facts_come_from_here_and_lose_to_the_device(self):
        local = {"a" * 16: {"duration": 6060.0, "width": 1280, "height": 720,
                            "vcodec": "h264", "acodec": "aac",
                            "tmdb_score": 0.91, "metadata_source": "tmdb",
                            "poster_source": "tmdb", "prepared_at": 1.0,
                            "pushed_at": 2.0, "notes": "[]"}}
        rows = device_library.device_movies(
            [{"id": "a" * 16, "title": "Ponyo", "duration": None}], local)
        self.assertEqual(rows[0]["tmdb_score"], 0.91)
        # The device said nothing about duration, so the local fact stands.
        self.assertEqual(rows[0]["duration"], 6060.0)
        self.assertTrue(rows[0]["in_admin_db"])

    def test_a_film_this_machine_did_not_prepare_is_marked(self):
        rows = device_library.device_movies([{"id": "b" * 16, "title": "Hook"}])
        self.assertFalse(rows[0]["in_admin_db"])

    def test_the_shelf_order_is_the_normalised_title(self):
        rows = device_library.device_movies([
            {"id": "a" * 16, "title": "Ponyo", "sort_title": "ponyo"},
            {"id": "b" * 16, "title": "The Fifth Element",
             "sort_title": "fifth element"}])
        self.assertEqual([r["title"] for r in rows],
                         ["The Fifth Element", "Ponyo"])


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


class IsTheStickThere(unittest.TestCase):
    """The shared verdict behind the indicator on the Library and Device tabs.

    Worth testing without a display because the interesting part is not the
    dot: it is which of several simultaneous truths gets said, and how long a
    stale one is allowed to stand.
    """

    def setUp(self):
        self.now = 1000.0
        self.state = device_state.DeviceState(clock=lambda: self.now)
        self.state.set_url("http://stick:8080")

    def test_nothing_is_claimed_before_anything_has_been_asked(self):
        """The window opens before the first poll answers. Green there would be
        a guess, and red would send somebody to look at a working stick."""
        colour, headline, _detail = self.state.describe()
        self.assertEqual(self.state.state, device_state.UNKNOWN)
        self.assertEqual(colour, "idle")
        self.assertIn("Checking", headline)

    def test_with_no_address_it_says_where_to_put_one(self):
        state = device_state.DeviceState()
        self.assertEqual(state.state, device_state.UNSET)
        _colour, _headline, detail = state.describe()
        self.assertIn("Settings", detail)

    def test_a_failed_push_turns_it_red_without_waiting_for_a_poll(self):
        """The whole reason every call reports its outcome. A poll four seconds
        ago saying the stick was there does not survive a push that just failed
        against it."""
        self.state.reached()
        self.assertEqual(self.state.state, device_state.ONLINE)
        self.state.sync_begin("sending Ponyo")
        self.state.sync_end("timed out")
        self.assertEqual(self.state.state, device_state.OFFLINE)
        self.assertIn("timed out", self.state.describe()[2])

    def test_ending_a_sync_without_an_error_claims_nothing(self):
        """For the caller that cannot tell -- a job that failed at some step
        after the push. Silence is the honest answer; sync_done is the one that
        says it worked."""
        self.state.unreachable("refused")
        self.state.sync_begin("a film")
        self.state.sync_end()
        self.assertEqual(self.state.state, device_state.OFFLINE)

    def test_syncing_is_a_depth_so_the_first_finisher_does_not_clear_it(self):
        """A queue pushing a film while somebody saves an edit is two writes
        from two threads. A flag would go quiet on the first one and leave the
        indicator claiming nothing was happening mid-upload."""
        self.state.sync_begin("pushing Hook")
        self.state.sync_begin("saving an edit")
        self.state.sync_end()
        self.assertTrue(self.state.syncing)
        self.state.sync_end()
        self.assertFalse(self.state.syncing)

    def test_an_unbalanced_end_cannot_drive_it_negative(self):
        """One stray end from a signal connected twice would otherwise make
        every later sync invisible."""
        self.state.sync_end()
        self.state.sync_end()
        self.state.sync_begin("a film")
        self.assertTrue(self.state.syncing)

    def test_a_sync_in_flight_is_what_the_headline_says(self):
        """"Connected" and "sending Ponyo" are both true during a push, and only
        one of them answers why the window is busy."""
        self.state.reached()
        self.state.sync_begin("sending Ponyo")
        colour, headline, detail = self.state.describe()
        self.assertEqual(colour, "busy")
        self.assertIn("Syncing", headline)
        self.assertIn("Ponyo", detail)

    def test_progress_arriving_late_leaves_no_stale_line(self):
        """A worker's progress signal can be queued behind its own done."""
        self.state.sync_begin("sending Ponyo")
        self.state.sync_end()
        self.state.sync_say("uploading the poster...")
        self.assertNotIn("poster", self.state.describe()[2])

    def test_it_says_how_long_ago_it_last_answered(self):
        """"Not answering" alone does not distinguish a stick switched off two
        seconds ago from one that has been off since Tuesday."""
        self.state.reached()
        self.now += 3700
        self.state.unreachable("timed out")
        self.assertIn("1h 1m", self.state.describe()[2])

    def test_a_fresh_answer_is_not_dated_and_an_old_one_is(self):
        """"as of 4s ago" on every poll is noise. An hour old -- a laptop that
        was shut with the window open -- is the difference between a fact and a
        memory, and it is still green because nothing has said otherwise."""
        self.state.reached()
        self.assertEqual(self.state.describe()[2], "http://stick:8080")
        self.now += 3700
        colour, _headline, detail = self.state.describe()
        self.assertEqual(colour, "ok")
        self.assertIn("as of 1h 1m ago", detail)

    def test_changing_the_address_drops_the_old_verdict(self):
        """Green for four seconds after somebody points the app at a different
        stick is a report on a machine they have stopped caring about."""
        self.state.reached()
        self.state.set_url("http://other:8080")
        self.assertEqual(self.state.state, device_state.UNKNOWN)
        self.assertIsNone(self.state.last_ok)

    def test_the_same_address_again_is_not_a_change(self):
        """refresh_url runs after every Settings dialog, including the ones
        that changed something else."""
        self.state.reached()
        self.state.set_url("http://stick:8080/")      # a trailing slash is not
        self.assertEqual(self.state.state, device_state.ONLINE)   # a new stick

    def test_the_snapshot_moves_only_when_something_visible_does(self):
        """What stops a poll every four seconds repainting two labels forever."""
        self.state.reached()
        before = self.state.snapshot()
        self.now += 4
        self.state.reached()
        self.assertEqual(self.state.snapshot(), before)


class WhatTheTerminalSees(unittest.TestCase):
    """`make admin` prints what the app is doing. These cover the two parts of
    that with a decision in them: which level, and what one line looks like."""

    def test_the_default_is_the_running_commentary_and_not_the_firehose(self):
        self.assertEqual(log_setup.level_from(["playstick"], {}),
                         logging.INFO)

    def test_the_environment_turns_it_up(self):
        """PLAYSTICK_ADMIN_LOG is the one that survives a desktop launcher,
        where there is no command line to add a flag to."""
        self.assertEqual(
            log_setup.level_from(["x"], {"PLAYSTICK_ADMIN_LOG": "DEBUG"}),
            logging.DEBUG)
        self.assertEqual(
            log_setup.level_from(["x"], {"PLAYSTICK_ADMIN_LOG": "quiet"}),
            logging.WARNING)

    def test_a_flag_beats_the_environment(self):
        """-v is for one run, and a shell that exports debug everywhere should
        not make it impossible to ask for less."""
        env = {"PLAYSTICK_ADMIN_LOG": "quiet"}
        self.assertEqual(log_setup.level_from(["x", "-v"], env), logging.DEBUG)

    def test_nonsense_is_ignored_rather_than_fatal(self):
        """A typo in an environment variable must not stop the app starting."""
        self.assertEqual(
            log_setup.level_from(["x"], {"PLAYSTICK_ADMIN_LOG": "loud"}),
            logging.INFO)

    def test_a_line_names_the_module_without_the_package_in_front(self):
        """Every line would otherwise begin with the same eighteen characters."""
        line = self._format("playstick_admin.core.nas_store", "moved it")
        self.assertIn("nas_store: moved it", line)
        self.assertNotIn("playstick_admin", line)

    def test_a_line_from_a_worker_says_which_worker(self):
        """The whole point of most of these lines is that they happened while
        somebody was doing something else."""
        self.assertIn("(queue)", self._format("playstick_admin.core.pipeline",
                                              "encoding", thread="queue"))

    def test_a_line_from_the_window_does_not(self):
        self.assertNotIn("(", self._format("playstick_admin.app", "starting"))

    @staticmethod
    def _format(name, message, thread="MainThread"):
        record = logging.LogRecord(name, logging.INFO, "f.py", 1, message,
                                   (), None)
        record.threadName = thread
        return log_setup._Formatter(
            "%(levelname)s %(where)s%(aside)s: %(message)s").format(record)


class WhenTheStickAnswersWithAPage(unittest.TestCase):
    """An error body is quoted in a log line, a status bar and a message box,
    and none of those has room for a page of HTML."""

    def test_a_short_message_is_left_exactly_as_it_is(self):
        self.assertEqual(device_client.brief("no such film"), "no such film")

    def test_a_page_becomes_one_line(self):
        """Newlines collapse rather than being dropped: a paragraph break in
        the middle of a log is worse than a run-on sentence."""
        page = "<html>\n  <body>\n    <p>Error code: 501</p>\n  </body>\n</html>"
        self.assertNotIn("\n", device_client.brief(page))
        self.assertIn("Error code: 501", device_client.brief(page))

    def test_a_long_one_is_cut_and_says_so(self):
        out = device_client.brief("x" * 500)
        self.assertEqual(len(out), device_client.DETAIL_LIMIT)
        self.assertTrue(out.endswith("..."))

    def test_nothing_at_all_is_not_an_error(self):
        self.assertEqual(device_client.brief(None), "")


if __name__ == "__main__":
    unittest.main()
