"""Harness for the HTTP tests: a real server, fake workers, no device.

Everything here exists to make one thing possible -- exercising the handler
over a real socket, with real status lines, real headers and real Range
arithmetic, on a machine that has no projector, no NAS and no mpv. The three
workers behind the handler are replaced by the fakes below; the handler itself,
the routing, the JSON shapes and the byte-serving are the shipped code.

TWO THINGS TO KNOW BEFORE ADDING A TEST

The daemon reads its whole configuration from the environment ONCE, at import
of playstick.config. So this module sets that environment before it imports
anything from the package, and test modules must reach the package through the
names re-exported here rather than importing it themselves -- an import that
beats this one to the punch would bake in the ambient environment instead.

Because config is read once, and because http.py binds its constants with
`from .config import ...`, changing one for a single test means patching the
name in the HTTP module's own namespace -- `playstick.http.PHONE_AUDIO`, not
`playstick.config.PHONE_AUDIO`. The `patched()` helper below does that.
"""

import atexit
import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = os.path.join(ROOT, "roles", "player", "files")
UI_HTML = os.path.join(FILES, "playstick-ui.html")

# Scratch for the two directories the daemon really touches: the poster cache
# (Thumbs.cached_path resolves against it, and _api_thumb reads it directly)
# and a stand-in library root for the audio fixtures.
TMP = tempfile.mkdtemp(prefix="playstick-tests-")
atexit.register(shutil.rmtree, TMP, ignore_errors=True)

os.environ.update({
    "PLAYSTICK_UI": UI_HTML,
    "PLAYSTICK_LIBRARY": os.path.join(TMP, "movies"),
    # Set-but-empty means "ignore any index", which is the distinction
    # config.py is careful about. Nothing here scans a library anyway.
    "PLAYSTICK_INDEX": "",
    "PLAYSTICK_THUMB_DIR": os.path.join(TMP, "thumbs"),
    "PLAYSTICK_BUSY_FILE": os.path.join(TMP, "playing"),
    "PLAYSTICK_RESTORE_FILE": os.path.join(TMP, "restore-airplay"),
    "PLAYSTICK_ALLOW_NETWORKS": "",
    "PLAYSTICK_AUDIO": "0",                 # the projector has no speakers
    "PLAYSTICK_PHONE_AUDIO": "1",
    # Pacing off by default so the suite is not sleeping through it. The one
    # test that is ABOUT the pacing patches it back on at a rate of its own.
    "PLAYSTICK_PHONE_AUDIO_KBPS": "0",
    "PLAYSTICK_PHONE_AUDIO_BURST": "0",
    "PLAYSTICK_PHONE_AUDIO_STREAMS": "6",
    # Empty means airplay_active() answers False without shelling out to ss,
    # so the interlock is a patch point rather than a dependency on iproute2.
    "PLAYSTICK_AIRPLAY_PORT": "",
})
os.makedirs(os.environ["PLAYSTICK_THUMB_DIR"], exist_ok=True)
os.makedirs(os.environ["PLAYSTICK_LIBRARY"], exist_ok=True)

if FILES not in sys.path:
    sys.path.insert(0, FILES)

# Not a micro-optimisation. roles/player/files/playstick/ is the src of an
# Ansible copy, and that task ships the directory whole -- so a __pycache__ the
# test run left behind is bytecode from a developer's Python transferred onto a
# device with 32 GB of eMMC and a different interpreter. Git ignores it and
# would never have said.
sys.dont_write_bytecode = True

from playstick import http as api           # noqa: E402
from playstick.player import Busy           # noqa: E402


# -- the fakes


class FakeLibrary:
    """Library's three-method surface. `snapshot` returns the same five-tuple
    the real one does, in the same order, because the handler unpacks it
    positionally."""

    def __init__(self):
        self.items = {}
        self.order = []
        self.available = True
        self.scanned_at = 1700000000.0
        self.error = ""
        self.rescans = 0

    def add(self, ident, title, **extra):
        item = {"id": ident, "title": title}
        item.update(extra)
        self.items[ident] = item
        self.order.append(ident)
        return item

    def snapshot(self):
        return (list(self.order), dict(self.items), self.available,
                self.scanned_at, self.error)

    def get(self, ident):
        return self.items.get(ident)

    def request_rescan(self):
        self.rescans += 1


