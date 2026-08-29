"""The HTTP surface: one handler, and the routes it will answer.

THE RULE ABOUT PATHS, AND THE ONE PLACE IT IS NARROWER THAN IT WAS

No filesystem path crosses this boundary on any route a phone uses. Clients
send opaque ids and small integers; /api/audio is the only route in the process
that streams file bytes, and it names a file by indexing a table rather than by
being told one.

The exception is /api/admin/films, and it is a real one rather than a wording
change. The library lives in a database on this device now, so the machine that
prepares films has to be able to put paths INTO it -- there is no index file on
the share left to read them from. The containment is three-fold and every part
of it is load-bearing:

  * library-relative only. An absolute path, or one that resolves outside the
    library root, is a 400 with a sentence -- refused, not silently dropped, so
    a mis-configured admin app is a visible failure rather than a film that
    quietly never appears.
  * the check is Library._under_root, the same realpath-and-compare that has
    always stood between a stored path and mpv. It runs twice: once here before
    a row is written, and again in Library._hydrate before a path is used. A
    bug in either one alone cannot reach the filesystem.
  * these routes are the only ones that accept a path in any form.

There is still no authentication -- the CIDR allowlist is the whole control,
exactly as it was before. See the package docstring for why, and note that it
is now the only thing standing between somebody already on the LAN and the
film table.
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

from .config import (
    ADMIN_SYNC_MAX, ADMIN_THUMB_CLEAR_RE, ADMIN_THUMB_MAX, ADMIN_THUMB_RE,
    ALLOW_NETWORKS, AUDIO_CHUNK, AUDIO_ROUTE_RE, AUDIO_SLOTS, HAS_AUDIO,
    LIBRARY, PHONE_AUDIO, PHONE_AUDIO_BPS, PHONE_AUDIO_BURST,
    PHONE_AUDIO_STREAMS, RANGE_RE, SYNC_HEADER, SYNC_KEEP_RE, SYNC_MAX,
    SYNC_MAX_RATE, UI_FILE, log
)
from .airplay import airplay_active
from .library import Library
from .thumbs import placeholder_svg
from .player import Busy


_sync_lock = threading.Lock()
_sync_window = [0.0, 0]         # start of the current second, lines written in it

# Which run of this daemon a clock reading came from. time.monotonic() counts
# from an arbitrary origin, and a restart picks a different one -- so without
# this a phone would carry on applying an offset that is wrong by an unbounded
# amount, silently, with no reading it could take that would say so. Opaque and
# per-process, like the build stamp: it names a run, not a machine or a person.
SESSION = secrets.token_hex(4)


def sync_allowed():
    """One second's worth of budget for telemetry lines, shared by everybody.

    Deliberately global rather than per client: the resource being protected is
    one journal on one eMMC, and a limit that reset per address would be no
    limit at all against the case that motivates it.
    """
    now = time.monotonic()
    with _sync_lock:
        if now - _sync_window[0] >= 1.0:
            _sync_window[0] = now
            _sync_window[1] = 0
        if _sync_window[1] >= SYNC_MAX_RATE:
            return False
        _sync_window[1] += 1
        return True


# The page announces which build it is by carrying this string, which the
# daemon rewrites on the way out. A copy that reaches a browser some other way
# -- opened off the disk, saved out of the inspector -- still parses, and says
# so plainly rather than claiming to be a build it is not.
_BUILD_TOKEN = b"__PLAYSTICK_BUILD__"

_ui_lock = threading.Lock()
_ui_cache = [None, "", b""]     # what it was built from, the id, the page


def ui_page():
    """The page as it is served, and the identity of the build it is.

    THE PROBLEM. Serving the page no-store means a browser that asks for it
    always gets the current one. Nothing in that reaches a browser that is not
    asking -- and after a deploy, none of them are. The phones in the house
    have the page open already; they poll /api/status forever and never
    navigate again. Left alone they run last week's JavaScript against this
    week's daemon until somebody thinks to pull down and refresh, which is
    exactly the thing nobody thinks to do, least of all a child.

    THE IDENTITY. A hash of the file on disk, so it changes when and only when
    the page changes. Not a version number, which somebody has to remember to
    raise. Not the start time of the process, which would order every phone in
    the house to reload after a power cut, for a page that had not changed by a
    byte. Not a hash of the daemon as well: the API is additive by house rule,
    so an older page against a newer daemon is a supported combination, and
    reloading for it would be interrupting people to deliver nothing.

    CACHED against the file's mtime and size, because /api/status asks for the
    identity on every poll and there are several phones polling. Keyed on a
    stat rather than read once at import, because the dev container bind-mounts
    this file straight out of the repository: edit it, and every browser
    pointed at that container reloads itself. That is the same mechanism the
    appliance uses, exercised every time anybody touches the page.
    """
    stat = os.stat(UI_FILE)
    key = (UI_FILE, stat.st_mtime_ns, stat.st_size)
    with _ui_lock:
        if _ui_cache[0] == key:
            return _ui_cache[1], _ui_cache[2]
    with open(UI_FILE, "rb") as fh:
        raw = fh.read()
    # Truncated: this is compared for equality by a browser, never used to
    # prove anything, and twelve hex digits is already far more than the number
    # of builds a household appliance will ever see.
    build = hashlib.sha256(raw).hexdigest()[:12]
    page = raw.replace(_BUILD_TOKEN, build.encode())
    with _ui_lock:
        _ui_cache[:] = [key, build, page]
    return build, page


def build_id():
    """Which build the page currently on disk is, for /api/status to report.

    A page that cannot be read at all answers with the last one that could be.
    The alternative -- an empty build, which the page reads as "no opinion" --
    is right too, and this is only better by a little: a UI file that vanished
    between two polls is a broken deploy, and telling every phone to reload
    into a 500 helps nobody.
    """
    try:
        return ui_page()[0]
    except OSError:
        return _ui_cache[1]


def _int_or_none(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN and the infinities go in a REAL column happily and come back out to
    # arithmetic that then produces neither an error nor a number.
    return number if number == number and abs(number) != float("inf") else None


def _float_or(value, default):
    number = _float_or_none(value)
    return default if number is None else number


def _image_type(data):
    """JPEG or PNG, by magic number. "" for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return ""


