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

    @classmethod
    def reset(cls, films=()):
        cls.films = list(films)
        cls.synced = []
        cls.thumbs = []


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


if __name__ == "__main__":
    unittest.main()