class FakePlayer:
    """Records what it was told to do. The state machine is only as deep as
    the handler can see: /api/status asks for a state, a title, an item and a
    status dict, and never for anything mpv would have to answer."""

    def __init__(self):
        self.playing = "idle"
        self.item = None
        self.data = {}
        self.calls = []
        # Set to an exception instance to make the next start() raise it.
        self.start_error = None

    def state(self):
        return self.playing

    def current_title(self):
        return self.item["title"] if self.item else ""

    def current_item(self):
        return self.item

    def status(self):
        return dict(self.data)

    def start(self, item):
        self.calls.append(("start", item["id"]))
        if self.start_error is not None:
            raise self.start_error
        self.item = item
        self.playing = "playing"

    def stop(self):
        self.calls.append(("stop",))
        self.item = None
        self.playing = "idle"

    def set_pause(self, paused):
        self.calls.append(("set_pause", paused))
        self.playing = "paused" if paused else "playing"

    def nudge_volume(self, delta):
        self.calls.append(("nudge_volume", delta))


class FakeThumbs:
    """Note that the handler does NOT go through this object to find a cached
    poster -- it calls the real Thumbs.cached_path, which resolves against
    THUMB_DIR. Only /api/library's has_thumb flag asks the worker."""

    def __init__(self):
        self.requested = []
        self.have_ids = set()
        self.pending_count = 0

    def have(self, ident):
        return ident in self.have_ids

    def request(self, ident):
        self.requested.append(ident)

    def pending(self):
        return self.pending_count


# -- the server


class Response:
    __slots__ = ("status", "headers", "body")

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def json(self):
        return json.loads(self.body)

    def header(self, name, default=None):
        return self.headers.get(name, default)


class ApiTest(unittest.TestCase):
    """One server per test, on a port the kernel picks, with the fakes wired
    in through a throwaway Handler subclass rather than onto Handler itself --
    the real daemon sets those as class attributes once at startup, and a suite
    that did the same would leak one test's library into the next."""

    def setUp(self):
        self.library = FakeLibrary()
        self.player = FakePlayer()
        self.thumbs = FakeThumbs()
        handler = type("TestHandler", (api.Handler,), {
            "library": self.library,
            "player": self.player,
            "thumbs": self.thumbs,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    # -- issuing requests

    def connect(self, timeout=10):
        """A connection the caller keeps, for the tests that care what happens
        to it after a response -- keep-alive reuse, or a stream held open."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        self.addCleanup(conn.close)
        return conn

    def fetch(self, path, method="GET", body=None, headers=None, conn=None,
              timeout=10):
        own = conn is None
        conn = conn or self.connect(timeout=timeout)
        payload = None
        headers = dict(headers or {})
        if body is not None:
            payload = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if own:
            conn.close()
        return Response(resp.status, dict(resp.getheaders()), data)

    def raw(self, request_bytes, timeout=10):
        """A socket the test drives itself, for the two questions http.client
        will not let one ask: what exactly went onto the wire for a malformed
        request, and WHEN each part of a paced body arrived."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        self.addCleanup(sock.close)
        sock.sendall(request_bytes)
        return sock

    def drain(self, sock, settle=0.5):
        """Everything the server sends, then silence.

        Needed because http.client cannot answer 'was anything written that
        should not have been'. After a HEAD it throws its read buffer away, so
        a body the server wrongly wrote is discarded along with it and the
        client sees a perfectly good connection -- until the timing shifts and
        it does not.
        """
        sock.settimeout(settle)
        buf = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except (socket.timeout, TimeoutError):
            pass
        sock.settimeout(None)
        return buf

    # -- assertions used in more than one place

    def assertJson(self, resp, status=200):
        self.assertEqual(resp.status, status,
                         "expected %d, got %d: %r" % (status, resp.status, resp.body[:200]))
        self.assertEqual(resp.header("Content-Type"), "application/json")
        return resp.json

    def assertNotFound(self, resp):
        self.assertEqual(self.assertJson(resp, 404), {"error": "not found"})


def patched(**names):
    """Override module-level configuration for one test.

    Patches into playstick.http rather than playstick.config on purpose: the
    handler bound those names at import with `from .config import ...`, so a
    patch on config would change a value nothing reads.
    """
    return mock.patch.multiple(api, **names)


def write_file(name, data):
    """A fixture file under the scratch root, returned as an absolute path.
    Used for audio tracks and posters -- both are paths the handler opens for
    real, which is the point of them being files rather than mocks."""
    path = os.path.join(TMP, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def audio_track(n=0, lang="eng", title="English", channels=2, default=True,
                offset=0.0, path=""):
    """One entry as playstick-prep.py writes it into the index. Every key here
    is one /api/status copies out by name."""
    return {"n": n, "lang": lang, "title": title, "channels": channels,
            "default": default, "offset": offset, "path": path}


__all__ = ["ApiTest", "Busy", "FakeLibrary", "FakePlayer", "FakeThumbs",
           "Response", "TMP", "UI_HTML", "api", "audio_track", "patched",
           "write_file"]