class Handler(BaseHTTPRequestHandler):
    server_version = "playstick"
    protocol_version = "HTTP/1.1"

    # A keep-alive connection that goes quiet used to cost nothing, because
    # every response was over in milliseconds. Now that /api/audio holds one
    # open for as long as somebody is listening, a phone that goes out of range
    # mid-film leaves a thread parked on a socket that will never say anything
    # again. Thirty seconds is far longer than any request here legitimately
    # waits between reads.
    timeout = 30

    library = None      # set on the server before serve_forever
    # The database, set alongside the workers that read it. Held so a route can
    # reach the store directly rather than through whichever worker happens to
    # own the table it wants.
    db = None
    # Whether the NAS is awake, and the one thing that can be done about it.
    nas = None
    thumbs = None
    player = None
    # The parsed request body for the POST in flight, or None between requests.
    # Cached so _body() can be called from more than one place -- and, above
    # all, so do_POST can drain it whatever the route decides. See do_POST.
    _posted = None
    # Asked about state, and about starting and stopping a film. The player is
    # still here because pause, volume and the status numbers come straight
    # from mpv and have nothing to do with the projector.
    projectionist = None

    # BaseHTTPRequestHandler logs every request to stderr, i.e. into the
    # journal, and a page polling once a second per phone would be the only
    # thing in it. Errors still get through via log_error.
    def log_message(self, fmt, *args):
        pass

    def _allowed(self):
        if not ALLOW_NETWORKS:
            return True
        try:
            addr = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(addr in net for net in ALLOW_NETWORKS)

    # -- helpers

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        # A HEAD carries the headers of the GET and none of its body. Writing
        # one anyway is not merely incorrect, it desynchronises the connection:
        # protocol_version is HTTP/1.1, so the socket stays open and the next
        # request on it reads this body as a status line. _stream_audio makes
        # the same check for the same reason.
        if self.command == "HEAD":
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, code=200):
        self._send(code, json.dumps(payload).encode(), "application/json",
                   {"Cache-Control": "no-store"})

    def _body(self):
        if self._posted is not None:
            return self._posted
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._posted = {}
            return self._posted
        if not length:
            self._posted = {}
            return self._posted
        try:
            data = json.loads(self.rfile.read(length))
        except ValueError:
            self._posted = {}
            return self._posted
        # Parsed, but not an object: a bare list or number would reach .get()
        # and take the connection down with an AttributeError that no caller
        # sees. Same answer as unparseable -- there is nothing in it.
        self._posted = data if isinstance(data, dict) else {}
        return self._posted

    # -- state shared by /api/status and /api/library

    def _state(self):
        # The projectionist answers first because it is the only thing that
        # knows about "preparing"; below that it delegates to the player, so
        # the rest of this is unchanged.
        state = self.projectionist.state()
        if state != "idle":
            return state
        if airplay_active():
            return "airplay"
        _order, _items, available, _at, _err = self.library.snapshot()
        return "idle" if available else "unavailable"

    # -- routes

    def do_GET(self):                                # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._send(200, b"ok\n", "text/plain")
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/api/time":
            return self._api_time()
        if path == "/":
            return self._serve_ui()
        if path == "/api/library":
            return self._api_library()
        if path == "/api/admin/library":
            return self._api_admin_library()
        if path.startswith("/api/thumb/"):
            return self._api_thumb(path[len("/api/thumb/"):])
        if path == "/api/status":
            return self._api_status()
        if PHONE_AUDIO:
            match = AUDIO_ROUTE_RE.match(path)
            if match:
                return self._api_audio(match.group(1), int(match.group(2)))
        return self._json({"error": "not found"}, 404)

    def do_HEAD(self):                               # noqa: N802 - stdlib API
        """Only the audio route, and only because media clients and anybody
        debugging one reach for HEAD first. Everything else on this server is
        generated per request, so answering HEAD for it would mean building the
        body to measure it and then throwing it away."""
        path = urlparse(self.path).path
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if PHONE_AUDIO:
            match = AUDIO_ROUTE_RE.match(path)
            if match:
                return self._api_audio(match.group(1), int(match.group(2)))
        return self._json({"error": "not found"}, 404)

    def do_POST(self):                               # noqa: N802 - stdlib API
        path = urlparse(self.path).path
        # Read the body up front, whatever the route below decides to do with
        # it. protocol_version is HTTP/1.1, so the socket stays open, and a
        # body left unread is parsed as the head of the NEXT request line --
        # "{}POST /api/resume" -> 501. Only the routes that take an argument
        # used to read it, so pause/resume/stop desynced the connection for a
        # browser that reuses it (the test client opens a fresh one each time,
        # which is why this hid). _body() caches, so the routes that do want it
        # get the same parsed dict back.
        self._posted = None
        self._body()
        if not self._allowed():
            return self._json({"error": "not on the local network"}, 403)
        if path == "/api/play":
            return self._api_play()
        if path in ("/api/pause", "/api/resume"):
            self.player.set_pause(path.endswith("pause"))
            return self._api_status()
        if path == "/api/stop":
            # One button on the page, two things it may mean: abandon a film
            # that is still warming a lamp, or stop one that is playing. The
            # projectionist does whichever applies, so the page does not have
            # to know which state it is in.
            self.projectionist.stop()
            return self._api_status()
        if path == "/api/volume":
            # Coerced here rather than in the player, because this is where an
            # untrusted number arrives: the player does arithmetic on it, and
            # a delta of "loud" would otherwise be a ValueError in a request
            # thread and a connection the page never gets an answer on.
            try:
                delta = int(self._body().get("delta", 0))
            except (TypeError, ValueError):
                delta = 0
            self.player.nudge_volume(delta)
            return self._api_status()
        if path == "/api/seek":
            return self._api_seek()
        if path == "/api/subtitle":
            return self._api_subtitle()
        if path == "/api/nas/wake":
            return self._api_nas_wake()
        if path == "/api/rescan":
            self.library.request_rescan()
            return self._json({"ok": True})
        if path == "/api/admin/item":
            return self._api_admin_item()
        if path == "/api/admin/films":
            return self._api_admin_films()
        match = ADMIN_THUMB_CLEAR_RE.match(path)
        if match:
            return self._api_admin_thumb_clear(match.group(1))
        return self._json({"error": "not found"}, 404)

    def do_PUT(self):                                # noqa: N802 - stdlib API
        """One route, and the only one on this server that takes a body which
        is not JSON.

        A poster could have been base64 in a POST like everything else. It is
        not, because the admin app pushes one per film on a full sync -- a few
        hundred of them -- and base64 would inflate every one by a third for no
        gain on a link whose whole job is to be the slow part. The body is read
        here rather than in _body() for the same reason: it must never be
        handed to json.loads.
        """
        path = urlparse(self.path).path
        if not self._allowed():
            # The body is still unread at this point, and protocol_version is
            # HTTP/1.1 -- so it would be parsed as the next request line. Drain
            # it before refusing.
            self._drain()
            return self._json({"error": "not on the local network"}, 403)
        match = ADMIN_THUMB_RE.match(path)
        if match:
            return self._api_admin_thumb(match.group(1))
        self._drain()
        return self._json({"error": "not found"}, 404)

    def _drain(self):
        """Read and discard a body this handler is not going to parse.

        Capped, because the point is to keep the connection usable rather than
        to accept anything a client cares to send: past the cap the connection
        is not worth saving and closing it is the honest answer.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return
        if length > ADMIN_THUMB_MAX:
            self.close_connection = True
            return
        try:
            self.rfile.read(length)
        except OSError:
            self.close_connection = True

    def _serve_ui(self):
        try:
            _build, body = ui_page()
        except OSError as exc:
            return self._send(500, ("UI missing: %s" % exc).encode(), "text/plain")
        # no-store rather than a validator: the page is 90 KB fetched once per
        # visit over a LAN, and a conditional request that saves that is not
        # worth one browser somewhere deciding a 304 means it may keep the copy
        # it has. Everything expensive here -- the posters, the soundtracks --
        # caches properly and is addressed separately.
        self._send(200, body, "text/html; charset=utf-8", {"Cache-Control": "no-store"})

    def _api_library(self):
        order, items, available, scanned_at, error = self.library.snapshot()
        # One query for the whole grid rather than one stat per tile, which is
        # what `have(ident)` per film used to cost when a poster was a file.
        have_thumb = self.thumbs.haves()
        payload = {
            "available": available,
            "error": error,
            "scanned_at": scanned_at,
            "state": self._state(),
            "items": [
                {
                    "id": ident,
                    "title": items[ident]["title"],
                    # Whether there are bytes to serve. False means the tile
                    # gets the placeholder and the request enqueues an
                    # extraction; a poster the admin app uploaded is already in
                    # the table, so those tiles never show one at all.
                    "has_thumb": ident in have_thumb,
                    # Empty for a film the preparer could find nothing about.
                    # The page ignores what it does not recognise, so this is
                    # additive for an older ui.html.
                    "year": items[ident].get("year"),
                    "rating": items[ident].get("rating"),
                    "genres": items[ident].get("genres") or [],
                    # Empty when the preparer had no shelf key to offer, where
                    # the page falls back to the title -- which is what it
                    # would have sorted on anyway.
                    "sort_title": items[ident].get("sort_title") or "",
                    # Whether this film is held back from the children's grid.
                    # Sent for every client, not just the desktop editor: the
                    # phones need it to leave a hidden film out, and the editor
                    # needs it to draw the toggle. Additive, so an older ui.html
                    # ignores it and shows everything as it always did.
                    "hidden": bool(items[ident].get("hidden")),
                    # Languages only, not the tracks themselves. The sheet on
                    # the grid offers a PREFERRED language before a film has
                    # started, and to do that it needs to know what the library
                    # has to offer, not which numbered track any one film keeps
                    # it on.
                    "audio_langs": sorted({t["lang"] for t
                                           in items[ident].get("audio") or []}),
                }
                for ident in order
            ],
        }
        self._json(payload)

    def _api_seek(self):
        """Move the film. The page sends one number of seconds, signed.

        Coerced here, like /api/volume and for the same reason -- the player
        does arithmetic on it and a delta of "forward" would otherwise be a
        ValueError on a thread nobody gets an answer from. The outer clamp is a
        sanity bound rather than the real one: the player clamps to the film,
        which is the only clamp that knows how long the film is.

        Refused with a sentence when nothing is playing, rather than silently
        doing nothing: the page only draws these buttons on the now-playing
        view, so a seek arriving on an idle daemon means two phones disagree
        about what is on, and the one that pressed should be told.
        """
        if self._state() not in ("playing", "paused"):
            return self._json({"error": "Nothing is playing."}, 409)
        try:
            delta = float(self._body().get("delta", 0))
        except (TypeError, ValueError):
            delta = 0.0
        if delta != delta or delta in (float("inf"), float("-inf")):
            delta = 0.0
        delta = max(-86400.0, min(86400.0, delta))
        self.player.seek(delta)
        return self._api_status()

    def _api_subtitle(self):
        """Which subtitle track is on the picture. 0 means none.

        The number is validated against mpv's own track list inside the player
        before it is used, which is the same shape of rule /api/play follows
        with film ids: a client names something, and the name is only ever
        looked up in a table this process built.
        """
        if self._state() not in ("playing", "paused"):
            return self._json({"error": "Nothing is playing."}, 409)
        try:
            track = int(self._body().get("track", 0))
        except (TypeError, ValueError):
            track = 0
        self.player.set_subtitle(max(0, track))
        return self._api_status()

    def _api_nas_wake(self):
        """Switch the machine the films live on back on.

        Takes no body: there is exactly one NAS and its address is a setting,
        not something a client may name. That is the same rule /api/play
        follows with film paths, applied to a host -- no string a client sends
        reaches the socket.

        Answers with the status payload on success, which is the convention
        every other control POST here follows, so the phone repaints from the
        response instead of waiting up to three seconds for the next poll. A
        refusal is a 409 with a sentence the page shows verbatim; the cooldown
        that produces most of them lives in the watcher rather than here,
        because there is more than one phone.
        """
        ok, reason = self.nas.wake()
        if not ok:
            return self._json({"error": reason}, 409)
        return self._api_status()

    def _api_admin_item(self):
        """One film's metadata, edited from the desktop admin view.

        The mirror of /api/play in shape -- an id that has to name a real film,
        a body coerced at this boundary, a status JSON back -- and the mirror of
        it in caution too. Nothing here can reach the filesystem: apply_override
        drops any key that is not editable metadata, so the poster path and the
        soundtrack paths this same film carries are not addressable through it.
        """
        body = self._body()
        ident = str(body.get("id", ""))
        raw = body.get("fields")
        if not isinstance(raw, dict):
            raw = {}
        item = self.library.apply_override(ident, self._admin_fields(raw))
        if item is None:
            return self._json({"error": "That film is not in the library."}, 404)
        return self._json({
            "id": item["id"],
            "title": item.get("title", ""),
            "sort_title": item.get("sort_title") or "",
            "year": item.get("year"),
            "rating": item.get("rating"),
            "genres": item.get("genres") or [],
            "hidden": bool(item.get("hidden")),
        })

    @staticmethod
    def _admin_fields(raw):
        """The editor's form, coerced where the untrusted value lands -- the
        same boundary /api/volume turns "loud" into 0 at. Only keys the form
        actually sent are touched, so changing one field leaves the rest of a
        film's overlay alone. An emptied box is a reset rather than a value: a
        blank title means "go back to what the index said", not "call this
        film ''" -- the library reads None on any field as exactly that."""
        fields = {}
        if "hidden" in raw:
            fields["hidden"] = bool(raw.get("hidden"))
        for key in ("title", "sort_title"):
            if key in raw:
                fields[key] = str(raw.get(key) or "").strip() or None
        if "year" in raw:
            try:
                fields["year"] = int(raw.get("year"))
            except (TypeError, ValueError):
                fields["year"] = None
        if "rating" in raw:
            # Whatever the .nfo held: a score like 7.8 or a certificate like
            # "PG". Numbers pass through; everything else is text, blank resets.
            value = raw.get("rating")
            if isinstance(value, bool) or value is None:
                value = None
            elif not isinstance(value, (int, float)):
                value = str(value).strip() or None
            fields["rating"] = value
        if "genres" in raw:
            value = raw.get("genres")
            picked = ([str(g).strip() for g in value if str(g).strip()]
                      if isinstance(value, list) else [])
            fields["genres"] = picked or None
        return fields

    # -- the admin app's routes ---------------------------------------------
    #
    # Everything below is what the desktop app uses and no phone ever calls.
    # They are the only routes that write the film table, and the only ones
    # that accept a path in any form -- see the module docstring for what that
    # costs and what contains it.

    def _api_admin_library(self):
        """The film table as it is stored: paths, overrides, poster etags.

        The one read that hands out stored paths, because the admin app is
        diffing against what it sent and cannot do that against the resolved
        view the phones get. The etags are what let it upload a poster only
        when the local file's bytes differ, rather than pushing every poster in
        the library on every sync.
        """
        return self._json({
            "films": self.library.rows(),
            "library": LIBRARY,
        })

    def _api_admin_films(self):
        """The library, pushed from the machine that prepared it.

        `reconcile` is what a full library push sets: films absent from the
        batch are then deleted, which is how a film removed from the share
        stops appearing on the grid. Without it this is an upsert of whatever
        was sent and nothing else, which is what correcting one film wants.

        Every entry is validated before ANY of them is written -- one bad path
        refuses the whole request rather than leaving half a library. The
        alternative, skipping the bad entry, would mean a typo in a path
        presents as a film that silently never appears, which is the failure
        this endpoint most needs not to have.
        """
        body = self._body()
        raw = body.get("films")
        if not isinstance(raw, list):
            return self._json({"error": "films must be a list."}, 400)
        if len(raw) > ADMIN_SYNC_MAX:
            return self._json(
                {"error": "That is more than %d films." % ADMIN_SYNC_MAX}, 400)
        films, seen = [], set()
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                return self._json(
                    {"error": "Film %d is not an object." % index}, 400)
            film, error = self._sync_film(entry)
            if error:
                return self._json({"error": "Film %d: %s" % (index, error)}, 400)
            if film["id"] in seen:
                return self._json(
                    {"error": "Film %d: %s appears twice."
                              % (index, film["id"])}, 400)
            seen.add(film["id"])
            films.append(film)
        written, removed = self.library.sync(films,
                                             bool(body.get("reconcile")))
        return self._json({"written": written, "removed": removed})

    @staticmethod
    def _sync_film(entry):
        """One film's row, coerced and proved. Returns (film, error).

        Every path here is checked with the same Library._under_root the player
        uses, against the real library root -- so a `rel` of "../../etc/passwd"
        or an absolute "/etc/passwd" is refused here and never becomes a row.
        The coercion is the boundary work /api/volume does for a delta, applied
        to a bigger shape.
        """
        root = os.path.realpath(LIBRARY)

        def under(rel):
            if not isinstance(rel, str) or not rel:
                return None
            if os.path.isabs(rel):
                return None
            return Library._under_root(root, rel)

        ident = entry.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{16}", ident):
            return None, "id must be sixteen lowercase hex characters."
        source_rel = entry.get("source_rel")
        if under(source_rel) is None:
            return None, "source_rel is not a path inside the library."
        # A film with no re-encode plays from its source, which is what the
        # preparer means by leaving media_rel out.
        media_rel = entry.get("media_rel") or source_rel
        if under(media_rel) is None:
            return None, "media_rel is not a path inside the library."
        title = str(entry.get("title") or "").strip()
        if not title:
            return None, "title is required."

        subs = []
        for sub in entry.get("subtitles") or ():
            if not isinstance(sub, dict) or under(sub.get("rel")) is None:
                return None, "a subtitle is not a path inside the library."
            subs.append({"rel": sub["rel"],
                         "lang": str(sub.get("lang") or "und").lower()[:8]})
        audio = []
        for track in entry.get("audio") or ():
            if not isinstance(track, dict) or under(track.get("rel")) is None:
                return None, "an audio track is not a path inside the library."
            try:
                number = int(track.get("n"))
            except (TypeError, ValueError):
                return None, "an audio track has no number."
            audio.append({
                "n": number, "rel": track["rel"],
                "lang": str(track.get("lang") or "und")[:8],
                "title": str(track.get("title") or "")[:200],
                "channels": _int_or_none(track.get("channels")),
                "default": bool(track.get("default")),
                "offset": _float_or(track.get("offset"), 0.0),
            })

        genres = [str(g).strip()[:60] for g in (entry.get("genres") or ())
                  if str(g).strip()]
        return {
            "id": ident,
            "source_rel": source_rel,
            "media_rel": media_rel,
            "title": title[:400],
            "sort_title": str(entry.get("sort_title") or "").strip()[:400],
            "year": _int_or_none(entry.get("year")),
            # Text in the column because it is both a score and a certificate;
            # str() here rather than a type test so 7.8 and "PG" round-trip the
            # same way, and _rating() in library.py turns it back.
            "rating": (None if entry.get("rating") in (None, "")
                       else str(entry["rating"])[:40]),
            "plot": str(entry.get("plot") or "")[:8000] or None,
            "duration": _float_or_none(entry.get("duration")),
            "width": _int_or_none(entry.get("width")),
            "height": _int_or_none(entry.get("height")),
            "vcodec": str(entry.get("vcodec") or "")[:40] or None,
            "acodec": str(entry.get("acodec") or "")[:40] or None,
            "tmdb_id": _int_or_none(entry.get("tmdb_id")),
            "hidden": 1 if entry.get("hidden") else 0,
            "genres": genres[:20],
            "subtitles": subs,
            "audio": audio,
        }, ""

    def _api_admin_thumb(self, ident):
        """A poster, as raw bytes, from the machine that made it.

        Refused for a film that is not in the library, so the table cannot
        collect posters for films that will never be shown. The magic-number
        check is not a security control -- the bytes are only ever handed back
        with the Content-Type this derives from them -- it is there so that a
        mistyped curl leaves an error rather than a tile that renders as
        nothing on six phones.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"error": "No image was sent."}, 400)
        if length > ADMIN_THUMB_MAX:
            # Not read: the point of the cap is to not allocate it.
            self.close_connection = True
            return self._json(
                {"error": "That image is larger than %d bytes."
                          % ADMIN_THUMB_MAX}, 413)
        if self.library.get(ident) is None:
            self._drain()
            return self._json({"error": "That film is not in the library."}, 404)
        data = self.rfile.read(length)
        mime = _image_type(data)
        if not mime:
            return self._json({"error": "That is not a JPEG or a PNG."}, 400)
        tag = self.thumbs.store(ident, data, mime, source="admin")
        return self._json({"id": ident, "etag": tag, "bytes": len(data)})

    def _api_admin_thumb_clear(self, ident):
        """Forget a film's poster, so the extractor may make one again.

        The way to undo a wrong poster: a re-sync will not replace it (a sync
        deliberately leaves posters alone, so it cannot discard one somebody
        uploaded), so there has to be something that says "that one was wrong".
        """
        if self.library.get(ident) is None:
            return self._json({"error": "That film is not in the library."}, 404)
        return self._json({"id": ident, "cleared": self.thumbs.clear(ident)})

    def _api_thumb(self, ident):
        """One film's poster, from the `thumb` table.

        Two sources land in the same column -- a poster the admin app uploaded
        and a frame this box extracted -- and they get the same answer here,
        which they did not when one was a file on the share and the other a
        file in /var/cache. The ETag is what replaced that split: the uploaded
        one used to be served max-age=86400 because a re-prep could change it,
        and the extracted one immutable-for-a-year because nothing could. Now
        the etag says which bytes these are, a revalidation costs one query and
        a 304, and neither has to be guessed at.
        """
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "not found"}, 404)
        found = self.thumbs.get(ident)
        if found is None:
            self.thumbs.request(ident)
            # no-store, so the next request after the frame is extracted gets
            # the real thing without the page having to cache-bust.
            return self._send(200, placeholder_svg(item["title"]),
                              "image/svg+xml", {"Cache-Control": "no-store"})
        body, mime, tag = found
        quoted = '"%s"' % tag
        # A weak comparison would be wrong here and a list is what browsers
        # actually send, so this checks membership rather than equality.
        supplied = (self.headers.get("If-None-Match") or "").split(",")
        if quoted in (s.strip() for s in supplied):
            return self._send(304, b"", mime,
                              {"ETag": quoted,
                               "Cache-Control": "public, max-age=86400"})
        self._send(200, body, mime,
                   {"ETag": quoted, "Cache-Control": "public, max-age=86400"})

    def _log_sync(self, state, position, buffering):
        """A listening phone's own account of the last second, into the journal.

        Sent by the page on every status poll while ?debug is in its URL, and
        by nothing otherwise -- which is the whole gate. There is no server-side
        switch because the alternative to logging this is not logging less, it
        is having no way at all to see what an iPhone in another room was doing
        when the sound broke up.

        Logged next to what this daemon believed at the same instant, because
        half the candidate faults are disagreements between the two: mpv's
        position against the element's, and mpv's buffering against the
        element's. A line reading

            sync 192.168.1.42 playing pos=1421.83 buf=0 v=2;id=8f2c;t=612.4;
            st=play;hid=0;ct=1421.79;rs=4;nb=1;ahead=48.2;amin=47.9;err=-38;
            errp=-41;rate=-712;ratio=-640;drift=-72;off=-1204.8;ort=11;ns=34;
            ep=7;tcage=430;rtt=24;trim=0;w=1;dw=140;sk=0;wt=0;bf=0;lag=22;ls=0

        says: the phone is 38 ms behind, correcting by 712 ppm, holding 48
        seconds of buffer, and lost no time to stalls. FIELDS --

          id      per page load, so several phones can be told apart
          t       seconds since that page loaded
          st      off (not listening) | idle (no track) | pause | play
          hid     the screen is locked or the page is in the background
          ct      the element's currentTime
          rs      HTMLMediaElement.readyState, 0-4
          nb      buffered ranges; more than one means it has been seeking
          ahead   seconds of audio past the play head, now
          amin    ...and its low-water mark since the previous line
          err     sound minus picture, ms. Negative is sound behind
          errp    the peak of that, signed, since the previous line
          rate    playbackRate as an offset in ppm -- the command, which is
                  ratio and drift together plus the proportional term
          ratio   this phone's clock against this daemon's, ppm, MEASURED off
                  /api/time rather than inferred from the audio error
          drift   what the integrator still has to add on top of that, ppm.
                  Small on a healthy phone: it is the difference between the
                  clock that timed the round trips and the one clocking the DAC
          off     this phone's clock offset from the daemon's, ms
          ort     round trip of the sample that offset came from, ms. The error
                  bar on `off` is half of this
          ns      clock samples in the window the slope is fitted through
          ep      timecode epoch. A change is a discontinuity the daemon saw
          tcage   how old the timecode anchor being extrapolated is, ms
          rtt     last round trip to this daemon, ms
          trim    this listener's manual headphone offset, ms
          w       writes to playbackRate since the previous line
          dw      the largest of them, ppm
          sk      hard seeks
          wt      waiting/stalled events on the element
          bf      polls where mpv reported paused-for-cache
          lag     worst shortfall in the element's own clock, ms
          ls      how many of those exceeded STALL
          tun     controller constants this listener has changed from the
                  debug sheet, "code:value" in the page's display units.
                  Empty on a stock build, and the only thing that makes a
                  capture taken mid-experiment interpretable afterwards.

        The three that settle it: `ahead`/`amin` collapsing means the daemon or
        the radio is starving the element, `w`/`dw` moving with the dropouts
        means this code is re-arming its pipeline, and `lag`/`ls` staying at
        zero through an audible break means neither -- the clock never stopped
        and the interruption is below anything the page can observe.
        """
        blob = self.headers.get(SYNC_HEADER)
        if not blob:
            return
        # An unauthenticated LAN client is writing to this device's journal, so
        # the value is filtered to a known-safe alphabet and truncated before it
        # goes anywhere near a log call -- and is passed as an ARGUMENT rather
        # than interpolated, so that even a '%' that survived could not reach a
        # format string.
        clean = SYNC_KEEP_RE.sub("", blob)[:SYNC_MAX]
        if not clean or not sync_allowed():
            return
        log("sync %s %s pos=%s buf=%d %s", self.client_address[0], state,
            "?" if position is None else "%.2f" % position, int(buffering), clean)

    def _api_time(self):
        """What time it is here. Nothing else, and that is the whole design.

        A phone measures its own clock against this one by timing the round
        trip: the answer was true somewhere between the request leaving and the
        reply arriving, so the midpoint is the estimate and half the round trip
        is the error bar on it. Which means every millisecond this handler
        spends is a millisecond of error bar -- so it takes no lock, reads no
        disk, asks mpv nothing, and answers first in do_GET, above the routes
        that do. log_message() is already silenced, so a burst of these costs
        no journal either.

        It is deliberately not folded into /api/status. That route takes the
        library snapshot and talks to mpv over a socket, and a phone estimating
        its clock offset from it would be measuring this daemon's own queueing
        as though it were the network. Measuring the film's position and the
        server's clock through one number is exactly the confusion the timecode
        exists to end; putting them back on one route would restore it.
        """
        self._json({"now": round(time.monotonic(), 4), "session": SESSION})

    def _api_status(self):
        state = self._state()
        data = self.player.status()
        # Through the projectionist so that a film still being prepared already
        # names itself: the page needs the id to draw its poster on the
        # preparing view, and mpv does not exist yet to be asked.
        item = self.projectionist.current_item()
        position = data.get("position")
        self._log_sync(state, position, bool(data.get("buffering")))
        tracks = []
        if PHONE_AUDIO and item:
            tracks = [
                {"n": t["n"], "lang": t["lang"], "title": t["title"],
                 "channels": t["channels"], "default": t["default"],
                 # Whatever the extracted track's own start turned out to be,
                 # measured by prep rather than assumed. Normally 0.0; the page
                 # adds it so that a container prep could not fully normalise
                 # is still fixable without touching this device.
                 "offset": t["offset"]}
                for t in item.get("audio") or []
            ]
        self._json({
            "state": state,
            # Which build of the page this daemon is serving. A page whose own
            # stamp no longer matches reloads itself -- see ui_page(). Additive
            # like everything else here, so a page from before this existed
            # carries on ignoring it and carries on needing a manual refresh.
            "build": build_id(),
            # The page cannot build an audio URL without this.
            "id": item["id"] if item else "",
            "title": self.projectionist.current_title(),
            # Unchanged shape, for the progress bar that has always read it.
            "position": position or 0,
            # ...and the distinction the progress bar does not need but a phone
            # syncing to the film does: mpv has not told us where it is yet.
            "position_valid": position is not None,
            # The master clock: where the film was, the instant on this
            # machine's clock when it was there, whether it is moving, and
            # which timeline that belongs to. Null while nothing is playing.
            # `at` is on the clock /api/time publishes and is meaningless
            # without it -- see Player._advance() for the rest.
            "timecode": data.get("timecode"),
            "buffering": bool(data.get("buffering")),
            "duration": data.get("duration", 0),
            "volume": data.get("volume"),
            # Does the PROJECTOR have sound. Nothing to do with the phones.
            "audio": HAS_AUDIO,
            # Does this build serve headphone audio at all.
            "phone_audio": PHONE_AUDIO,
            # Empty for a film nobody has run playstick-prep.py over.
            "tracks": tracks,
            # The subtitle tracks mpv actually loaded, and which of them is on
            # the picture (0 for none). Asked of mpv rather than derived from
            # the index, because a container can carry text tracks of its own.
            # Only while a film is up: sub_tracks() answers off a cache that is
            # empty until mpv has loaded the file.
            "subs": self.player.sub_tracks() if state in ("playing", "paused") else [],
            "sub": int(data.get("sid") or 0),
            "thumbs_pending": self.thumbs.pending(),
            # Which step of getting ready we are on, or null. Additive, so an
            # older ui.html that has never heard of it carries on ignoring it
            # -- the same property every field added to this payload has had.
            "prepare": self.projectionist.progress(),
            # Why the last attempt gave up, for a few seconds after it did.
            # A preparation fails on a thread, long after the POST that began
            # it answered 200, so this is the only route the explanation has.
            "notice": self.projectionist.notice(),
            # Whether there is a projector, whether its lamp is lit, and one
            # sentence if it could not be reached. The page only draws the
            # last of those, and only while a film is playing: a fault while
            # nothing is on screen is not yet anybody's problem.
            "projector": self.projectionist.projector_status(),
            # Whether the machine the films live on is switched on, and
            # whether this daemon could switch it on if asked. The page only
            # draws it when the state is already "unavailable", where it
            # turns a sentence about the NAS being off into a button.
            #
            # Additive, and the state field above is deliberately NOT changed
            # to match: "unavailable" still means "no library", so a page
            # cached from before this existed keeps working exactly as it did.
            "nas": self.nas.snapshot(),
        })

    def _api_audio(self, ident, track):
        """One prepared audio track, streamed to one phone's headphones.

        The only endpoint in this process that streams file bytes, and every
        part of it is narrower than it strictly needs to be. See the module
        header for what that buys: the id has already been proved to be sixteen
        hex characters by the route regex and is used only to look up a table,
        the track number only to index a list, and the path that comes out was
        proved to live under the library root when the index was read. Nothing
        the client sent reaches the filesystem.

        Range is not an optimisation here, it is the price of admission. iOS
        Safari opens every media resource with `Range: bytes=0-1` and, if the
        answer is a 200, refuses the resource outright rather than falling back
        to a whole-file fetch.
        """
        item = self.library.get(ident)
        tracks = (item or {}).get("audio") or []
        if item is None or not 0 <= track < len(tracks):
            return self._json({"error": "not found"}, 404)

        path = tracks[track]["path"]
        try:
            stat = os.stat(path)
            handle = open(path, "rb")
        except OSError:
            # Listed in the index but not on the share: somebody re-prepped the
            # library, or the NAS went away. A 404 the page can put into words,
            # not a traceback in the journal.
            log("audio track missing from the share: %s", path)
            return self._json({"error": "That soundtrack is not on the share."}, 404)

        # Each listener holds a thread for as long as they listen. Refusing the
        # seventh is better than starving the page that every phone in the
        # house is polling.
        if not AUDIO_SLOTS.acquire(blocking=False):
            handle.close()
            log("refusing an audio stream: all %d slots busy", PHONE_AUDIO_STREAMS)
            return self._send(503, b'{"error":"too many listeners"}',
                              "application/json", {"Retry-After": "2"})
        try:
            self._stream_audio(handle, stat)
        finally:
            AUDIO_SLOTS.release()
            handle.close()

    def _stream_audio(self, handle, stat):
        size = stat.st_size
        etag = '"%x-%x"' % (size, stat.st_mtime_ns)
        start, end = 0, size - 1
        partial = False

        span = self.headers.get("Range")
        # A phone that cached part of a track it then re-requests must not be
        # handed bytes from a different file at the same offsets, which is the
        # whole job of If-Range. A mismatch means "ignore the Range", not
        # "fail" -- the client gets the whole thing and sorts itself out.
        if span and self.headers.get("If-Range", etag) == etag:
            match = RANGE_RE.match(span.strip())
            if not match or not (match.group(1) or match.group(2)):
                return self._send(416, b"", "application/json",
                                  {"Content-Range": "bytes */%d" % size})
            if match.group(1):
                start = int(match.group(1))
                if match.group(2):
                    end = min(int(match.group(2)), size - 1)
            else:
                # A suffix range: the last N bytes. Safari asks for this when
                # it wants the tail of an mp4, which for a +faststart file it
                # normally does not need -- but it is two lines to be correct.
                start = max(0, size - int(match.group(2)))
            if start >= size or start > end:
                return self._send(416, b"", "application/json",
                                  {"Content-Range": "bytes */%d" % size})
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "audio/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("ETag", etag)
        # Private: this is one household's film, and there is no proxy in the
        # path that should be keeping it.
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()

        if self.command == "HEAD":
            return

        # Never via _send(): that takes a complete body, and two phones holding
        # 80 MB of film soundtrack in this process is most of the RAM on the
        # device.
        handle.seek(start)
        sent = 0
        began = time.monotonic()
        try:
            while sent < length:
                chunk = handle.read(min(AUDIO_CHUNK, length - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)
                if PHONE_AUDIO_BPS:
                    # A token bucket with a burst rather than a hard cap. The
                    # first PHONE_AUDIO_BURST seconds of audio go out as fast as
                    # the socket will take them, so playback starts at once and
                    # has a cushion; after that the stream is paced. What is
                    # being protected is the film's own CIFS read, which crosses
                    # the same radio in the other direction and which nobody can
                    # do anything about once it starts stuttering.
                    #
                    # Slept in short steps and in a LOOP, which is not
                    # decoration. One capped sleep per chunk sounds equivalent
                    # and is not: a chunk owes far more than the cap at any
                    # sensible rate -- 128 KiB at 384 kbps owes 2.7 s -- so a
                    # single capped sleep silently turns the limit into "one
                    # chunk per cap", about 1 MB/s, and the pacing stops pacing
                    # anything. The cap is only here so that a shutdown is not
                    # waiting on a minute-long sleep.
                    due = began + sent / PHONE_AUDIO_BPS - PHONE_AUDIO_BURST
                    while True:
                        owed = due - time.monotonic()
                        if owed <= 0:
                            break
                        time.sleep(min(owed, 0.25))
        except (BrokenPipeError, ConnectionResetError):
            # Somebody switched language, locked their phone, or walked out of
            # Wi-Fi range. All of them look like this and none of them are
            # worth a line in the journal.
            self.close_connection = True

    def _api_play(self):
        ident = str(self._body().get("id", ""))
        item = self.library.get(ident)
        if item is None:
            return self._json({"error": "That film is not in the library."}, 404)
        try:
            # Returns as soon as the film is accepted. Everything after that --
            # the lamp, the input, taking the display, mpv -- happens on a
            # thread and is reported through /api/status, because a cold lamp
            # takes longer to light than a browser will hold a request open.
            self.projectionist.begin(item)
        except Busy as exc:
            return self._json({"error": str(exc), "state": self._state()}, 409)
        except Exception as exc:                     # noqa: BLE001
            log("play failed: %s", exc)
            return self._json({"error": "The player would not start."}, 500)
        return self._api_status()
