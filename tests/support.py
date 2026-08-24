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
SERVER = os.path.join(ROOT, "src", "server")
# The served page is the built bundle, committed under src/player/dist. It is
# the daemon's real input (config.py's PLAYSTICK_UI), so the HTTP tests exercise
# what ships rather than a hand-written stand-in. Rebuild with src/player's
# `npm run build` after a UI change.
UI_HTML = os.path.join(ROOT, "src", "player", "dist", "playstick-ui.html")

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

if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

# Not a micro-optimisation. src/server/playstick/ is the src of an
# Ansible copy, and that task ships the directory whole -- so a __pycache__ the
# test run left behind is bytecode from a developer's Python transferred onto a
# device with 32 GB of eMMC and a different interpreter. Git ignores it and
# would never have said.
sys.dont_write_bytecode = True

from playstick import http as api           # noqa: E402
from playstick import player                # noqa: E402
from playstick.player import Busy           # noqa: E402
# Re-exported for the same reason as `api`: config is read once at import, and
# a test module that reached for playstick.* itself could beat this file to it
# and bake in the ambient environment.
from playstick import projector             # noqa: E402
from playstick import projectionist         # noqa: E402
from playstick.projector import serial_io   # noqa: E402


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
        self.overrides = []
        # Counted so a route can be asserted NOT to have asked. /api/time
        # exists to be the cheapest thing this server does, and "cheap" is a
        # claim about what it touches, not a comment about it.
        self.snapshots = 0

    def add(self, ident, title, **extra):
        item = {"id": ident, "title": title}
        item.update(extra)
        self.items[ident] = item
        self.order.append(ident)
        return item

    def snapshot(self):
        self.snapshots += 1
        return (list(self.order), dict(self.items), self.available,
                self.scanned_at, self.error)

    def get(self, ident):
        return self.items.get(ident)

    def request_rescan(self):
        self.rescans += 1

    def apply_override(self, ident, fields):
        """The real Library keeps a base and an overlay and re-merges; the
        handler only cares that an edit lands on the item and that an unknown
        id answers None, so the fake just mutates in place. A field set to None
        is a reset, dropped from the item the way the overlay would drop it."""
        self.overrides.append((ident, dict(fields)))
        item = self.items.get(ident)
        if item is None:
            return None
        for key, value in fields.items():
            if value is None:
                item.pop(key, None)
            else:
                item[key] = value
        return dict(item)


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
        # Same counter, same reason as FakeLibrary.snapshots: the real status()
        # is four round trips to mpv, and a route that claims to touch nothing
        # has to be held to it.
        self.statuses = 0

    def state(self):
        return self.playing

    def current_title(self):
        return self.item["title"] if self.item else ""

    def current_item(self):
        return self.item

    def status(self):
        self.statuses += 1
        return dict(self.data)

    def timecode(self, tc, at, rate=1.0, epoch=1):
        """Publish a timeline, the way Player._advance() would.

        Kept as a method rather than left to each test to build the dict,
        because the four keys travel together and a test that spelled one of
        them wrong would assert successfully about nothing.
        """
        self.data["timecode"] = {"tc": tc, "at": at, "rate": rate,
                                 "epoch": epoch}
        self.data["position"] = tc
        self.data["position_valid"] = True
        return self.data["timecode"]

    def start(self, item, progress=None):
        self.calls.append(("start", item["id"]))
        # Refused before either step is reported, which is where the real
        # player refuses too: its state and AirPlay checks both run before it
        # touches the display.
        if self.start_error is not None:
            raise self.start_error
        if progress:
            # The real player reports these two from inside itself, so a test
            # that hands one in should see them. Nothing in the HTTP suite
            # does; test_projectionist.py is what cares.
            progress("display")
            progress("starting")
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


class FakeProjectionist:
    """The handler's view of the projectionist, with the thread taken out.

    begin() starts the film synchronously. The real one hands the work to a
    thread and answers the page over /api/status, which is exactly the wrong
    shape for a suite about routing and JSON: every assertion would have to
    wait for something. The sequence itself -- steps, cancels, faults, the idle
    timeout -- is tested against the real class in test_projectionist.py, where
    the clock is a variable rather than a wall.
    """

    def __init__(self, player):
        self._player = player
        # Set to a dict to make the handler report a preparation in flight.
        self.prepare = None
        self.projector = {"model": "none", "power": "unknown", "fault": ""}
        self.message = ""
        self.calls = []
        # Set to an exception instance to make the next begin() raise it.
        self.begin_error = None

    def state(self):
        if self.prepare is not None:
            return "preparing"
        return self._player.state()

    def current_item(self):
        return self._player.current_item()

    def current_title(self):
        return self._player.current_title()

    def progress(self):
        return self.prepare

    def projector_status(self):
        return dict(self.projector)

    def notice(self):
        return self.message

    def begin(self, item):
        self.calls.append(("begin", item["id"]))
        if self.begin_error is not None:
            raise self.begin_error
        self._player.start(item)

    def stop(self):
        self.calls.append(("stop",))
        self.prepare = None
        self._player.stop()


class FakeProjector:
    """A projector made of a script, for test_projectionist.py.

    Records every call in order -- the sequence is mostly about ordering, so
    the order of this list is the assertion. Any entry in `fail` makes that
    method raise instead, which is how the "a broken projector still plays the
    film" cases are set up.
    """

    model = "fake"

    def __init__(self, power="standby", input_code=""):
        self.power = power
        self.input_code = input_code
        self.calls = []
        self.fail = {}          # method name -> exception to raise
        self.closed = False
        # Ticks of power_state() after power_on() before the lamp reports on.
        # 0 means it is lit the moment PON returns.
        self.warmup_polls = 0
        self._polls_left = 0

    def _maybe_fail(self, name):
        exc = self.fail.get(name)
        if exc is not None:
            raise exc

    def power_state(self):
        self.calls.append("power_state")
        self._maybe_fail("power_state")
        if self._polls_left > 0:
            self._polls_left -= 1
            return "standby"
        return self.power

    def power_on(self):
        self.calls.append("power_on")
        self._maybe_fail("power_on")
        self.power = "on"
        self._polls_left = self.warmup_polls

    def power_off(self):
        self.calls.append("power_off")
        self._maybe_fail("power_off")
        self.power = "standby"

    def set_input(self, code):
        self.calls.append("set_input:%s" % code)
        self._maybe_fail("set_input")
        self.input_code = code

    def current_input(self):
        self.calls.append("current_input")
        self._maybe_fail("current_input")
        return self.input_code

    def close(self):
        self.closed = True


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
        self.projectionist = FakeProjectionist(self.player)
        self.thumbs = FakeThumbs()
        handler = type("TestHandler", (api.Handler,), {
            "library": self.library,
            "player": self.player,
            "projectionist": self.projectionist,
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


class Clock:
    """Time as a variable the test advances.

    The projectionist waits ten seconds for a lamp blackout and thirty minutes
    for an idle timeout, and a suite that sat through either would be a suite
    nobody runs. Both the clock and the sleep go in through the constructor,
    and sleeping here is simply addition -- which is also what makes the
    assertions exact instead of approximate.
    """

    def __init__(self, now=0.0):
        self.now = now
        self.slept = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.slept += seconds
        self.now += seconds


__all__ = ["ApiTest", "Busy", "Clock", "FakeLibrary", "FakePlayer",
           "FakeProjectionist", "FakeProjector", "FakeThumbs", "Response",
           "TMP", "UI_HTML", "api", "audio_track", "patched", "projectionist",
           "projector", "serial_io", "write_file"]
